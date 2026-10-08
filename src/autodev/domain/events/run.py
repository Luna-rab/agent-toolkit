"""Run 集約が出すイベント（ストリーム `run`）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..codec import RENAMED_FROM
from ..value_objects.artifact_ref import ArtifactRef
from ..value_objects.branch_name import BranchName
from ..value_objects.design_version import DesignVersion
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.event_id import EventId
from ..value_objects.execution_id import ExecutionId
from ..value_objects.finding_transfer import FindingTransfer
from ..value_objects.hint import Hint
from ..value_objects.instruction import Instruction
from ..value_objects.model_class import ModelClasses
from ..value_objects.parallel_limit import ParallelLimit
from ..value_objects.planned_task import PlannedTask
from ..value_objects.pointers import Pointers
from ..value_objects.pr_number import PrNumber
from ..value_objects.proposal import Proposal
from ..value_objects.question_id import QuestionId
from ..value_objects.repository import Repository
from ..value_objects.run_name import RunName
from ..value_objects.task_id import TaskId
from ..value_objects.task_kind import TaskKind
from ..value_objects.task_spec import TaskSpec
from ..value_objects.task_status import TaskStatus
from ..value_objects.verify_command import VerifyCommand
from .base import Event


@dataclass(frozen=True)
class RunStarted(Event):
    name: RunName
    instruction: Instruction
    repository: Repository
    base: BranchName
    limit: ParallelLimit
    #: クラスごとのモデルと effort。None は、この欄を記録する前に始めたラン（既定値で動かす）
    models: ModelClasses | None = None


@dataclass(frozen=True)
class TaskStarted(Event):
    """タスクを始めた。そのときのラン共通の成果物（brief・codemap・design の版）を載せる。"""

    task: TaskId
    kind: TaskKind
    #: 実装タスクだけが持つ
    spec: TaskSpec | None = None
    blocked_by: frozenset[TaskId] = frozenset()
    artifacts: tuple[ArtifactRef, ...] = ()
    #: 実装タスクのブランチ（`stack/<ラン名>--task-<番号>`）。名前の規約は Run が持つ
    branch: BranchName | None = None
    #: 一度始めて、積む列から外したタスクをもう一度始めた（範囲が変わった。TasksPlanned の
    #: withdraw）。ポリシーは OpenTask ではなく ChangeScope を出す
    reopened: bool = False
    #: 統合に失敗したタスクを引き継いだなら、解き直す衝突したファイル（ResolveConflict が書いてよい）
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class TasksPlanned(Event):
    """確定した提案を反映した。"""

    design: DesignVersion
    #: 反映した後の、終端でない実装タスク（提案に書かれていないタスクは今の中身のまま）と、
    #: 提案に書かれた積み済みのタスク
    tasks: tuple[PlannedTask, ...]
    #: ラン共通の回帰テスト（改名前の記録の `verify` はこれとして読む）
    regression_tests: tuple[VerifyCommand, ...] = field(metadata={RENAMED_FROM: ("verify",)})
    artifacts: tuple[ArtifactRef, ...]
    #: 再計画の反映か（初回なら概要 PR を作り、再計画なら本文を差し替える）
    replan: bool
    #: ラン共通の軽い検査。記録に無ければ空
    quick_checks: tuple[VerifyCommand, ...] = ()
    #: 再計画のきっかけになったエスカレーション（確定した提案を退けて頼み直したなら、前のきっかけも）。
    #: 一緒に出る EscalationClosed で閉じる
    triggers: tuple[EventId, ...] = ()
    #: 走っている・止まっているタスクのうち、範囲が変わったものと、きっかけのエスカレーションを
    #: 上げたタスク。ポリシーが ChangeScope を送る（ScopeChanged が待っているエスカレーションを閉じる）
    scope_changed: frozenset[TaskId] = frozenset()
    #: 積む列で待っていて範囲が変わったタスク。Run はまだ始めていない状態に戻し、TaskScheduler が
    #: 上限と依存を見て始め直す（TaskStarted の reopened）。ポリシーは積む頼みを外す（WithdrawRequest）
    withdraw: frozenset[TaskId] = frozenset()
    carry: tuple[FindingTransfer, ...] = ()
    #: 反映した後もまだ開いている Run のエスカレーション。ラン統括を起こし直す
    still_open: tuple[EventId, ...] = ()


@dataclass(frozen=True)
class ReplanRequested(Event):
    reason: str
    trigger: EventId | None = None
    answer: QuestionId | None = None
    #: 設計が一度でも確定していたか。まだなら Replan ではなく Prepare → Plan からやり直す
    settled_before: bool = True
    #: 反映したときに閉じる Run のエスカレーション。進んでいた再計画（確定した提案を退けた）の
    #: きっかけに、今度のきっかけを足したもの。計画タスクのエスカレーション（一緒に閉じる）は足さない
    closes_on_apply: tuple[EventId, ...] = ()


@dataclass(frozen=True)
class TaskInserted(Event):
    """ラン統括がタスクを差し込んだ。id は Run が使っていない番号から決める。"""

    task: TaskId
    spec: TaskSpec
    blocked_by: frozenset[TaskId] = frozenset()
    takes_over: TaskId | None = None
    #: 引き継ぎ元の統合で衝突したファイル（IntegrationFailureRecorded）。引き継ぎ先が解き直す
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskSuperseded(Event):
    task: TaskId
    by: TaskId


@dataclass(frozen=True)
class TasksStopped(Event):
    tasks: frozenset[TaskId]


@dataclass(frozen=True)
class TasksDiscarded(Event):
    tasks: frozenset[TaskId]
    #: 破棄した所より上に積んであって、破棄しないタスク（下から）。積む列に戻す（ReturnToQueue）
    requeue: tuple[TaskId, ...] = ()


@dataclass(frozen=True)
class TasksReturnedToQueue(Event):
    #: 積み直す順（下から）
    tasks: tuple[TaskId, ...]
    #: 積み直すときに切り直すブランチ（tasks と同じ順。`-r<積み直した回数>` が付く）
    branches: tuple[BranchName, ...] = ()


@dataclass(frozen=True)
class TaskMarkedStacked(Event):
    task: TaskId
    pr: PrNumber
    #: 破棄した所を閉じ終える前に積み終えたので、閉じる中に入り、閉じた後に積み直す
    requeue: bool = False


@dataclass(frozen=True)
class EscalationRaised(Event):
    """ループで解けない問題が起きた。Task と Run の両方が出す。

    このイベントの id（EventId）が、エスカレーションの id になる。
    """

    kind: EscalationKind
    pointers: Pointers
    hint: Hint = field(default_factory=Hint)
    #: 上げてきたタスク（Task が出すなら自分、Run が出すなら EscalateToRun を出した統括のタスク）
    task: TaskId | None = None
    #: どの実行で起きたか
    origin: ExecutionId | None = None
    #: Run が出すとき、上げる元になったタスクの側のエスカレーション（回答をそこへ渡す）
    source: EventId | None = None
    #: 上げる理由。Run が出すときは統括が書いた理由、Task が出すときは報告の中身（reportReason）
    #: や受け取る側が受けなかった理由。本文や出力そのものは載せない
    reason: str = ""
    #: ユーザーに聞くとよいこと（DesignJudge が書いた問い）。ラン統括が質問を出すときに使う
    question: str | None = None
    #: supervisor-failed なら、統括が応じられなかった知らせ（統括を起こしたイベント）。答えたら、
    #: その統括を新しいセッションで、この知らせで起こし直す
    failed_notice: EventId | None = None
    #: supervisor-failed なら、同じ知らせから起こし直しても続けて応じなかった数（最初の上げで 1）。
    #: 上限を超えたら、ラン統括はユーザーの回答を添えて答える（EscalationRouter.requires_user_answer）
    failures: int = 0
    #: 答え以外では閉じられない。ラン統括の答え以外の判断（insert-task・stop-tasks・apply-plan の
    #: respondsTo、replan の trigger）で閉じようとしたら Run が拒む。やめるとランが終わらない止めた
    #: git の仕事（`GitJobKind.can_drop` が偽）の上げで立てる
    answer_only: bool = False


@dataclass(frozen=True)
class TaskStatusChanged(Event):
    """Run が持つタスクの状態が変わった。`cause` はきっかけのイベントの名前。"""

    task: TaskId
    from_status: TaskStatus
    to_status: TaskStatus
    cause: str


@dataclass(frozen=True)
class AllTasksSettled(Event):
    """最後の実装タスクが終端になった（積んだ・止めた・引き継がれた・破棄した）。ラン統括を仕上げに起こす。"""


@dataclass(frozen=True)
class SettledPlanRecorded(Event):
    """確定した提案を Run が受け取った。DiscardTasks の検査と ApplyPlan に使う。"""

    proposal: Proposal
    #: ラン共通の成果物（brief・codemap・design）
    artifacts: tuple[ArtifactRef, ...] = ()
    #: 計画をすでに 1 回反映しているか。反映していなければポリシーが ApplyPlan を出し、
    #: していればラン統括を起こして apply-plan（ApplyReplan）を返させる
    replan: bool = False


@dataclass(frozen=True)
class AnswerRecorded(Event):
    """ユーザーの回答を Run が受け取った。回答を渡す・再計画の上限を外すのに、1 回だけ使える。"""

    question: QuestionId
    answer: str
    #: その質問を出したエスカレーション（Run の側）。回答はこのエスカレーションにだけ使える
    escalation: EventId | None = None
    #: ラン統括が応じられなかったので聞いた質問なら、その知らせ。ラン統括を新しいセッションで、
    #: この知らせで起こし直す（回答はそのエスカレーションを閉じる）
    failed_notice: EventId | None = None
    #: 届いたとき、質問を出したエスカレーションはもう回答以外で閉じていた（回答と stop-tasks・replan
    #: が前後した）。答える先が無いので、ラン統括を起こさない。起こすと answer を拒まれ続け、
    #: supervisor-failed から同じ知らせで起こし直す輪になる
    escalation_closed: bool = False


@dataclass(frozen=True)
class EscalationAnswered(Event):
    """ラン統括が Run のエスカレーションに答えた。回答は、上げてきたタスクへ下ろす（ポリシーが ResolveEscalation）。

    ユーザーの回答を渡したなら、本文は AnswerRecorded から写したもので、`question` を持つ。
    """

    escalation: EventId
    task: TaskId | None
    #: 回答を渡す先（タスクの側のエスカレーション）。無ければ、ポリシーは回答を AddNote で残す
    source: EventId | None
    answer: str
    question: QuestionId | None = None
    #: supervisor-failed への回答なら、タスク統括が応じられなかった知らせ。そのタスク統括を新しい
    #: セッションで、この知らせで起こし直す
    failed_notice: EventId | None = None


@dataclass(frozen=True)
class RunFinished(Event):
    ready_overview: bool


@dataclass(frozen=True)
class RunPanicked(Event):
    cause: str


@dataclass(frozen=True)
class RunResumed(Event):
    """既にあるラン名で呼び直された（パニックの後も、driver が落ちた後も）。

    ポリシーは `tasks` の各タスクに ResumeInterrupted を送り、どの実行を続きから再開するかは
    Task が止めた理由（InterruptCause）で決める。
    """

    #: 終端でないタスク（計画タスク・git 管理タスクを含む）
    tasks: tuple[TaskId, ...] = ()
    #: パニックの後か
    after_panic: bool = False


@dataclass(frozen=True)
class IntegrationFailureRecorded(Event):
    """積んでいる途中のタスクの統合が失敗した（Stack の IntegrationFailed を受けて）。

    衝突したファイルを覚え、そのタスクを引き継ぐタスクを差し込んだら（InsertTask の takes_over）、
    引き継ぎ先に渡す（TaskInserted.conflicts）。
    """

    task: TaskId
    reason: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegrationFailureCleared(Event):
    """統合の失敗に回答が届き、同じ仕事で統合をやり直す。もう引き継げない（ClearIntegrationFailure）。"""

    task: TaskId
