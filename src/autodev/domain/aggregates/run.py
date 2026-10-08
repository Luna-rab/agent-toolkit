"""Run 集約。

ランの全体と、タスクの依存のグラフと各タスクの状態（TaskStatus）を持つ。タスクをいつ始め、いつ
止めるかはここで決まる。ラン統括の判断は、質問（PostQuestion）を除いてすべてここが受ける。

ほかの集約で起きたこと（エスカレーション・フローの終わり・積む列から取り出した、など）による
状態の遷移は、ポリシーが UpdateTaskStatus で知らせる。Run が自分のコマンドで
決める遷移（始める・止める・引き継ぐ・破棄する・積み直す・積んだ・積む列から外す）は、そのイベントで
動かす。running へ入るのは、TaskScheduler が上限と依存を確かめる TaskStarted と、もう上限に
数えている escalated から戻るときだけである。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum

from ..commands.run import (
    AnswerEscalation,
    ApplyPlan,
    ApplyReplan,
    ClearIntegrationFailure,
    CloseRelayedEscalation,
    EscalateToRun,
    FinishRun,
    InsertTask,
    MarkStacked,
    Panic,
    RecordAnswer,
    RecordIntegrationFailure,
    RecordSettledPlan,
    ReportSupervisorFailure,
    RequestReplan,
    ResumeRun,
    ReturnToQueue,
    StartReadyTasks,
    StartRun,
    StartTask,
    StopTasks,
    UpdateTaskStatus,
)
from ..events.base import Event
from ..events.run import (
    AllTasksSettled,
    AnswerRecorded,
    EscalationAnswered,
    EscalationRaised,
    IntegrationFailureCleared,
    IntegrationFailureRecorded,
    ReplanRequested,
    RunFinished,
    RunPanicked,
    RunResumed,
    RunStarted,
    SettledPlanRecorded,
    TaskInserted,
    TaskMarkedStacked,
    TasksDiscarded,
    TasksPlanned,
    TasksReturnedToQueue,
    TasksStopped,
    TaskStarted,
    TaskStatusChanged,
    TaskSuperseded,
)
from ..events.task import EscalationClosed
from ..services.escalation_router import EscalationRouter, Route, SupervisorLevel
from ..services.task_scheduler import SchedulingEntry, TaskScheduler
from ..value_objects.artifact_kind import ArtifactKind
from ..value_objects.artifact_ref import ArtifactRef
from ..value_objects.branch_name import BranchName
from ..value_objects.design_version import DesignVersion
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.event_id import EventId
from ..value_objects.limits import MAX_REPLANS_WITHOUT_STACK, MAX_SUPERVISOR_FAILURES
from ..value_objects.model_class import ModelClasses
from ..value_objects.parallel_limit import ParallelLimit
from ..value_objects.planned_task import PlannedTask
from ..value_objects.pointers import Pointers
from ..value_objects.proposal import Proposal
from ..value_objects.question_id import QuestionId
from ..value_objects.run_name import RunName
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId
from ..value_objects.task_kind import TaskKind
from ..value_objects.task_spec import TaskSpec
from ..value_objects.task_status import TaskStatus
from ..value_objects.verify_command import VerifyCommand
from .base import Aggregate, Rejected, applies, handles

_S = TaskStatus

#: ほかの集約のイベントを受けて、ポリシーが UpdateTaskStatus で動かす遷移。
#: Run 自身のコマンドで決まる遷移は含めない。gated から running へは戻さない（範囲が変わった
#: gated のタスクは、まだ始めていない状態に戻し、TaskScheduler が上限を見て始め直す）
REPORTED_TRANSITIONS: Mapping[TaskKind, frozenset[tuple[TaskStatus, TaskStatus]]] = {
    TaskKind.IMPLEMENTATION: frozenset(
        {
            # EscalationRaised
            (_S.RUNNING, _S.ESCALATED),
            # EscalationResolved・EscalationClosed・FlowAccepted・ScopeChanged
            (_S.ESCALATED, _S.RUNNING),
            # TaskGated
            (_S.RUNNING, _S.GATED),
            # GitJobTaken（積む仕事）
            (_S.GATED, _S.STACKING),
            # GitJobFinished（積む仕事のフローを捨て、仕事を積む列に残した）。再計画で範囲が変われば
            # gated のタスクとして積む列から外せる
            (_S.STACKING, _S.GATED),
        }
    ),
    TaskKind.PLANNING: frozenset({(_S.RUNNING, _S.ESCALATED), (_S.ESCALATED, _S.RUNNING)}),
    TaskKind.GIT: frozenset(
        {
            (_S.RUNNING, _S.ESCALATED),
            (_S.ESCALATED, _S.RUNNING),
            # 仕上げの並びを終えたとき。RunFinished の後だけ
            (_S.RUNNING, _S.FINISHED),
        }
    ),
}

#: 書き換えないタスク。stacked は確定した再計画の破棄だけが動かす
_UNTOUCHABLE = frozenset({_S.DROPPED, _S.SUPERSEDED, _S.DISCARDED})
#: 止められる状態
_STOPPABLE = frozenset({_S.PENDING, _S.RUNNING, _S.ESCALATED, _S.GATED, _S.STACKING})
#: 範囲が変わったら、そのまま ChangeScope を送る状態（もう並列の上限に数えている）
_SCOPE_CHANGEABLE = frozenset({_S.RUNNING, _S.ESCALATED})
#: 範囲が変わったら、積む列から外して始め直す状態
_WITHDRAWABLE = frozenset({_S.GATED})


class RunPhase(Enum):
    """ランがいまどこにいるか（`Run.phase`。外向けの `status --json` の `run.phase`）。"""

    #: RunStarted がまだ無い
    NOT_STARTED = "not-started"
    #: 初めての計画を反映する前（実装タスクはまだ無い）
    PLANNING = "planning"
    RUNNING = "running"
    #: パニックした。呼び直されるまでタスクを始めない
    PANICKED = "panicked"
    #: RunFinished の後、git 管理タスクが仕上げの並びを走らせている
    FINISHING = "finishing"
    #: 仕上げも終えた（driver が終了コード 0 で終える）
    FINISHED = "finished"

    @property
    def expects_driver(self) -> bool:
        """driver が走っているはずのフェーズ（running・planning・finishing）。"""
        return self in _DRIVEN_PHASES


_DRIVEN_PHASES = frozenset({RunPhase.PLANNING, RunPhase.RUNNING, RunPhase.FINISHING})


@dataclass(frozen=True)
class TaskEntry:
    """Run が持つタスク 1 つ。"""

    id: TaskId
    status: TaskStatus
    spec: TaskSpec | None = None
    blocked_by: frozenset[TaskId] = frozenset()
    takes_over: TaskId | None = None
    superseded_by: TaskId | None = None
    #: ブランチを切り直した回数（破棄の後に積み直した・積む列から外して始め直した）。ブランチ名の
    #: `-r<回数>` になる。push したかもしれない名前は使い回さない
    branch_round: int = 0
    #: 一度でも始めたか（始め直すときは OpenTask ではなく ChangeScope になる）
    started: bool = False
    #: 統合に失敗したときに衝突したファイル（積んでいる途中のタスク）。引き継いだタスクなら、
    #: 引き継ぎ元のもの（解き直す）
    conflicts: tuple[str, ...] = ()
    #: 積んでいる途中で統合に失敗した（IntegrationFailureRecorded）。引き継げるのはこのタスクだけ
    integration_failed: bool = False

    @property
    def is_implementation(self) -> bool:
        return self.id.kind is TaskKind.IMPLEMENTATION

    @property
    def own_findings(self) -> StreamId | None:
        """そのタスクの欄に出す指摘の台帳。実装タスクだけが持つ（計画タスクの指摘は設計の台帳で、
        ランの計画として出す）。"""
        return StreamId.review(self.id) if self.is_implementation else None


@dataclass(frozen=True)
class RunEscalation:
    """Run の側で開いているエスカレーション（タスクの統括が上げてきたもの）。"""

    id: EventId
    kind: EscalationKind
    task: TaskId | None
    #: 上げる元になったタスクの側のエスカレーション
    source: EventId | None
    #: supervisor-failed なら、統括が応じられなかった知らせ
    failed_notice: EventId | None = None
    #: supervisor-failed なら、同じ知らせで続けて応じなかった数（EscalationRaised.failures）
    failures: int = 0
    #: 答え以外では閉じられない（EscalationRaised.answer_only）
    answer_only: bool = False
    #: 上げる理由（EscalationRaised.reason）
    reason: str = ""
    #: ユーザーに聞くとよいこと（EscalationRaised.question）
    question: str | None = None

    @property
    def route(self) -> Route:
        return EscalationRouter.route(StreamId.run(), self.kind, self.task)

    @property
    def for_user(self) -> bool:
        """`/autodev`（ユーザー）が受けるか。ラン統括はこのエスカレーションに応えない・閉じない。"""
        return self.route.level is SupervisorLevel.USER

    @property
    def requires_user_answer(self) -> bool:
        """ラン統括が answer で応えるなら、自分で書いた答えではなく、ユーザーの回答を添える。"""
        return EscalationRouter.requires_user_answer(self.kind, self.failures)


@dataclass(frozen=True)
class RecordedAnswer:
    answer: str
    #: その質問を出したエスカレーション。回答はこのエスカレーションにだけ使える
    escalation: EventId | None
    used: bool = False


class Run(Aggregate):
    NAME = "Run"

    def __init__(self, stream: StreamId) -> None:
        super().__init__(stream)
        self.name: RunName | None = None
        self.limit = ParallelLimit(ParallelLimit.DEFAULT)
        #: クラスごとのモデルと effort。記録の無い古いランと、RunStarted の前は既定値
        self.models = ModelClasses.default()
        self.tasks: dict[TaskId, TaskEntry] = {}
        #: 使ったことのある実装タスクの番号（捨てたタスクのブランチが残るので、使い回さない）
        self.used_numbers: set[int] = set()
        #: 計画が進んでいる間に差し込んだタスク。その計画の提案が同じ番号を書いていたら拒む
        self.inserted_while_planning: set[TaskId] = set()
        #: 今スタックに積んである順（下から）
        self.stack_order: list[TaskId] = []
        #: 破棄した所より上に積んであって、積む列に戻すのを待っているタスク（下から）。積む列に
        #: 戻す一覧の出どころはここだけで、閉じ終えた（StackCutBack → ReturnToQueue）ときに戻す
        self.requeue: tuple[TaskId, ...] = ()
        #: 破棄して、まだ閉じ終えていない数。0 になるまで、ランは終わらない
        self.cuts_pending = 0
        self.escalations: dict[EventId, RunEscalation] = {}
        #: 開いたことのある Run のエスカレーション（閉じても消さない）。回答が届いたときに、答える先が
        #: 閉じたのか、もともと Run のエスカレーションではない id（取り違え）なのかを分ける
        self.opened_escalations: set[EventId] = set()
        self.answers: dict[QuestionId, RecordedAnswer] = {}
        #: タスクを 1 本も積まないまま続けた再計画の数
        self.replan_streak = 0
        #: 計画が進んでいる（初回はランの開始から、再計画は ReplanRequested から、TasksPlanned まで）
        self.planning = False
        #: 進んでいる再計画のきっかけ（Run の側のエスカレーション）。反映したら閉じる。確定した提案を
        #: 退けて頼み直したら、前のきっかけに足す
        self.replan_triggers: tuple[EventId, ...] = ()
        #: 確定して、まだ反映していない提案
        self.settled: SettledPlanRecorded | None = None
        #: 設計が一度でも確定したか
        self.settled_once = False
        #: 計画を一度でも反映したか
        self.planned_once = False
        #: ラン共通の成果物（brief・codemap・design）と、軽い検査・回帰テスト
        self.artifacts: tuple[ArtifactRef, ...] = ()
        self.quick_checks: tuple[VerifyCommand, ...] = ()
        self.regression_tests: tuple[VerifyCommand, ...] = ()
        #: パニックした原因（RunPanicked の cause）。呼び直したら None に戻す
        self.panic_cause: str | None = None
        self.finished = False
        #: 呼び直された回数（RunResumed の数）
        self.resumes = 0
        #: Run の知らせ（エスカレーション・回答）が関わるタスク。ラン統括が応じなかった知らせが
        #: どのタスクのものかを引く（ランを終えた後に受けてよいかを、そのタスクで決める）
        self.notice_tasks: dict[EventId, TaskId] = {}
        #: supervisor-failed を上げたことのある知らせ（閉じた後も残す）
        self.failed_notices: set[EventId] = set()
        #: 起こし直しの知らせ（supervisor-failed への回答）→ そこまでに続けて応じなかった数
        self.retry_failures: dict[EventId, int] = {}

    # --- 読む ---

    @property
    def panicked(self) -> bool:
        return self.panic_cause is not None

    def reported_failure(self, notice: EventId) -> bool:
        """その知らせに統括が応じなかったことを、もう受けたか（supervisor-failed を上げた）。

        呼び直した driver は、受けた知らせで統括を起こし直さない（上げたエスカレーションが知らせを持つ）。
        """
        return notice in self.failed_notices

    def schedule_view(self) -> dict[TaskId, SchedulingEntry]:
        """TaskScheduler に渡す見え方。積む列へ戻すのを待つタスクは、もう積んだものに数えない
        （閉じる所より上にあって、スタックから外れる。待つタスクを始めると、その変更の無い一番上から切る）。"""
        return {
            task: SchedulingEntry(
                task, _S.GATED if task in self.requeue else entry.status, entry.blocked_by
            )
            for task, entry in self.tasks.items()
        }

    @property
    def all_settled(self) -> bool:
        """すべての実装タスクが終端になった（AllTasksSettled の条件。名前の付いた規則）。

        計画を反映していて、計画が進んでおらず、破棄の後に閉じ終えていて積む列へ戻すのを待つタスクも
        無いこと。実装タスクが 0 件の計画でも、反映したら終端になったとみなす（ラン統括が仕上げる）。
        積む列へ戻すのを待つタスクは stacked のままだが、積み直すまで終端とみなさない。
        """
        return (
            self.planned_once
            and not self.planning
            and not self.cuts_pending
            and not self.requeue
            and all(
                entry.status.is_terminal for entry in self.tasks.values() if entry.is_implementation
            )
        )

    @property
    def complete(self) -> bool:
        """ランを終え（RunFinished）、仕上げの並びも終えた（終端でないタスクが無い）。

        driver が終了コード 0 で終えてよいかの問い。git 管理タスクは RunFinished の後に仕上げの並びを
        走らせ、その途中で上げることもあるので、RunFinished だけでは終えない。
        """
        return self.finished and not self.live_tasks

    @property
    def phase(self) -> RunPhase:
        """パニックは、ランを終えた後（仕上げの途中）にも起きるので、終わりより先に見る。"""
        if self.name is None:
            return RunPhase.NOT_STARTED
        if self.panicked:
            return RunPhase.PANICKED
        if self.complete:
            return RunPhase.FINISHED
        if self.finished:
            return RunPhase.FINISHING
        if not self.planned_once:
            return RunPhase.PLANNING
        return RunPhase.RUNNING

    @property
    def applied_design(self) -> DesignVersion | None:
        """最後に反映した計画の設計の版。まだ反映していなければ None。"""
        for artifact in self.artifacts:
            if artifact.kind is ArtifactKind.DESIGN:
                return DesignVersion(int(artifact.at))
        return None

    def driver_stopped(self, driver_running: bool, awaiting_answer: bool) -> bool:
        """driver が走っているはずのフェーズなのに走っておらず、回答を待っているのでもない。
        driver の事実と回答待ちはこの集約の外にあるので、呼ぶ側が渡す。"""
        return self.phase.expects_driver and not driver_running and not awaiting_answer

    @property
    def stacked_count(self) -> int:
        """実装タスクのうち、status が stacked の数。"""
        return sum(1 for entry in self.implementation_tasks if entry.status is _S.STACKED)

    @property
    def stack_target_count(self) -> int:
        """実装タスクのうち、dropped・superseded・discarded を除いた数（積む数の分母）。"""
        return sum(1 for entry in self.implementation_tasks if entry.status not in _UNTOUCHABLE)

    @property
    def implementation_tasks(self) -> tuple[TaskEntry, ...]:
        """実装タスク（番号の順）。計画タスクと git 管理タスクは入らない。"""
        return tuple(
            sorted(
                (entry for entry in self.tasks.values() if entry.is_implementation),
                key=lambda entry: _number(entry.id),
            )
        )

    @property
    def live_tasks(self) -> tuple[TaskId, ...]:
        """終端でないタスク（計画タスク・git 管理タスクを含む）。呼び直したときに再開を頼む相手。"""
        return tuple(task for task, entry in self.tasks.items() if not entry.status.is_terminal)

    def successor(self, task: TaskId) -> TaskId:
        """引き継がれたタスクなら、引き継ぎ先（の引き継ぎ先…）。"""
        seen = {task}
        while (entry := self.tasks.get(task)) is not None and entry.superseded_by is not None:
            task = entry.superseded_by
            if task in seen:
                break
            seen.add(task)
        return task

    # --- 共通の検査 ---

    def _require_open(self) -> None:
        if self.name is None:
            raise Rejected("ランが始まっていない")
        if self.finished:
            raise Rejected("ランはもう終わっている")

    def _require_running(self) -> None:
        self._require_open()
        if self.panicked:
            raise Rejected("パニックの後、再開するまでタスクを始めない")

    def _require_escalation_route(self, task: TaskId | None) -> None:
        """エスカレーションとその回答を受けてよいか（名前の付いた規則）。

        ランが終わった後も、git 管理タスクのものは受ける。git 管理タスクは仕上げの並び（RunFinished の
        後に走る）を終えるまで続き、その途中で上げることがあるからである。
        """
        if self.name is None:
            raise Rejected("ランが始まっていない")
        if self.finished and task != TaskId.git():
            raise Rejected(
                "ランはもう終わっている（受けるのは git 管理タスクのエスカレーションだけ）"
            )

    def _entry(self, task: TaskId) -> TaskEntry:
        entry = self.tasks.get(task)
        if entry is None:
            raise Rejected(f"{task} はランに無い")
        return entry

    def _open_escalation(self, escalation: EventId | None) -> RunEscalation | None:
        """ラン統括の判断が応える・閉じるエスカレーション（名前の付いた規則）。

        ユーザーが受けるもの（ラン統括自身が応じなかった supervisor-failed）は拒む。ラン統括がそれを
        閉じると、ユーザーの代わりに答えたことになる。閉じるのはユーザーの回答だけ（RecordAnswer）。
        """
        if escalation is None:
            return None
        found = self.escalations.get(escalation)
        if found is None:
            raise Rejected(f"{escalation} は開いている Run のエスカレーションではない")
        if found.for_user:
            raise Rejected(
                f"{escalation} は /autodev（ユーザー）が受けるエスカレーション（{found.kind.value}）で、"
                "ラン統括は応えない。ユーザーの回答が届くと閉じる"
            )
        return found

    def _closable(self, escalation: EventId | None) -> RunEscalation | None:
        """ラン統括の答え以外の判断（差し込む・止める・再計画する・反映する）で閉じてよいエスカレーション
        （名前の付いた規則）。

        答え以外では閉じられないもの（`answer_only`。やめるとランが終わらない止めた git の仕事）を拒む。
        閉じるとタスクの側で仕事をやめようとして Stack が拒み、仕事が止まったまま誰も起こさなくなる。
        拒めば、ラン統括は理由を添えて差し戻され、答え直せる。
        """
        found = self._open_escalation(escalation)
        if found is not None and found.answer_only:
            raise Rejected(
                f"{found.id} は答え以外では閉じられないエスカレーション（{found.task} の"
                f" {found.kind.value}）。止めた git の仕事は、やめるとランが終わらないので、"
                f"insert-task・stop-tasks・replan・apply-plan では閉じない。answer で答えて"
                f"（escalation に {found.id} を書く）続ける"
            )
        return found

    def _settles(self, events: list[Event]) -> list[Event]:
        """最後の実装タスクが、どの道で終端になっても AllTasksSettled を出す。"""
        if self.all_settled or not self.preview(events).all_settled:
            return []
        return [AllTasksSettled()]

    def _branch(self, entry: TaskEntry, branch_round: int) -> BranchName:
        name = self.name
        assert name is not None
        return BranchName.for_task(name, _number(entry.id), branch_round)

    def _started(self, entry: TaskEntry) -> TaskStarted:
        if not entry.is_implementation:
            return TaskStarted(entry.id, entry.id.kind)
        artifacts = self.artifacts
        if entry.conflicts and entry.takes_over is not None:
            # 引き継いだ衝突は、引き継ぎ先の成果物として渡す（ResolveConflict が書いてよいファイル）
            artifacts = (*artifacts, ArtifactRef(ArtifactKind.CONFLICTS, str(entry.takes_over)))
        return TaskStarted(
            entry.id,
            TaskKind.IMPLEMENTATION,
            spec=entry.spec,
            blocked_by=entry.blocked_by,
            artifacts=artifacts,
            branch=self._branch(entry, entry.branch_round),
            reopened=entry.started,
            conflicts=entry.conflicts if entry.takes_over is not None else (),
        )

    def _usable_answer(self, question: QuestionId, escalation: EventId | None) -> RecordedAnswer:
        """ユーザーの回答を使えるか。1 回使った回答と、別のエスカレーションへの流用を拒む。"""
        recorded = self.answers.get(question)
        if recorded is None:
            raise Rejected(f"{question} の回答はまだ届いていない")
        if recorded.used:
            raise Rejected(f"{question} の回答はもう使った。1 つの回答は 1 回だけ使える")
        if recorded.escalation is not None and recorded.escalation != escalation:
            raise Rejected(
                f"{question} の回答は、その質問を出したエスカレーション（{recorded.escalation}）にだけ使える"
            )
        return recorded

    @staticmethod
    def _close(escalation: RunEscalation, reason: str) -> EscalationClosed:
        """回答以外で閉じる。タスクの側の元のエスカレーションも閉じられるように、task と source を載せる。"""
        return EscalationClosed(escalation.id, reason, escalation.task, escalation.source)

    def _close_responded(
        self, events: list[Event], responds: RunEscalation | None, reason: str
    ) -> list[Event]:
        """応えたエスカレーションを、まだ閉じていなければ閉じる。"""
        closed = {e.escalation for e in events if isinstance(e, EscalationClosed)}
        if responds is None or responds.id in closed:
            return events
        return [*events, self._close(responds, reason)]

    # --- 始める ---

    @handles(StartRun)
    def _start_run(self, command: StartRun) -> list[Event]:
        if self.name is not None:
            raise Rejected(f"ラン {self.name} はもう始まっている")
        return [
            RunStarted(
                command.name,
                command.instruction,
                command.repository,
                command.base,
                command.limit,
                command.models,
            )
        ]

    @handles(StartTask)
    def _start_task(self, command: StartTask) -> list[Event]:
        self._require_running()
        task = command.task
        if task.kind is not TaskKind.IMPLEMENTATION:
            # 計画タスクと git 管理タスクは、ランに 1 つずつ。ランが終わるまで続き、始め直さない
            if task in self.tasks:
                raise Rejected(f"{task.kind.value} のタスクはもう始まっている")
            return [TaskStarted(task, task.kind)]
        if self.planning and self.planned_once:
            raise Rejected("再計画が進んでいる間は、新しい実装タスクを始めない")
        if reason := TaskScheduler.why_not(task, self.schedule_view(), self.limit):
            raise Rejected(reason)
        return [self._started(self.tasks[task])]

    @handles(StartReadyTasks)
    def _start_ready(self, command: StartReadyTasks) -> list[Event]:
        # ポリシーは Run の状態を知らずに出すので、始めない間は拒まずに何もしない。再計画が進んでいる
        # 間に始めると、反映した計画で止めることになりうる。パニックの後は、呼び直された（RunResumed）
        # ときにもう一度頼まれる
        if self.name is None:
            raise Rejected("ランが始まっていない")
        if self.finished or self.panicked or self.planning:
            return []
        chosen = TaskScheduler.startable(self.schedule_view(), self.limit)
        return [self._started(self.tasks[task]) for task in chosen]

    # --- 計画 ---

    @handles(RecordSettledPlan)
    def _record_settled(self, command: RecordSettledPlan) -> list[Event]:
        self._require_open()
        if not self.planning:
            raise Rejected("計画が進んでいないのに、確定した提案が届いた")
        return [SettledPlanRecorded(command.proposal, command.artifacts, replan=self.planned_once)]

    def _settled_proposal(self, design: object) -> Proposal:
        if self.settled is None:
            raise Rejected("確定して、まだ反映していない提案が無い")
        proposal = self.settled.proposal
        if proposal.design != design:
            raise Rejected(f"確定した提案の版は {proposal.design} で、{design} ではない")
        return proposal

    @handles(ApplyPlan)
    def _apply_plan(self, command: ApplyPlan) -> list[Event]:
        self._require_open()
        proposal = self._settled_proposal(command.design)
        if self.planned_once:
            raise Rejected(
                "計画はもう反映してある。再計画の反映はラン統括の apply-plan（ApplyReplan）"
            )
        events = self._plan(proposal)
        return events + self._settles(events)

    @handles(ApplyReplan)
    def _apply_replan(self, command: ApplyReplan) -> list[Event]:
        self._require_open()
        proposal = self._settled_proposal(command.design)
        if not self.planned_once:
            raise Rejected("まだ一度も計画を反映していない。初めての反映は ApplyPlan")
        if not command.stop <= proposal.stop:
            extra = ", ".join(sorted(str(t) for t in command.stop - proposal.stop))
            raise Rejected(f"確定した再計画が止めると提案していないタスクを止めようとした: {extra}")
        if not command.discard <= proposal.discard:
            extra = ", ".join(sorted(str(t) for t in command.discard - proposal.discard))
            raise Rejected(f"確定した再計画が破棄を提案していないタスクを破棄しようとした: {extra}")
        responds = self._closable(command.responds_to)
        events: list[Event] = []
        if command.stop:
            # 止めたタスクを待つタスクは、反映した後のグラフで確かめる（新しい計画が付け替えうる）
            events += self._stop(command.stop, check_waiting=False)
        if command.discard:
            events += self._discard(command.discard, proposal)
        # 止めた・破棄した後の状態で、計画を確かめる（依存先が止めたタスクなら拒む）。応えた
        # エスカレーションは一緒に閉じるので、まだ開いているものに数えない
        closing = frozenset({responds.id}) if responds is not None else frozenset()
        events += self.preview(events)._plan(proposal, closing=closing)
        events = self._close_responded(events, responds, "再計画を反映した")
        return events + self._settles(events)

    def _plan(self, proposal: Proposal, closing: frozenset[EventId] = frozenset()) -> list[Event]:
        """確定した提案を反映する TasksPlanned（ときに EscalationClosed）を決める。

        `closing` は、同じコマンドで閉じる Run のエスカレーション（反映した後も開いているものに数えない）。
        """
        planned = self._resolve_plan(proposal)
        mentioned = {task.id for task in planned}
        # 提案に書かれていない、終端でない実装タスクは、今の中身のまま残す
        kept = tuple(
            PlannedTask(entry.id, entry.spec, entry.blocked_by)
            for entry in sorted(self.tasks.values(), key=lambda e: e.id.number or 0)
            if entry.is_implementation
            and not entry.status.is_terminal
            and entry.id not in mentioned
            and entry.spec is not None
        )
        graph = {task.id: task.blocked_by for task in (*planned, *kept)}
        self._check_graph(graph)
        changed = {
            task.id
            for task in planned
            if (entry := self.tasks.get(task.id)) is not None and entry.spec != task.spec
        }
        # 反映したときに閉じるきっかけ。もう閉じたもの（応えた・止めたタスクのもの）は除く
        triggers = [self.escalations[e] for e in self.replan_triggers if e in self.escalations]
        for trigger in triggers:
            if trigger.task is not None and trigger.task.kind is TaskKind.IMPLEMENTATION:
                # きっかけのエスカレーションを上げた実装タスクには、範囲が変わらなくても知らせる。
                # ChangeScope は実装タスクにだけ送る
                changed.add(trigger.task)
        status = {task: entry.status for task, entry in self.tasks.items()}
        closed = closing | {trigger.id for trigger in triggers}
        # ユーザーが受けるものは、ラン統括に渡さない（応えられない）
        still_open = tuple(
            e for e, found in self.escalations.items() if e not in closed and not found.for_user
        )
        events: list[Event] = [
            TasksPlanned(
                design=proposal.design,
                tasks=(*planned, *kept),
                quick_checks=proposal.quick_checks,
                regression_tests=proposal.regression_tests,
                artifacts=self.settled.artifacts if self.settled else (),
                replan=self.planned_once,
                triggers=tuple(trigger.id for trigger in triggers),
                scope_changed=frozenset(t for t in changed if status.get(t) in _SCOPE_CHANGEABLE),
                withdraw=frozenset(t for t in changed if status.get(t) in _WITHDRAWABLE),
                carry=proposal.carry,
                still_open=still_open,
            )
        ]
        events += [self._close(trigger, "再計画を反映した") for trigger in triggers]
        return events

    def _resolve_plan(self, proposal: Proposal) -> tuple[PlannedTask, ...]:
        """提案のタスクを確かめ、依存先の引き継がれたタスクを引き継ぎ先に付け替える。"""
        resolved: list[PlannedTask] = []
        for task in proposal.tasks:
            if task.id.kind is not TaskKind.IMPLEMENTATION:
                raise Rejected(f"計画に書けるのは実装タスクだけ（{task.id}）")
            if task.id in self.inserted_while_planning:
                # 提案は差し込む前の一覧から書いたので、同じ番号は別のタスクを指している
                raise Rejected(
                    f"{task.id} は計画の間に差し込んだタスクの番号で、提案のタスクとぶつかる"
                )
            entry = self.tasks.get(task.id)
            if entry is None:
                if _number(task.id) in self.used_numbers:
                    raise Rejected(
                        f"{task.id} の番号は使ったことがある。新しいタスクは新しい番号で書く"
                    )
            elif entry.status in _UNTOUCHABLE:
                raise Rejected(f"{task.id} は {entry.status.value} で、書き換えない")
            elif entry.status in (_S.STACKED, _S.STACKING) and entry.spec != task.spec:
                raise Rejected(
                    f"{task.id} は {entry.status.value} で、中身を書き換えない（破棄するなら提案の破棄の候補にする）"
                )
            blocked = frozenset(self.successor(dependency) for dependency in task.blocked_by)
            resolved.append(replace(task, blocked_by=blocked))
        return tuple(resolved)

    def _check_graph(self, graph: Mapping[TaskId, frozenset[TaskId]]) -> None:
        """依存先が在り、止めた・破棄したタスクでなく、循環が無い。"""
        for task, dependencies in graph.items():
            for dependency in sorted(dependencies, key=str):
                if dependency == task:
                    raise Rejected(f"{task} が自分を待っている")
                if dependency in graph:
                    continue
                entry = self.tasks.get(dependency)
                if entry is None or not entry.is_implementation:
                    raise Rejected(f"{task} が待つ {dependency} は計画に無い")
                if entry.status in (_S.DROPPED, _S.DISCARDED):
                    raise Rejected(
                        f"{task} が {entry.status.value} の {dependency} を待っている（積まれることが無い）"
                    )
        if cycle := _find_cycle(graph):
            raise Rejected("依存のグラフが輪になっている: " + " → ".join(str(t) for t in cycle))

    @handles(RequestReplan)
    def _request_replan(self, command: RequestReplan) -> list[Event]:
        self._require_open()
        trigger = self._closable(command.trigger)
        planning_task = TaskId.planning()
        from_planning = trigger is not None and trigger.task == planning_task
        if self.planning and self.settled is None and not from_planning:
            # 計画が進んでいる間（初回も含む）は受けない。ただし計画タスクのエスカレーション（ask
            # など）に replan で応じるのは、進んでいる計画をやり直すことなので受ける。確定した
            # 提案が反映を待っているなら、それを退けて頼み直せる
            raise Rejected(
                "計画が進んでいる間は、計画タスクのエスカレーションに応じる再計画と、確定した提案を"
                "退ける再計画だけを受ける"
            )
        if self.replan_streak >= MAX_REPLANS_WITHOUT_STACK and command.answer is None:
            raise Rejected(
                f"タスクを積まないまま {self.replan_streak} 回再計画した。ユーザーに聞いてから、"
                "その回答の QuestionId を添えて頼み直す"
            )
        if command.answer is not None:
            self._usable_answer(command.answer, command.trigger)
        # 進んでいた再計画（確定した提案を退けた・計画タスクのエスカレーションに応じた）のきっかけは、
        # 置き換えずに残す。退けた提案を反映しなくても、前のきっかけは今度の反映で片付く。計画タスクの
        # エスカレーションは一緒に閉じるので足さない
        closes_on_apply = self.replan_triggers if self.planning else ()
        if trigger is not None and not from_planning and trigger.id not in closes_on_apply:
            closes_on_apply = (*closes_on_apply, trigger.id)
        # 計画タスクの側で待っているエスカレーションは、再計画で要らなくなるので閉じる。
        # ReplanRequested より前に出す。ReplanRequested を受けた計画タスクの統括が新しいフローを組む
        # ときに、計画タスクの側のエスカレーションがもう閉じている（フローを置き換えられる）ように
        events: list[Event] = [
            self._close(e, "再計画を頼んだ")
            for e in self.escalations.values()
            if e.task == planning_task
        ]
        events.append(
            ReplanRequested(
                command.reason,
                command.trigger,
                command.answer,
                settled_before=self.settled_once,
                closes_on_apply=closes_on_apply,
            )
        )
        return events

    # --- タスクの一覧を直す ---

    @handles(InsertTask)
    def _insert(self, command: InsertTask) -> list[Event]:
        self._require_open()
        if not self.planned_once:
            # 初めての計画の提案は差し込む前の一覧から書くので、同じ番号の別のタスクを書く。待つ相手の
            # タスクもまだ無い。計画を反映してから差し込む
            raise Rejected("まだ一度も計画を反映していない。差し込むのは計画を反映した後")
        responds = self._closable(command.responds_to)
        number = max(self.used_numbers, default=0) + 1
        new = TaskId.numbered(number)
        blocked = frozenset(self.successor(dependency) for dependency in command.blocked_by)
        graph = {
            entry.id: entry.blocked_by
            for entry in self.tasks.values()
            if entry.is_implementation and not entry.status.is_terminal
        }
        taken = self.tasks.get(command.takes_over) if command.takes_over is not None else None
        conflicts = taken.conflicts if taken is not None else ()
        events: list[Event] = [
            TaskInserted(new, command.spec, blocked, command.takes_over, conflicts)
        ]
        if command.takes_over is not None:
            old = self._entry(command.takes_over)
            if old.status is _S.STACKED:
                raise Rejected(f"{old.id} はもう積まれていて、引き継げない")
            if old.status is not _S.STACKING or not old.integration_failed:
                state = "統合に失敗していない" if old.status is _S.STACKING else old.status.value
                raise Rejected(
                    f"引き継げるのは、積んでいる途中で統合に失敗したタスクだけ（{old.id} は {state}）"
                )
            if old.id in blocked:
                raise Rejected(f"引き継ぐタスクは、引き継ぎ元の {old.id} を待てない")
            graph.pop(old.id, None)
            graph = {
                task: frozenset(new if d == old.id else d for d in dependencies)
                for task, dependencies in graph.items()
            }
            events.append(TaskSuperseded(old.id, new))
        graph[new] = blocked
        self._check_graph(graph)
        return self._close_responded(events, responds, "タスクを差し込んだ")

    @handles(StopTasks)
    def _stop_tasks(self, command: StopTasks) -> list[Event]:
        self._require_open()
        responds = self._closable(command.responds_to)
        events = self._stop(command.tasks, check_waiting=True)
        events = self._close_responded(events, responds, "タスクを止めた")
        return events + self._settles(events)

    def _stop(self, tasks: frozenset[TaskId], *, check_waiting: bool) -> list[Event]:
        if not tasks:
            raise Rejected("止めるタスクが無い")
        for task in sorted(tasks, key=str):
            entry = self._entry(task)
            if not entry.is_implementation:
                raise Rejected(f"止められるのは実装タスクだけ（{task}）")
            if entry.status is _S.STACKED:
                raise Rejected(f"{task} は積み済みで、止められない（外すなら破棄する）")
            if entry.status not in _STOPPABLE:
                raise Rejected(f"{task} は {entry.status.value} で、書き換えない")
        if check_waiting:
            # 止めたタスクを待つタスクは、積まれることが無くなり、ランが終わらない
            for entry in self.tasks.values():
                waiting = entry.blocked_by & tasks
                if (
                    entry.id not in tasks
                    and entry.is_implementation
                    and not entry.status.is_terminal
                    and waiting
                ):
                    names = ", ".join(sorted(str(t) for t in waiting))
                    raise Rejected(
                        f"{entry.id} が止める {names} を待っている（一緒に止めるか、再計画する）"
                    )
        events: list[Event] = [TasksStopped(tasks)]
        events += [
            self._close(e, "タスクを止めた") for e in self.escalations.values() if e.task in tasks
        ]
        return events

    def _discard(self, tasks: frozenset[TaskId], proposal: Proposal) -> list[Event]:
        if not tasks:
            raise Rejected("破棄するタスクが無い")
        for task in sorted(tasks, key=str):
            entry = self._entry(task)
            if entry.status is not _S.STACKED:
                raise Rejected(f"破棄できるのは積んだタスクだけ（{task} は {entry.status.value}）")
            if task not in proposal.discard:
                raise Rejected(f"確定した再計画は {task} の破棄を提案していない")
        # 破棄した中で一番下より上に積んであって、破棄しないもの。前の破棄でまだ戻していないものも
        # 含むが、今度の破棄で破棄するものは除く（戻すと、破棄したタスクを積み直してしまう）
        cut = min(self.stack_order.index(task) for task in tasks)
        above = {t for t in self.stack_order[cut + 1 :]} | set(self.requeue)
        requeue = tuple(t for t in self.stack_order if t in above - tasks)
        return [TasksDiscarded(tasks, requeue)]

    @handles(ReturnToQueue)
    def _return_to_queue(self, command: ReturnToQueue) -> list[Event]:
        """閉じ終えた。戻すのを待っているタスクを、新しいブランチで積む列に戻す（無ければ空で出す）。"""
        self._require_open()
        if not self.cuts_pending:
            raise Rejected("破棄して閉じるのを待っているものが無い")
        branches = tuple(
            self._branch(self.tasks[task], self.tasks[task].branch_round + 1)
            for task in self.requeue
        )
        events: list[Event] = [TasksReturnedToQueue(self.requeue, branches)]
        return events + self._settles(events)

    @handles(MarkStacked)
    def _mark_stacked(self, command: MarkStacked) -> list[Event]:
        self._require_open()
        entry = self._entry(command.task)
        if entry.status is not _S.STACKING:
            raise Rejected(f"{command.task} は {entry.status.value} で、積んでいる途中ではない")
        # 破棄した後、閉じる前に積み終えたタスクは、閉じる所より上に載ったので、一緒に閉じて
        # 積み直す（Stack も閉じる中に入れる）
        requeue = bool(self.cuts_pending)
        events: list[Event] = [TaskMarkedStacked(command.task, command.pr, requeue=requeue)]
        return events + self._settles(events)

    @handles(RecordIntegrationFailure)
    def _record_integration_failure(self, command: RecordIntegrationFailure) -> list[Event]:
        self._require_open()
        entry = self._entry(command.task)
        if entry.status is not _S.STACKING:
            # 止めたタスクの、遅れて届いた知らせ
            return []
        return [IntegrationFailureRecorded(command.task, command.reason, command.files)]

    @handles(ClearIntegrationFailure)
    def _clear_integration_failure(self, command: ClearIntegrationFailure) -> list[Event]:
        """統合の失敗の印は、その失敗のエスカレーションが開いている間だけ立てる。やり直すなら下ろす。"""
        self._require_open()
        entry = self._entry(command.task)
        return [IntegrationFailureCleared(command.task)] if entry.integration_failed else []

    # --- エスカレーションと回答 ---

    @handles(EscalateToRun)
    def _escalate(self, command: EscalateToRun) -> list[Event]:
        self._require_escalation_route(command.task)
        entry = self._entry(command.task)
        if entry.status.is_terminal:
            raise Rejected(f"{command.task} は {entry.status.value} で、もう上げない")
        if reason := EscalationRouter.why_not_relay(command.task.kind, command.kind):
            raise Rejected(reason)
        if not command.reason.strip():
            raise Rejected("上げる理由が空")
        if entry.status is _S.ESCALATED and command.source is None:
            # タスクの側に待っているエスカレーションがあるのに上げ元を書かないと、ラン統括の回答が
            # そこへ下りず（notes に残るだけ）、タスクは止まったままになる
            raise Rejected(
                f"{command.task} はエスカレーションの回答を待っている。それに応じて上げるなら source を書く"
            )
        return [
            EscalationRaised(
                command.kind,
                command.pointers,
                command.hint,
                task=command.task,
                source=command.source,
                reason=command.reason,
                question=command.question,
                answer_only=command.answer_only,
            )
        ]

    @handles(ReportSupervisorFailure)
    def _report_supervisor_failure(self, command: ReportSupervisorFailure) -> list[Event]:
        """統括が応じなかった知らせを、その統括の 1 段上へ上げる（段を飛ばさない）。

        タスク統括ならラン統括が、ラン統括なら `/autodev`（質問。ポリシーが PostQuestion）が受ける
        （EscalationRouter.route）。受けた側が答えたら、応じなかった統括を新しいセッションで、同じ
        知らせで起こし直す（EscalationAnswered・AnswerRecorded の failed_notice）。

        ランを終えた後に受けてよいかは、知らせが関わるタスクで決める。ラン統括が git 管理タスクの
        エスカレーションに応じなかったなら、git 管理タスクのものとして受ける。
        """
        task = command.supervisor
        concerned = task if task is not None else self.notice_tasks.get(command.notice)
        self._require_escalation_route(concerned)
        if not command.reason.strip():
            raise Rejected("応じなかった理由が空")
        if task is not None:
            entry = self._entry(task)
            if not entry.is_implementation:
                raise Rejected(f"LLM の統括を持つのは実装タスクだけ: {task}")
            if entry.status.is_terminal:
                # 終わったタスクの統括は、もう起こし直さない
                return []
        if self.reported_failure(command.notice):
            return []
        return [
            EscalationRaised(
                EscalationKind.SUPERVISOR_FAILED,
                Pointers(),
                task=task,
                reason=command.reason,
                failed_notice=command.notice,
                # 起こし直しの知らせにまた応じなかったら、前の上げから数え続ける
                failures=self.retry_failures.get(command.notice, 0) + 1,
            )
        ]

    @handles(CloseRelayedEscalation)
    def _close_relayed(self, command: CloseRelayedEscalation) -> list[Event]:
        if self.name is None:
            raise Rejected("ランが始まっていない")
        # タスクの側はもう閉じたので、タスクの側を閉じる source は載せない
        return [
            EscalationClosed(escalation.id, command.reason, escalation.task)
            for escalation in self.escalations.values()
            if escalation.source == command.source
        ]

    @handles(RecordAnswer)
    def _record_answer(self, command: RecordAnswer) -> list[Event]:
        asked = self.escalations.get(command.escalation) if command.escalation else None
        self._require_escalation_route(self.notice_tasks.get(asked.id) if asked else None)
        if command.question in self.answers:
            raise Rejected(f"{command.question} の回答はもう受け取った")
        if asked is not None and asked.for_user:
            # ユーザーが直に受けた上げ（ラン統括が応じなかった）は、回答で閉じる。ラン統括は、
            # 回答を添えて同じ知らせで起こし直す
            return [
                AnswerRecorded(
                    command.question, command.answer, asked.id, failed_notice=asked.failed_notice
                ),
                EscalationClosed(asked.id, "ユーザーが答えた", asked.task),
            ]
        # 閉じたエスカレーションへの回答も記録する（ユーザーの言葉を回答の記録に残す）。拒むと、
        # 質問は answered のまま Run に届かない。開いたことの無い id（取り違え）には印を付けない。
        # 付けるとラン統括が起きず、取り違えた側で開いているエスカレーションを起こすものが無くなる
        return [
            AnswerRecorded(
                command.question,
                command.answer,
                command.escalation,
                escalation_closed=asked is None and command.escalation in self.opened_escalations,
            )
        ]

    @handles(AnswerEscalation)
    def _answer(self, command: AnswerEscalation) -> list[Event]:
        escalation = self._open_escalation(command.escalation)
        assert escalation is not None
        self._require_escalation_route(escalation.task)
        if (command.answer is None) == (command.question is None):
            raise Rejected(
                "自分で答えるなら answer、ユーザーの回答を渡すなら question の、どちらか一方を書く"
            )
        if command.question is not None:
            text = self._usable_answer(command.question, escalation.id).answer
        else:
            assert command.answer is not None
            if escalation.requires_user_answer:
                raise Rejected(
                    f"{escalation.task} のタスク統括は同じ知らせに {escalation.failures} 回続けて"
                    f"応じなかった（上限 {MAX_SUPERVISOR_FAILURES}）。ラン統括の答えでは直らないので、"
                    f"ask-user でユーザーに聞いてから（escalation に {escalation.id} を書く）、その回答の"
                    "QuestionId を question に添えて答え直す。stop-tasks・replan で応えてもよい"
                )
            text = command.answer
            if not text.strip():
                raise Rejected("回答が空")
        return [
            EscalationAnswered(
                escalation.id,
                escalation.task,
                escalation.source,
                text,
                command.question,
                failed_notice=escalation.failed_notice,
            ),
            # 回答は EscalationAnswered で下ろすので、タスクの側を閉じる source は載せない
            EscalationClosed(escalation.id, "ラン統括が答えた", escalation.task),
        ]

    # --- 状態の知らせ ---

    @handles(UpdateTaskStatus)
    def _update_status(self, command: UpdateTaskStatus) -> list[Event]:
        if self.name is None:
            raise Rejected("ランが始まっていない")
        entry = self._entry(command.task)
        # 終端のタスクは、ほかの集約から遅れて届いた知らせでは動かさない（止めた後に閉じた
        # エスカレーションの知らせなど）。同じ状態への知らせも、何もしない
        if entry.status.is_terminal or entry.status is command.to_status:
            return []
        transition = (entry.status, command.to_status)
        if transition not in REPORTED_TRANSITIONS[command.task.kind]:
            raise Rejected(
                f"{command.task} を {entry.status.value} から {command.to_status.value} へは動かせない"
            )
        if command.to_status is _S.FINISHED and not self.finished:
            raise Rejected(f"{command.task} が終わるのは、ランが終わった後")
        return [TaskStatusChanged(command.task, entry.status, command.to_status, command.cause)]

    # --- パニックと終わり ---

    @handles(Panic)
    def _panic(self, command: Panic) -> list[Event]:
        """利用枠の上限に当たった。ランを終えた後も、走るタスク（仕上げの並びを走らせる git 管理タスク）
        が残っている間は受ける。その上げに応じるラン統括が当たることがあり、拒むと統括を二度と起こさない。"""
        if self.name is None:
            raise Rejected("ランが始まっていない")
        if self.finished and not self.live_tasks:
            raise Rejected("ランはもう終わっている")
        if self.panicked:
            raise Rejected("もうパニックしている")
        return [RunPanicked(command.cause)]

    @handles(ResumeRun)
    def _resume(self, command: ResumeRun) -> list[Event]:
        """呼び直された。パニックの後も、落ちた後も、止まった実行の再開をタスクに頼む。

        ランが終わった後も、git 管理タスクが仕上げの並びを終えていなければ受ける。
        """
        if self.name is None:
            raise Rejected("ランが始まっていない")
        live = self.live_tasks
        if self.finished and not live:
            raise Rejected("ランはもう終わっている")
        return [RunResumed(live, after_panic=self.panicked)]

    @handles(FinishRun)
    def _finish(self, command: FinishRun) -> list[Event]:
        self._require_open()
        if self.planning:
            raise Rejected("計画が進んでいる")
        if self.cuts_pending or self.requeue:
            raise Rejected("破棄したタスクの上を閉じて、積む列へ戻し終えていない")
        # 計画タスクと git 管理タスクはランが終わるまで続くので、見ない
        unsettled = sorted(
            str(entry.id)
            for entry in self.tasks.values()
            if entry.is_implementation and not entry.status.is_terminal
        )
        if unsettled:
            raise Rejected(f"終端でない実装タスクがある: {', '.join(unsettled)}")
        return [RunFinished(command.ready_overview)]

    # --- apply ---

    @applies(RunStarted)
    def _on_run_started(self, event: RunStarted) -> None:
        self.name = event.name
        self.limit = event.limit
        self.models = event.models or ModelClasses.default()
        self.planning = True

    @applies(TaskStarted)
    def _on_task_started(self, event: TaskStarted) -> None:
        entry = self.tasks.get(event.task) or TaskEntry(
            event.task, _S.PENDING, event.spec, event.blocked_by
        )
        self.tasks[event.task] = replace(entry, status=_S.RUNNING, started=True)

    @applies(SettledPlanRecorded)
    def _on_settled(self, event: SettledPlanRecorded) -> None:
        self.settled = event
        self.settled_once = True

    @applies(TasksPlanned)
    def _on_planned(self, event: TasksPlanned) -> None:
        for task in event.tasks:
            if (entry := self.tasks.get(task.id)) is not None:
                self.tasks[task.id] = replace(entry, spec=task.spec, blocked_by=task.blocked_by)
            else:
                self.tasks[task.id] = TaskEntry(task.id, _S.PENDING, task.spec, task.blocked_by)
                self.used_numbers.add(_number(task.id))
        for task in event.withdraw:
            # 積む列から外し、始め直すときは新しいブランチで切る（push したかもしれない名前は使わない）
            entry = self.tasks[task]
            self.tasks[task] = replace(
                entry, status=_S.PENDING, branch_round=entry.branch_round + 1
            )
        self.planning = False
        self.planned_once = True
        self.replan_triggers = ()
        self.settled = None
        self.inserted_while_planning = set()
        self.artifacts = event.artifacts
        self.quick_checks = event.quick_checks
        self.regression_tests = event.regression_tests

    @applies(ReplanRequested)
    def _on_replan(self, event: ReplanRequested) -> None:
        self.planning = True
        self.settled = None
        self.replan_triggers = event.closes_on_apply
        # 今度の再計画は、今のタスクの一覧（差し込んだタスクも含む）から書くので、ぶつからない
        self.inserted_while_planning = set()
        if event.answer is not None:
            self.answers[event.answer] = replace(self.answers[event.answer], used=True)
            self.replan_streak = 0
        else:
            self.replan_streak += 1

    @applies(TaskInserted)
    def _on_inserted(self, event: TaskInserted) -> None:
        self.tasks[event.task] = TaskEntry(
            event.task,
            _S.PENDING,
            event.spec,
            event.blocked_by,
            takes_over=event.takes_over,
            conflicts=event.conflicts,
        )
        self.used_numbers.add(_number(event.task))
        if self.planning:
            self.inserted_while_planning.add(event.task)

    @applies(TaskSuperseded)
    def _on_superseded(self, event: TaskSuperseded) -> None:
        old = self.tasks[event.task]
        self.tasks[event.task] = replace(old, status=_S.SUPERSEDED, superseded_by=event.by)
        # 引き継ぎ元に依存していたタスクは、引き継ぎ先に依存し直す
        for entry in list(self.tasks.values()):
            if event.task in entry.blocked_by and entry.id != event.by:
                blocked = (entry.blocked_by - {event.task}) | {event.by}
                self.tasks[entry.id] = replace(entry, blocked_by=blocked)

    @applies(TasksStopped)
    def _on_stopped(self, event: TasksStopped) -> None:
        for task in event.tasks:
            self.tasks[task] = replace(self.tasks[task], status=_S.DROPPED)

    @applies(TasksDiscarded)
    def _on_discarded(self, event: TasksDiscarded) -> None:
        for task in event.tasks:
            self.tasks[task] = replace(self.tasks[task], status=_S.DISCARDED)
        self.stack_order = [t for t in self.stack_order if t not in event.tasks]
        self.requeue = event.requeue
        self.cuts_pending += 1

    @applies(TasksReturnedToQueue)
    def _on_returned(self, event: TasksReturnedToQueue) -> None:
        for task in event.tasks:
            entry = self.tasks[task]
            self.tasks[task] = replace(entry, status=_S.GATED, branch_round=entry.branch_round + 1)
        returned = set(event.tasks)
        self.stack_order = [t for t in self.stack_order if t not in returned]
        self.requeue = ()
        self.cuts_pending = max(0, self.cuts_pending - 1)

    @applies(TaskMarkedStacked)
    def _on_stacked(self, event: TaskMarkedStacked) -> None:
        self.tasks[event.task] = replace(self.tasks[event.task], status=_S.STACKED)
        self.stack_order.append(event.task)
        self.replan_streak = 0
        if event.requeue:
            self.requeue = (*self.requeue, event.task)

    @applies(IntegrationFailureRecorded)
    def _on_integration_failed(self, event: IntegrationFailureRecorded) -> None:
        self.tasks[event.task] = replace(
            self.tasks[event.task], conflicts=event.files, integration_failed=True
        )

    @applies(IntegrationFailureCleared)
    def _on_integration_cleared(self, event: IntegrationFailureCleared) -> None:
        self.tasks[event.task] = replace(self.tasks[event.task], integration_failed=False)

    @applies(EscalationRaised)
    def _on_escalated(self, event: EscalationRaised) -> None:
        self.escalations[self.event_id] = RunEscalation(
            self.event_id,
            event.kind,
            event.task,
            event.source,
            event.failed_notice,
            event.failures,
            event.answer_only,
            event.reason,
            event.question,
        )
        self.opened_escalations.add(self.event_id)
        # ラン統括が応じなかった上げは、応じなかった知らせのタスクに関わる
        concerned = event.task
        if concerned is None and event.failed_notice is not None:
            concerned = self.notice_tasks.get(event.failed_notice)
        if concerned is not None:
            self.notice_tasks[self.event_id] = concerned
        if event.failed_notice is not None:
            self.failed_notices.add(event.failed_notice)

    @applies(EscalationClosed)
    def _on_escalation_closed(self, event: EscalationClosed) -> None:
        self.escalations.pop(event.escalation, None)

    def _note_retry(self, escalation: EventId) -> None:
        """当てているイベントが supervisor-failed への回答なら、続けて応じなかった数を覚える。"""
        answered = self.escalations.get(escalation)
        if answered is not None and answered.failed_notice is not None:
            self.retry_failures[self.event_id] = answered.failures

    @applies(EscalationAnswered)
    def _on_answered(self, event: EscalationAnswered) -> None:
        if event.question is not None:
            self.answers[event.question] = replace(self.answers[event.question], used=True)
        self._note_retry(event.escalation)

    @applies(AnswerRecorded)
    def _on_answer_recorded(self, event: AnswerRecorded) -> None:
        self.answers[event.question] = RecordedAnswer(event.answer, event.escalation)
        if event.escalation is not None:
            if (concerned := self.notice_tasks.get(event.escalation)) is not None:
                self.notice_tasks[self.event_id] = concerned
            self._note_retry(event.escalation)

    @applies(TaskStatusChanged)
    def _on_status(self, event: TaskStatusChanged) -> None:
        self.tasks[event.task] = replace(self.tasks[event.task], status=event.to_status)

    @applies(AllTasksSettled)
    def _on_settled_all(self, event: AllTasksSettled) -> None:
        pass

    @applies(RunPanicked)
    def _on_panicked(self, event: RunPanicked) -> None:
        self.panic_cause = event.cause

    @applies(RunResumed)
    def _on_resumed(self, event: RunResumed) -> None:
        self.panic_cause = None
        self.resumes += 1

    @applies(RunFinished)
    def _on_finished(self, event: RunFinished) -> None:
        self.finished = True
        planning = TaskId.planning()
        if (entry := self.tasks.get(planning)) is not None and not entry.status.is_terminal:
            self.tasks[planning] = replace(entry, status=_S.FINISHED)


def _number(task: TaskId) -> int:
    number = task.number
    assert number is not None, f"{task} は実装タスクではない"
    return number


def _find_cycle(graph: Mapping[TaskId, Iterable[TaskId]]) -> list[TaskId] | None:
    """依存のグラフの輪を 1 つ。無ければ None。"""
    done: set[TaskId] = set()
    for start in sorted(graph, key=str):
        if start in done:
            continue
        path: list[TaskId] = [start]
        on_path: set[TaskId] = {start}
        stack: list[tuple[TaskId, list[TaskId]]] = [(start, sorted(graph.get(start, ()), key=str))]
        while stack:
            node, rest = stack[-1]
            if not rest:
                stack.pop()
                path.pop()
                on_path.discard(node)
                done.add(node)
                continue
            child = rest.pop(0)
            if child in on_path:
                return [*path[path.index(child) :], child]
            if child in done or child not in graph:
                continue
            stack.append((child, sorted(graph.get(child, ()), key=str)))
            path.append(child)
            on_path.add(child)
    return None
