"""Run 集約が受けるコマンド。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from ..value_objects.artifact_ref import ArtifactRef
from ..value_objects.branch_name import BranchName
from ..value_objects.design_version import DesignVersion
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.event_id import EventId
from ..value_objects.hint import Hint
from ..value_objects.instruction import Instruction
from ..value_objects.issuer_kind import IssuerKind
from ..value_objects.model_class import ModelClasses
from ..value_objects.parallel_limit import ParallelLimit
from ..value_objects.pointers import Pointers
from ..value_objects.pr_number import PrNumber
from ..value_objects.proposal import Proposal
from ..value_objects.question_id import QuestionId
from ..value_objects.repository import Repository
from ..value_objects.run_name import RunName
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId
from ..value_objects.task_spec import TaskSpec
from ..value_objects.task_status import TaskStatus
from .base import Command

_K = IssuerKind


@dataclass(frozen=True, kw_only=True)
class RunCommand(Command):
    AGGREGATE: ClassVar[str] = "Run"

    @property
    def target(self) -> StreamId:
        return StreamId.run()


@dataclass(frozen=True, kw_only=True)
class StartRun(RunCommand):
    ISSUERS = frozenset({_K.CLI})
    name: RunName
    instruction: Instruction
    repository: Repository
    base: BranchName
    limit: ParallelLimit = field(default_factory=lambda: ParallelLimit(ParallelLimit.DEFAULT))
    #: クラスごとのモデルと effort。ランの間変わらない（RunStarted に記録する）
    models: ModelClasses = field(default_factory=ModelClasses.default)


@dataclass(frozen=True, kw_only=True)
class StartTask(RunCommand):
    ISSUERS = frozenset({_K.POLICY})
    task: TaskId


@dataclass(frozen=True, kw_only=True)
class ApplyPlan(RunCommand):
    """記録してある確定した提案（`RecordSettledPlan`）のうち、`design` の版のものを、初めて反映する。

    `SettledPlanRecorded.replan` が偽のときにポリシーが出す。再計画の反映は ApplyReplan。
    """

    ISSUERS = frozenset({_K.POLICY})
    design: DesignVersion


@dataclass(frozen=True, kw_only=True)
class ApplyReplan(RunCommand):
    """ラン統括の `apply-plan`。確定した再計画を、止める・破棄する・反映するまで 1 回で行う。

    StopTasks → DiscardTasks → ApplyPlan の 3 つに分けると、「前が拒まれたら後ろを出さない」という
    並びの知識がドメインの外に残り、前半だけが確定することもある。そこで 1 つのコマンドにし、Run がまとめて確かめて TasksStopped・
    TasksDiscarded・TasksPlanned を出す（空の一覧のイベントは出さない）。初回の反映は ApplyPlan。
    """

    ISSUERS = frozenset({_K.RUN_SUPERVISOR})
    design: DesignVersion
    #: 止める実装タスク。確定した提案の止める候補から選ぶ
    stop: frozenset[TaskId] = frozenset()
    #: 破棄する積んだタスク。確定した提案の破棄する候補から選ぶ
    discard: frozenset[TaskId] = frozenset()
    #: 応える Run のエスカレーション（再計画のきっかけ以外に、処理したら閉じるもの）
    responds_to: EventId | None = None


@dataclass(frozen=True, kw_only=True)
class RequestReplan(RunCommand):
    ISSUERS = frozenset({_K.RUN_SUPERVISOR})
    reason: str
    #: きっかけのエスカレーション（needs-replan や計画タスクの ask に応じるとき）
    trigger: EventId | None = None
    #: 再計画の上限に達した後だけ要る、ユーザーの回答
    answer: QuestionId | None = None


@dataclass(frozen=True, kw_only=True)
class StartReadyTasks(RunCommand):
    """始められる実装タスクを、TaskScheduler の選んだだけ始める。

    ポリシーは Run の状態を持たないので、どれを始めるかは Run が TaskScheduler に聞いて決める。
    ポリシーは、始められるタスクが増えうるイベント（TasksPlanned・TaskMarkedStacked など）を
    受けてこれを出すだけでよい。
    """

    ISSUERS = frozenset({_K.POLICY, _K.DRIVER})


@dataclass(frozen=True, kw_only=True)
class InsertTask(RunCommand):
    ISSUERS = frozenset({_K.RUN_SUPERVISOR})
    spec: TaskSpec
    blocked_by: frozenset[TaskId] = frozenset()
    takes_over: TaskId | None = None
    #: 応える Run のエスカレーション。処理したら閉じる
    responds_to: EventId | None = None


@dataclass(frozen=True, kw_only=True)
class StopTasks(RunCommand):
    ISSUERS = frozenset({_K.RUN_SUPERVISOR})
    tasks: frozenset[TaskId]
    #: 応える Run のエスカレーション。処理したら閉じる
    responds_to: EventId | None = None


@dataclass(frozen=True, kw_only=True)
class ReturnToQueue(RunCommand):
    """閉じ終えた（StackCutBack）。積む列に戻すタスクは Run が持っている一覧（TasksDiscarded.requeue）
    で、ポリシーは写さない。一覧の出どころを 1 つにするためである。"""

    ISSUERS = frozenset({_K.POLICY})


@dataclass(frozen=True, kw_only=True)
class RecordIntegrationFailure(RunCommand):
    """積んでいる途中のタスクの統合が失敗した（Stack の IntegrationFailed を受けて）。"""

    ISSUERS = frozenset({_K.POLICY})
    task: TaskId
    reason: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ClearIntegrationFailure(RunCommand):
    """統合をやり直すので、統合の失敗の印を下ろす（Stack の IntegrationRetried を受けて）。"""

    ISSUERS = frozenset({_K.POLICY})
    task: TaskId


@dataclass(frozen=True, kw_only=True)
class MarkStacked(RunCommand):
    ISSUERS = frozenset({_K.POLICY})
    task: TaskId
    pr: PrNumber


@dataclass(frozen=True, kw_only=True)
class EscalateToRun(RunCommand):
    ISSUERS = frozenset({_K.TASK_SUPERVISOR})
    #: 上げてきたタスク
    task: TaskId
    kind: EscalationKind
    #: 上げる理由（統括が書く。プログラムの統括は、タスクの側のエスカレーションの理由を写す）
    reason: str
    pointers: Pointers
    hint: Hint = field(default_factory=Hint)
    #: 上げる元になった、タスクの側のエスカレーション。ラン統括の回答はそこへ下りる
    source: EventId | None = None
    #: ユーザーに聞くとよいこと（DesignJudge が書いた問いなど。タスクの側のものを写す）
    question: str | None = None
    #: 答え以外では閉じられない（タスクの側のものを写す。EscalationRaised.answer_only）
    answer_only: bool = False

    @property
    def supervised_task(self) -> TaskId | None:
        return self.task


@dataclass(frozen=True, kw_only=True)
class CloseRelayedEscalation(RunCommand):
    """タスクの側で閉じたエスカレーション（`source`）を中継していた、Run の側のエスカレーションを閉じる。

    タスクの側の EscalationClosed を受けたポリシーが、中継があるかを知らずに出す。無ければ何もしない。
    """

    ISSUERS = frozenset({_K.POLICY})
    source: EventId
    reason: str


@dataclass(frozen=True, kw_only=True)
class AnswerEscalation(RunCommand):
    """ラン統括の `answer`。Run のエスカレーションに答え、上げてきたタスクへ回答を下ろす。

    自分で答えるなら `answer`、ユーザーの回答を渡すなら `question` のどちらか一方を書く。ユーザーの
    回答の本文は、ラン統括の判断からではなく、Run が受け取った回答（AnswerRecorded）から写す。
    """

    ISSUERS = frozenset({_K.RUN_SUPERVISOR})
    escalation: EventId
    answer: str | None = None
    question: QuestionId | None = None


@dataclass(frozen=True, kw_only=True)
class RecordAnswer(RunCommand):
    """ユーザーの回答を Run に渡す。QuestionAnswered を受けたポリシーが出す。"""

    ISSUERS = frozenset({_K.POLICY})
    question: QuestionId
    answer: str
    escalation: EventId | None = None


@dataclass(frozen=True, kw_only=True)
class Panic(RunCommand):
    ISSUERS = frozenset({_K.DRIVER})
    cause: str


@dataclass(frozen=True, kw_only=True)
class ReportSupervisorFailure(RunCommand):
    """LLM の統括が、差し戻しと呼び直しを使い切っても知らせに応じなかった（driver が出す）。

    どこへ上げるかは Run が EscalationRouter に聞いて決める（段を飛ばさない）。
    """

    ISSUERS = frozenset({_K.DRIVER})
    #: 応じなかったタスク統括のタスク。None ならラン統括
    supervisor: TaskId | None
    #: 応じられなかった知らせ（統括を起こしたイベント）
    notice: EventId
    reason: str


@dataclass(frozen=True, kw_only=True)
class FinishRun(RunCommand):
    ISSUERS = frozenset({_K.RUN_SUPERVISOR})
    ready_overview: bool


@dataclass(frozen=True, kw_only=True)
class UpdateTaskStatus(RunCommand):
    """Task・Stack のイベントを受けたポリシーが、Run の持つタスクの状態を動かす。"""

    ISSUERS = frozenset({_K.POLICY})
    task: TaskId
    to_status: TaskStatus
    #: きっかけのイベントの名前
    cause: str


@dataclass(frozen=True, kw_only=True)
class RecordSettledPlan(RunCommand):
    ISSUERS = frozenset({_K.POLICY})
    proposal: Proposal
    #: ラン共通の成果物（brief・codemap・design）
    artifacts: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ResumeRun(RunCommand):
    """既にあるラン名で呼び直された（パニックの後も、driver が落ちた後も）。"""

    ISSUERS = frozenset({_K.CLI, _K.DRIVER})
