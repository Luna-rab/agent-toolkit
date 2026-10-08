"""Task 集約が出すイベント（ストリーム `task/<TaskId>`）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..flow.flow import Cursor, Flow, FlowStep
from ..stages.kinds import Handoff
from ..value_objects.artifact_kind import ArtifactKind
from ..value_objects.artifact_ref import ArtifactRef
from ..value_objects.branch_name import BranchName
from ..value_objects.commit_sha import CommitSha
from ..value_objects.decision import Decision
from ..value_objects.design_version import DesignVersion
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.event_id import EventId
from ..value_objects.execution_id import ExecutionId
from ..value_objects.gate_item_result import GateItemResult
from ..value_objects.git_job import GitJob
from ..value_objects.interrupt_cause import InterruptCause
from ..value_objects.pointers import Pointers
from ..value_objects.question_id import QuestionId
from ..value_objects.session_id import SessionId
from ..value_objects.stage_kind import StageKind
from ..value_objects.stage_result import StageResult
from ..value_objects.task_id import TaskId
from ..value_objects.task_kind import TaskKind
from ..value_objects.task_spec import TaskSpec
from .base import Event


@dataclass(frozen=True)
class FlowAccepted(Event):
    flow: Flow
    #: 上がってきたエスカレーションへの応答なら、そのエスカレーション（閉じる）
    closes: EventId | None = None
    #: 最初の位置（合成ステージで始まるなら、1 ラウンド目の最初の中のステージ）。Task が決める
    cursor: Cursor = field(default_factory=Cursor)


@dataclass(frozen=True)
class FlowRejected(Event):
    steps: tuple[FlowStep, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TaskOpened(Event):
    """Task のストリームの最初のイベント。種類・中身・そのときのラン共通の成果物を持つ。"""

    kind: TaskKind
    spec: TaskSpec | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    blocked_by: frozenset[TaskId] = frozenset()
    #: 実装タスクのブランチ。フローを終えたら、この名前で積む列に入る（TaskGated）
    branch: BranchName | None = None
    #: 統合に失敗したタスクを引き継いだなら、解き直す衝突したファイル。ResolveConflict が書いてよい
    #: ファイル（WriteScope.LISTED）で、成果物 conflicts の中身
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageRequested(Event):
    """ステージを走らせると決めた。実行器がこれを受けて HEAD とセッション id を集め、BeginStage を出す。

    試行の回数は Task が決め、`execution` に載せる（実行器はこの id で BeginStage を出す）。
    """

    execution: ExecutionId
    #: Flow.steps の添字
    step: int


@dataclass(frozen=True)
class StageStarted(Event):
    execution: ExecutionId
    #: 始めた時点のコミット。再開に失敗したら、ここに戻して新しい実行を作る
    start_commit: CommitSha
    #: LLM のステージのセッション。決定的なステージには無い
    session: SessionId | None = None


@dataclass(frozen=True)
class ExecutionRestarted(Event):
    """再開に失敗した実行を捨てた（restarted）。続きは新しい実行で、始めた時点のコミットから。"""

    execution: ExecutionId
    reason: str
    #: 戻す先（反応が worktree をここへ戻してから、新しい実行を始める）
    start_commit: CommitSha | None = None


@dataclass(frozen=True)
class StageCompleted(Event):
    """ステージが完了した。ポリシーは、ここに載った欄だけで、結果を受け取る側へのコマンドを組む。"""

    execution: ExecutionId
    produced: tuple[ArtifactRef, ...] = ()
    #: Rebase が衝突したファイル。空なら、続く「衝突したときだけ」のステージを飛ばす
    conflicts: tuple[str, ...] = ()
    #: 完了して外した成果物（StageSpec の consumes）
    removed: tuple[ArtifactKind, ...] = ()
    #: 進んだ先。None なら cursor は動かない（書き直す前のフローの実行・並列の見る役の途中・判定・
    #: 結果を渡した先が受けるのを待っている）。飛ばす段を飛ばした後の位置で、Task が決める
    cursor: Cursor | None = None
    #: 結果の中身（StageSpec.result の欄を読み替えたもの）。書き直す前のフローの実行には無い
    result: StageResult | None = None
    #: 結果をどこへどう渡すか（StageSpec.hands_to）。在れば、ConfirmHandoff（判定は Conclude*Round）が
    #: 届くまで cursor を進めない。ポリシーは、これで受け取る側へのコマンドを決める
    handoff: Handoff | None = None
    #: 設計の段で、このステージが見た提案の版（DesignReview・DesignJudge。RecordFindings・
    #: RecordJudgement の design）
    reviewed: DesignVersion | None = None
    #: 提案を Design へ渡すとき、ラン共通の成果物（brief・codemap）の在りか（ProposeDesign の artifacts）
    shared: tuple[ArtifactRef, ...] = ()
    #: git 管理タスクのフローなら、処理している仕事（相手のタスク・ブランチ・base・閉じる所）
    job: GitJob | None = None
    #: 結果を渡すとき、調べる先（受けられなかったら、上げるエスカレーションに添える）
    pointers: Pointers | None = None


@dataclass(frozen=True)
class HandoffConfirmed(Event):
    """結果を渡した先が受けた。cursor を進める（行き先は Task が決める）。"""

    execution: ExecutionId
    cursor: Cursor | None = None


@dataclass(frozen=True)
class HandoffFailed(Event):
    """結果を渡した先が受けなかった。実行は refused になり、一緒に出る EscalationRaised
    （result-refused）が解けたら、同じステージをもう一度走らせる。"""

    execution: ExecutionId
    reason: str


@dataclass(frozen=True)
class StageFailed(Event):
    execution: ExecutionId
    reason: str


@dataclass(frozen=True)
class StageSentBack(Event):
    execution: ExecutionId
    #: 差し戻す理由（落ちた軽い検査のコマンドと出力の末尾）
    reason: str


@dataclass(frozen=True)
class StageInterrupted(Event):
    execution: ExecutionId
    #: 止めた理由。呼び直したときに再開するかは、これで決まる（InterruptCause.resumes_on_restart）
    cause: InterruptCause


@dataclass(frozen=True)
class StageCancelled(Event):
    """走らせると決めてまだ始めていない実行を、フローを捨てたのでやめた。以後 BeginStage を受けない。"""

    execution: ExecutionId
    reason: str


@dataclass(frozen=True)
class StageDeferred(Event):
    """計画ステージの ask が defer で止まった。"""

    execution: ExecutionId
    tool_use_id: str


@dataclass(frozen=True)
class StageReported(Event):
    """報告（designGap など）を返して終わった。cursor は進めない。"""

    execution: ExecutionId
    kind: EscalationKind
    #: 報告の中身（結果の reportReason・期待した証拠と食い違った所）。RejectRequest の理由になる
    reason: str = ""
    #: git 管理タスクのフローなら、処理している仕事（RejectRequest の相手のタスク）
    job: GitJob | None = None


@dataclass(frozen=True)
class GateFailed(Event):
    """Gate が不合格だった（ステージの失敗ではない）。cursor は直前の ReviewLoop の直す役（Fix）を指す。

    Fix はすぐには起動しない。G- の指摘を判定した台帳の結果（FindingsEvaluated）を受けた
    ポリシーの ConcludeGateRound で起動する。
    """

    execution: ExecutionId
    failed: tuple[GateItemResult, ...]
    #: 戻った先（直前の ReviewLoop の、最後のラウンドの直す役）
    cursor: Cursor
    #: Gate の結果に添えた調べる先。G- の指摘が停滞したときの上げに載せる
    pointers: Pointers | None = None


@dataclass(frozen=True)
class WorktreeReady(Event):
    """git 管理タスクの CutBranch が worktree を切った。"""

    #: その worktree を使うタスク
    task: TaskId
    #: ランディレクトリからのパス（`trees/overview`・`trees/stack-top`・`trees/<TaskId>`）
    tree: str
    branch: BranchName | None = None
    #: 切った仕事。計画タスクの統括は、仕事の種類（概要ブランチ・stack-top）で組む並びを決める
    job: GitJob | None = None
    #: 切った元のコミット。ポリシーが、その worktree を使うタスクに RecordBase で渡す
    base: CommitSha | None = None


@dataclass(frozen=True)
class BranchRebased(Event):
    """git 管理タスクの Rebase が、タスクのブランチを載せ直した（積み直しで根元が動いた）。"""

    #: 載せ直したブランチのタスク
    task: TaskId
    #: 載せ直した先のコミット。ポリシーが、そのタスクに RecordBase で渡す
    onto: CommitSha
    job: GitJob | None = None


@dataclass(frozen=True)
class BaseRecorded(Event):
    """タスクのブランチの根元（差分の起点）と、そのタスクのために切った worktree を覚えた。"""

    base: CommitSha
    #: 切った worktree（ランディレクトリからのパス）。Rebase で根元だけが動いたなら None
    tree: str | None = None


@dataclass(frozen=True)
class RoundConcluded(Event):
    """合成ステージの 1 ラウンドの判定を受けた。Gate の不合格の後の判定も含む。

    `finished` なら合成ステージを抜けて `produced` を作り、次の段へ進む。そうでなければ、
    同じラウンドの直す役（ReviewLoop は Fix、DesignLoop は Revise）へ進む。行き先は `cursor`。
    """

    step: int
    round: int
    finished: bool
    cursor: Cursor
    produced: tuple[ArtifactRef, ...] = ()
    #: 判定の実行（Judge・DesignJudge・Gate）。判定の結果を渡した先を待っていたなら、待ちを解く
    judge: ExecutionId | None = None
    #: そのラウンドで走らせる直す役の並び。空（古いイベント）なら、直す役の後は次のラウンドの頭へ進む
    fixers: tuple[StageKind, ...] = ()


@dataclass(frozen=True)
class FlowFinished(Event):
    """フローの最後の段を終えた。種類を問わず出る（実装タスクは TaskGated も出す）。"""

    version: int
    #: git 管理タスクのフローなら、終えた仕事（FinishGitJob。仕上げなら git 管理タスクを終える）
    job: GitJob | None = None


@dataclass(frozen=True)
class FlowAbandoned(Event):
    """フローを続けずに捨てた（回答以外でエスカレーションを閉じた・AbandonFlow）。置き換えを待つ。

    git 管理タスクなら、処理していた仕事を終える（FinishGitJob）。
    """

    version: int
    reason: str
    job: GitJob | None = None


@dataclass(frozen=True)
class TaskGated(Event):
    """フローを終えた（gated と pr-body が揃った）。実装タスクだけが出す。"""

    branch: BranchName | None = None


@dataclass(frozen=True)
class TaskStopped(Event):
    """Run がタスクを止めた（TasksStopped）。以後、このタスクはステージを始めない。"""

    reason: str


@dataclass(frozen=True)
class EscalationResolved(Event):
    """回答が届いた。ポリシーは種類で続きを決める（design-* なら ResumeDesign、ask なら反応が回答の
    ファイルを書いてから ResumeStage）。"""

    escalation: EventId
    answer: str
    #: 答えたエスカレーションの種類
    kind: EscalationKind
    #: そのエスカレーションが起きた実行
    origin: ExecutionId | None = None
    #: ユーザーの回答を渡したなら、その質問
    question: QuestionId | None = None
    #: 回答を受けて続きから再開する実行（defer で止まった計画ステージの ask）。Task が決める
    resume: ExecutionId | None = None
    #: 再開する実行の止めた呼び出し。反応は `answers/<tool_use_id>.json` に回答を書いてから再開する
    tool_use_id: str | None = None


@dataclass(frozen=True)
class EscalationClosed(Event):
    """エスカレーションを閉じた。Task と Run の両方が出す。

    Task では回答以外で片付いたときで、defer で止まっていた実行は abandoned になり、そのフローは
    置き換えを待つ。Run では、ラン統括の判断（answer・insert-task・stop-tasks・apply-plan）や
    再計画を処理したとき。
    """

    escalation: EventId
    reason: str
    #: Run が出すとき、上げてきたタスクと、そのタスクの側のエスカレーション。`source` があれば
    #: ポリシーはタスクの側も CloseEscalation で閉じる。答えたとき（EscalationAnswered）は回答を
    #: 下ろすので載せない
    task: TaskId | None = None
    source: EventId | None = None
    #: Task が出すとき、閉じたエスカレーションの種類と、起きた実行（EscalationRaised のもの）。
    #: ポリシーが、どの上げを答え以外で閉じたかを見分ける（止めた git の仕事をやめる など）
    kind: EscalationKind | None = None
    origin: ExecutionId | None = None


@dataclass(frozen=True)
class ScopeChanged(Event):
    spec: TaskSpec
    #: そのときのラン共通の成果物（design の版など）
    artifacts: tuple[ArtifactRef, ...]
    pointers: Pointers
    #: 積む列から外して始め直したタスクの、新しいブランチ（使ったブランチの名前は使い回さない）
    branch: BranchName | None = None


@dataclass(frozen=True)
class NoteAdded(Event):
    decision: Decision
