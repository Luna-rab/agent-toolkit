"""Task 集約が受けるコマンド。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..flow.flow import FlowStep
from ..value_objects.artifact_ref import ArtifactRef
from ..value_objects.branch_name import BranchName
from ..value_objects.commit_sha import CommitSha
from ..value_objects.decision import Decision
from ..value_objects.design_version import DesignVersion
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.event_id import EventId
from ..value_objects.evidence import Evidence
from ..value_objects.execution_id import ExecutionId
from ..value_objects.finding_id import FindingId
from ..value_objects.finding_route import FindingRoute
from ..value_objects.git_job import GitJob
from ..value_objects.hint import Hint
from ..value_objects.interrupt_cause import InterruptCause
from ..value_objects.issuer_kind import IssuerKind
from ..value_objects.pointers import Pointers
from ..value_objects.question_id import QuestionId
from ..value_objects.session_id import SessionId
from ..value_objects.stall_cause import StallCause
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId
from ..value_objects.task_kind import TaskKind
from ..value_objects.task_spec import TaskSpec
from .base import Command

_K = IssuerKind


@dataclass(frozen=True, kw_only=True)
class TaskCommand(Command):
    AGGREGATE: ClassVar[str] = "Task"
    task: TaskId

    @property
    def target(self) -> StreamId:
        return StreamId.task(self.task)

    @property
    def supervised_task(self) -> TaskId | None:
        return self.task


@dataclass(frozen=True, kw_only=True)
class AcceptFlow(TaskCommand):
    """統括が返したフロー。版の番号は、受け入れるときに Task が付ける。"""

    ISSUERS = frozenset({_K.TASK_SUPERVISOR})
    steps: tuple[FlowStep, ...]
    #: 上がってきたエスカレーションへの応答なら、そのエスカレーション
    responds_to: EventId | None = None
    #: git 管理タスクの統括（プログラム）が、取り出した仕事（GitJobTaken.job）を写す。ほかの統括は書かない
    job: GitJob | None = None


@dataclass(frozen=True, kw_only=True)
class OpenTask(TaskCommand):
    """Task のストリームの最初のコマンド。TaskStarted（Run）を受けたポリシーが出す。"""

    ISSUERS = frozenset({_K.POLICY})
    kind: TaskKind
    #: 実装タスクだけが持つ
    spec: TaskSpec | None = None
    #: そのときのラン共通の成果物（brief・codemap・design の版）
    artifacts: tuple[ArtifactRef, ...] = ()
    blocked_by: frozenset[TaskId] = frozenset()
    #: 実装タスクのブランチ（TaskStarted から写す）
    branch: BranchName | None = None
    #: 引き継いで解き直す衝突したファイル（TaskStarted から写す）
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class BeginStage(TaskCommand):
    """走らせると決めたステージを始める（2 段目）。StageRequested を受けた実行器が出す。

    HEAD とセッション id はドメインの外の値なので、実行器が集めて載せる。
    まだ始まっていない StageRequested の実行と合わなければ、Task が拒む。
    """

    ISSUERS = frozenset({_K.EXECUTOR})
    execution: ExecutionId
    head: CommitSha
    #: LLM のステージのセッション。決定的なステージには無いので None を明示して渡す
    session: SessionId | None


@dataclass(frozen=True, kw_only=True)
class ReportBeginFailure(TaskCommand):
    """走らせると決めたステージを、実行器が始められなかった（worktree を戻せない・HEAD が取れない・
    止めた実行が同じ worktree で終わらない）。BeginStage の代わりに出す。

    やり直すか上げるかは、走らせて落ちたときと同じ規則で Task が決める。出さないと、実行は requested
    のまま誰も進めない。
    """

    ISSUERS = frozenset({_K.EXECUTOR})
    execution: ExecutionId
    error: str


@dataclass(frozen=True, kw_only=True)
class ReportStageResult(TaskCommand):
    """実行器が集めた証拠。完了・失敗・エスカレーションのどれにするかは Task が決める。"""

    ISSUERS = frozenset({_K.EXECUTOR})
    execution: ExecutionId
    evidence: Evidence
    pointers: Pointers
    #: ステージが返した結果の JSON（形を確かめられなかったら None）
    result: dict[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class InterruptStage(TaskCommand):
    """ポリシーが走っている実行を止める。理由はパニックか、ポリシーの判断（requested）。"""

    ISSUERS = frozenset({_K.POLICY})
    execution: ExecutionId
    cause: InterruptCause = InterruptCause.REQUESTED


@dataclass(frozen=True, kw_only=True)
class MarkInterrupted(TaskCommand):
    """driver が、running のまま残っていた実行を interrupted にする。

    起動時の後始末なら startup、パニックで止めるときなら panic。どれが running かは Task に聞く
    （`Task.running_executions`）。
    """

    ISSUERS = frozenset({_K.DRIVER})
    execution: ExecutionId
    cause: InterruptCause = InterruptCause.STARTUP


@dataclass(frozen=True, kw_only=True)
class ResumeInterrupted(TaskCommand):
    """呼び直された（RunResumed）。止めた理由が再開を求める実行を、続きから再開する。

    どれを再開するかは Task が決める（InterruptCause.resumes_on_restart・書き直す前のフローの実行は
    再開しない）。再開するものが無ければ何もしない。
    """

    ISSUERS = frozenset({_K.POLICY})


@dataclass(frozen=True, kw_only=True)
class ConfirmHandoff(TaskCommand):
    """結果を渡した先の答え（ResultReceived・ResultRefused を受けて）。

    受けたなら cursor を進め、受けなかったなら `refused` に理由を書き、Task が result-refused で上げる。
    判定の役（Judge・DesignJudge）の結果を受けたことは Conclude*Round が知らせるので、受けなかった
    ときだけ届く。
    """

    ISSUERS = frozenset({_K.POLICY})
    #: 結果を返したステージの実行（ResultReceived・ResultRefused の source）
    execution: ExecutionId
    refused: str | None = None


@dataclass(frozen=True, kw_only=True)
class AbandonFlow(TaskCommand):
    """今のフローを捨てる（処理中の git 管理タスクの仕事を外した GitJobWithdrawn を受けて）。

    走っている実行を止め、統括の次のフローを待つ。
    """

    ISSUERS = frozenset({_K.POLICY})
    reason: str


@dataclass(frozen=True, kw_only=True)
class ResumeStage(TaskCommand):
    ISSUERS = frozenset({_K.CLI, _K.REACTION, _K.DRIVER})
    execution: ExecutionId


@dataclass(frozen=True, kw_only=True)
class Escalate(TaskCommand):
    ISSUERS = frozenset({_K.POLICY})
    kind: EscalationKind
    pointers: Pointers
    hint: Hint = field(default_factory=Hint)
    origin: ExecutionId | None = None
    #: 上げる理由。書かなければ、Task が判定した実行（origin）の結果から読む
    reason: str = ""
    #: 答え以外では閉じられない（EscalationRaised.answer_only）
    answer_only: bool = False


@dataclass(frozen=True, kw_only=True)
class ResolveEscalation(TaskCommand):
    """回答をタスクへ下ろす。Run の EscalationAnswered を受けたポリシーが出す。

    ラン統括の `answer` は Run が受け（AnswerEscalation）、経路と QuestionId を確かめてから下ろす。
    """

    ISSUERS = frozenset({_K.POLICY})
    escalation: EventId
    answer: str
    #: ユーザーの回答を渡すなら、その質問。無ければラン統括の回答
    question: QuestionId | None = None


@dataclass(frozen=True, kw_only=True)
class CloseEscalation(TaskCommand):
    """回答以外で閉じる。もう閉じていれば何もしない（Run の EscalationClosed を受けたポリシーは、
    タスクの側でもう閉じたかを知らずに出す）。"""

    ISSUERS = frozenset({_K.POLICY})
    escalation: EventId
    reason: str


@dataclass(frozen=True, kw_only=True)
class ChangeScope(TaskCommand):
    """実装タスクにだけ送る。"""

    ISSUERS = frozenset({_K.POLICY})
    spec: TaskSpec
    #: そのときのラン共通の成果物（design の版など）
    artifacts: tuple[ArtifactRef, ...]
    pointers: Pointers
    #: 積む列から外して始め直すなら、新しいブランチ（TaskStarted の reopened から写す）
    branch: BranchName | None = None


@dataclass(frozen=True, kw_only=True)
class AddNote(TaskCommand):
    ISSUERS = frozenset({_K.POLICY})
    decision: Decision


@dataclass(frozen=True, kw_only=True)
class RecordBase(TaskCommand):
    """タスクのブランチの根元を覚えさせる（WorktreeReady・BranchRebased を受けたポリシーが出す）。"""

    ISSUERS = frozenset({_K.POLICY})
    base: CommitSha
    #: そのタスクのために切った worktree。根元だけが動いた（Rebase）なら None
    tree: str | None = None


@dataclass(frozen=True, kw_only=True)
class ConcludeReviewRound(TaskCommand):
    """ReviewLoop の Judge の判定を台帳に当て終えた。台帳の結果を Task に渡す。

    指摘の台帳の FindingsEvaluated（RecordJudgement が出す）から組む。次の一手（Fix・抜ける・停滞で
    上げる）は Task が決める。Judge の結果を台帳が受けたことの知らせも兼ねる。
    """

    ISSUERS = frozenset({_K.POLICY})
    #: 判定を返した Judge の実行（FindingsEvaluated.execution）
    judge: ExecutionId
    #: 判定の後も open の指摘（FindingsEvaluated.open_findings）
    unresolved: tuple[FindingId, ...]
    #: そのうち停滞したもの（FindingsEvaluated.stalled）
    stalled: tuple[FindingId, ...] = ()
    #: ジャッジが停滞に付けた分類（FindingsEvaluated.stall_cause）
    cause: StallCause | None = None
    #: open の指摘の宛先（FindingsEvaluated.open_findings）。無い指摘は impl として扱う
    routes: tuple[FindingRoute, ...] = ()
    pointers: Pointers = field(default_factory=Pointers)


@dataclass(frozen=True, kw_only=True)
class ConcludeGateRound(TaskCommand):
    """Gate の不合格の後、G- の指摘を判定した台帳の結果を Task に渡す。

    指摘の台帳の FindingsEvaluated（RecordGateResult が出す）から組む。Fix へ戻るか、停滞で上げるかは
    Task が決める。Gate が通った後の結果なら、何もしない。
    """

    ISSUERS = frozenset({_K.POLICY})
    #: G- の指摘を判定した Gate の実行（FindingsEvaluated.execution）
    gate: ExecutionId
    unresolved: tuple[FindingId, ...]
    stalled: tuple[FindingId, ...] = ()
    routes: tuple[FindingRoute, ...] = ()
    pointers: Pointers = field(default_factory=Pointers)


@dataclass(frozen=True, kw_only=True)
class ConcludeDesignRound(TaskCommand):
    """DesignLoop の DesignJudge の後、Design が確定したか直すかを Task に渡す。

    `settled` があれば設計が確定した（DesignSettled）ので DesignLoop を抜ける。無ければ Revise の
    ラウンドを使った（DesignRevisionStarted）ので Revise へ進む。
    """

    ISSUERS = frozenset({_K.POLICY})
    #: そう決めた DesignJudge の実行（DesignSettled.execution・DesignRevisionStarted.execution）
    judge: ExecutionId
    settled: DesignVersion | None = None


@dataclass(frozen=True, kw_only=True)
class StopTask(TaskCommand):
    """Run が止めたタスク（TasksStopped）を、Task の側でも止める。ポリシーが出す。

    走っている実行を中断し、開いているエスカレーションを閉じ、以後ステージを始めない。
    """

    ISSUERS = frozenset({_K.POLICY})
    reason: str
