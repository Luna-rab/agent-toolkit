"""Task 集約。

タスク 1 つの中身。種類（計画・実装・git 管理）にかかわらず形は同じで、ストリームは TaskOpened から
始まる。フローと cursor を持ち、**次にどのステージを走らせるかは Task が決める**。

- 次の一手は `_moves` が cursor から決め、Task が StageRequested を自分で出す。実行器はそれを受けて
  HEAD とセッション id を集め、BeginStage を出す
- cursor の行き先（段・中のステージ・ラウンド・飛ばす段）は handle で決めてイベントに載せ、apply は
  それを当てるだけにする（再生のときに規則を走らせ直さない）
- ステージの性質は StageSpec の宣言を読む（合成ステージの中の役・返してよい報告・結果の JSON で読む
  欄・結果を渡す先・決定的なステージが期待する証拠・衝突したときだけ走るか・変えずに終えてよいか）。
  ステージごとの表や分岐をここに置かない
- 合成ステージ（ReviewLoop・DesignLoop）の中も、実行器のループではなく Task の状態で進める。
  1 ラウンドは「頭 → 見る役（並列）→ 判定 → 直す役」で、判定の後は、ほかの集約
  （指摘の台帳・Design）の結果が ConcludeReviewRound・ConcludeDesignRound で届くまで止まる。Gate の
  不合格の後は、G- の指摘の判定の結果が ConcludeGateRound で届くまで止まる
- **結果をほかの集約へ渡すステージ（StageSpec.hands_to）は、渡した先の「受けた／受けられない」が
  届くまで cursor を進めない。** 受けた（ConfirmHandoff。判定の役は Conclude*Round）なら進め、受け
  られなかった（ConfirmHandoff の refused）なら result-refused で上げ、解けたら同じステージを
  もう一度走らせる
- 実行器は証拠を集めるだけで、完了・失敗・エスカレーションのどれにするかは ReportStageResult を
  受けた Task が決める。結果の JSON は `domain/stages/results.py` で値に読み替えて、
  StageCompleted に載せる
- 回答以外でエスカレーションを閉じたら（EscalationClosed）、そのフローは続けず（FlowAbandoned）、
  置き換えを待つ
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from ..commands.task import (
    AbandonFlow,
    AcceptFlow,
    AddNote,
    BeginStage,
    ChangeScope,
    CloseEscalation,
    ConcludeDesignRound,
    ConcludeGateRound,
    ConcludeReviewRound,
    ConfirmHandoff,
    Escalate,
    InterruptStage,
    MarkInterrupted,
    OpenTask,
    RecordBase,
    ReportBeginFailure,
    ReportStageResult,
    ResolveEscalation,
    ResumeInterrupted,
    ResumeStage,
    StopTask,
)
from ..events.base import Event
from ..events.run import EscalationRaised
from ..events.task import (
    BaseRecorded,
    BranchRebased,
    EscalationClosed,
    EscalationResolved,
    ExecutionRestarted,
    FlowAbandoned,
    FlowAccepted,
    FlowFinished,
    FlowRejected,
    GateFailed,
    HandoffConfirmed,
    HandoffFailed,
    NoteAdded,
    RoundConcluded,
    ScopeChanged,
    StageCancelled,
    StageCompleted,
    StageDeferred,
    StageFailed,
    StageInterrupted,
    StageReported,
    StageRequested,
    StageSentBack,
    StageStarted,
    TaskGated,
    TaskOpened,
    TaskStopped,
    WorktreeReady,
)
from ..flow.flow import Cursor, Flow, FlowStep
from ..flow.validator import FlowValidator
from ..guard import cut_off_by_denials
from ..services.escalation_router import EscalationRouter, task_of_stream
from ..services.gate import GateEvaluator
from ..stages.catalog import STAGE_SPECS, StageSpec, inner_with
from ..stages.kinds import (
    EvidenceCheck,
    Handoff,
    InnerRole,
    SessionScope,
    StageMode,
    StepArgument,
    has_own_commits,
)
from ..stages.results import has_report, parse_result, read_report
from ..value_objects.artifact_kind import RUN_SHARED_ARTIFACTS, ArtifactKind
from ..value_objects.artifact_ref import ArtifactRef
from ..value_objects.base import InvalidValue
from ..value_objects.branch_name import BranchName
from ..value_objects.commit_sha import CommitSha
from ..value_objects.decision import Decision
from ..value_objects.decision_origin import DecisionOrigin
from ..value_objects.design_version import DesignVersion
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.event_id import EventId
from ..value_objects.evidence import Evidence
from ..value_objects.execution_id import ExecutionId
from ..value_objects.finding_id import FindingId
from ..value_objects.finding_route import FindingRoute
from ..value_objects.finding_target import FindingTarget
from ..value_objects.gate_item_result import GateItemResult
from ..value_objects.gate_report import GateReport
from ..value_objects.hint import Hint
from ..value_objects.interrupt_cause import InterruptCause
from ..value_objects.pointers import Pointers
from ..value_objects.question_id import QuestionId
from ..value_objects.session_id import SessionId
from ..value_objects.stage_exit import StageExit
from ..value_objects.stage_kind import StageKind
from ..value_objects.stage_result import StageResult
from ..value_objects.stall_cause import StallCause
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId
from ..value_objects.task_kind import TaskKind
from ..value_objects.task_spec import TaskSpec
from ..value_objects.verify_kind import VerifyKind
from ..value_objects.verify_result import VerifyResult
from .base import Aggregate, Rejected, applies, handles

_S = StageKind
_E = EscalationKind
_A = ArtifactKind
_R = InnerRole
_C = InterruptCause

#: 同じ位置が続けてこの回数落ちたら、stage-errors で上げる（1 回はやり直す）
FAILURES_BEFORE_ESCALATION = 2

#: 同じ位置で軽い検査が続けてこの回数落ちて差し戻したら、次に落ちたときは差し戻さず quick-check-failed で
#: 上げる（2 回は同じ位置をもう一度走らせる）
SEND_BACKS_BEFORE_ESCALATION = 2

#: 指摘の台帳へ渡す受け渡し（設計の段なら、見た提案の版を添える）
_TO_REVIEW = frozenset({Handoff.FINDINGS, Handoff.JUDGEMENT})

#: 止めた理由ごとに、それを出せるコマンド
_MARKED_BY_DRIVER = frozenset({_C.STARTUP, _C.PANIC})
_INTERRUPTED_BY_POLICY = frozenset({_C.PANIC, _C.REQUESTED})


class ExecutionStatus(Enum):
    """ステージの実行の状態。`requested` は、走らせると決めて BeginStage を待っている。"""

    REQUESTED = "requested"
    RUNNING = "running"
    COMPLETED = "completed"
    REPORTED = "reported"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    DEFERRED = "deferred"
    ABANDONED = "abandoned"
    RESTARTED = "restarted"
    #: 完了したが、結果を渡した先が受けなかった（HandoffFailed）。解けたら同じ位置をもう一度走らせる
    REFUSED = "refused"
    #: 完了にせず、同じ位置をもう一度走らせるために差し戻した（StageSentBack）
    SENT_BACK = "sent-back"


_X = ExecutionStatus

#: 今のフローの、次のステージを始めるのを止める状態。interrupted と deferred は、同じ実行を続きから
#: 再開する。書き直す前のフローの実行で止めるのは、走っているものだけ（同時に走るステージは 1 つ）
_ACTIVE = frozenset({_X.REQUESTED, _X.RUNNING, _X.INTERRUPTED, _X.DEFERRED})

#: StageStarted で続きから始めた元の状態（Execution.resumed_from）
_RESUMED_FROM = frozenset({_X.INTERRUPTED, _X.DEFERRED})

#: 続けたセッションが、この回数続けて落ちたら捨てて新しく立てる
SESSION_FAILURES_BEFORE_FRESH = 2


class StartMode(Enum):
    """走っている実行を、実行器がどう起こすか（`Task.how_to_start`）。"""

    #: 初めて始める（プロンプトを渡す）
    FRESH = "fresh"
    #: ask の defer で止まった実行を、回答のファイルを書いた後に続ける（プロンプトを渡さない）
    DEFERRED = "deferred"
    #: 止めた実行を続ける（短い続きの指示を渡す）。決定的なステージは、もう一度流す
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class StageStart:
    mode: StartMode
    #: claude を `--resume` で起こすか（そのセッションが、この起動の前にもう始まっている）
    resume_session: bool


_START_MODES: Mapping[ExecutionStatus, StartMode] = {
    ExecutionStatus.DEFERRED: StartMode.DEFERRED,
    ExecutionStatus.INTERRUPTED: StartMode.INTERRUPTED,
}


@dataclass(frozen=True)
class Execution:
    """ステージの実行 1 回。"""

    id: ExecutionId
    #: Flow.steps の添字
    step: int
    #: どの版のフローで走らせたか。書き直す前のフローの実行は、cursor を動かさない
    flow_version: int
    status: ExecutionStatus = _X.REQUESTED
    start_commit: CommitSha | None = None
    session: SessionId | None = None
    tool_use_id: str | None = None
    #: 完了して、結果を渡した先が受けるのを待っている（渡す先）
    awaiting: Handoff | None = None
    #: 結果を渡した先が受けなかったとき・判定の後に上げるときに、エスカレーションへ添える調べる先
    pointers: Pointers | None = None
    #: 完了したときの結果。判定の後に上げるエスカレーションの理由（停滞の理由・設計の問い）を読む
    result: StageResult | None = None
    #: interrupted なら、止めた理由
    interrupted_by: InterruptCause | None = None
    #: 最後の StageStarted が、どの状態から続けたものか（interrupted・deferred）。初めて始めたなら
    #: None。実行器が再開のしかた（プロンプトを渡すか・続きの指示か）を決めるのに読む
    resumed_from: ExecutionStatus | None = None
    #: 終わった理由（失敗・取り消し・やり直し・受け渡しの失敗・報告）。完了・中断なら None
    end_reason: str | None = None
    #: Gate で落ちた項目（GateFailed.failed）
    gate_failures: tuple[GateItemResult, ...] = ()

    @property
    def position(self) -> tuple[int, StageKind, int]:
        return (self.step, self.id.stage, self.id.round)


class StepState(Enum):
    """フローの段 1 つが、いまどこまで進んだか（`Task.step_states`）。"""

    DONE = "done"
    #: cursor が越えたが、その段で完了した実行が無い（衝突しなかった Rebase の後の段など）
    SKIPPED = "skipped"
    CURRENT = "current"
    PENDING = "pending"


@dataclass(frozen=True)
class OpenEscalation:
    id: EventId
    kind: EscalationKind
    origin: ExecutionId | None
    #: 上げたときのフローの版。今のフローのものを回答以外で閉じたら、そのフローは置き換えを待つ
    flow_version: int | None = None
    #: 上げる理由（EscalationRaised.reason）
    reason: str = ""
    #: ユーザーに聞くとよいこと（EscalationRaised.question）
    question: str | None = None


@dataclass(frozen=True)
class Move:
    """次に走らせるステージ 1 つ。"""

    stage: StageKind
    step: int
    round: int


class _Verdict(Enum):
    HOLDS = "holds"
    MISMATCH = "mismatch"
    #: 確かめる証拠そのものが無い（形の誤り）
    MISSING = "missing"


# --- 合成ステージの役（StageSpec の宣言から） ---


def _one(composite: StageKind, role: InnerRole) -> StageKind:
    (kind,) = inner_with(composite, role)
    return kind


def _fixer_for(composite: StageKind, target: FindingTarget) -> StageKind:
    """合成ステージの中で、宛先 `target` の指摘を直す役（ReviewLoop の Fix・TestFix）。"""
    (kind,) = (k for k in STAGE_SPECS[composite].inner if STAGE_SPECS[k].fixes is target)
    return kind


def _fixers_for(
    composite: StageKind, routes: tuple[FindingRoute, ...], unresolved: tuple[FindingId, ...]
) -> tuple[StageKind, ...]:
    """open の指摘の宛先から、そのラウンドで走らせる直す役の並び（TestFix → Fix の順）。

    宛先を持つ直す役を持たない合成ステージ（DesignLoop）は、直す役が 1 つなので並びを持たない。
    `routes` に無い指摘は impl として扱う。
    """
    if not any(STAGE_SPECS[k].fixes is not None for k in STAGE_SPECS[composite].inner):
        return ()
    targets = {r.finding: r.target for r in routes}
    wanted = {targets.get(f, FindingTarget.IMPL) for f in unresolved}
    return tuple(
        _fixer_for(composite, target)
        for target in (FindingTarget.TESTS, FindingTarget.IMPL)
        if target in wanted
    )


def _lookers(step: FlowStep, round: int) -> tuple[StageKind, ...]:
    """そのラウンドの見る役。reviewers を引数に取る合成ステージは、フローの段に書いた顔ぶれ。"""
    if StepArgument.REVIEWERS in STAGE_SPECS[step.stage].arguments:
        assert step.reviewers is not None  # FlowValidator が求める
        return step.reviewers.for_round(round)
    return inner_with(step.stage, _R.LOOKER)


def _round_head(step: FlowStep, round: int, artifacts: Collection[ArtifactKind]) -> StageKind:
    """ラウンドの最初の中のステージ。頭の役は、走る条件の成果物があるときだけ置く。"""
    for head in inner_with(step.stage, _R.HEAD):
        if STAGE_SPECS[head].when in artifacts:
            return head
    return _lookers(step, round)[0]


def _settle(
    flow: Flow,
    cursor: Cursor,
    artifacts: Collection[ArtifactKind],
    conflicts: tuple[str, ...] | None,
) -> Cursor:
    """飛ばす段を飛ばし、合成ステージの段なら 1 ラウンド目の頭に入った位置。"""
    while not cursor.is_done(flow) and cursor.inner is None:
        step = flow.steps[cursor.step]
        spec = STAGE_SPECS[step.stage]
        earlier = {s.stage for s in flow.steps[: cursor.step]}
        # 衝突したときだけ走るステージは、同じフローの前の Rebase が衝突しなかったら飛ばす
        if spec.after_conflict_only and _S.REBASE in earlier and conflicts == ():
            cursor = cursor.next_step()
            continue
        if spec.mode is StageMode.COMPOSITE:
            cursor = cursor.enter(_round_head(step, 1, artifacts), 1)
        break
    return cursor


def _decision(answer: str, question: QuestionId | None) -> Decision:
    """回答の出どころ。QuestionId があればユーザー、無ければラン統括。"""
    origin = DecisionOrigin.USER if question is not None else DecisionOrigin.RUN_SUPERVISOR
    return Decision(answer, origin, question)


class Task(Aggregate):
    NAME = "Task"

    def __init__(self, stream: StreamId) -> None:
        super().__init__(stream)
        self.id: TaskId = task_of_stream(stream)
        self.kind: TaskKind | None = None
        self.spec: TaskSpec | None = None
        self.branch: BranchName | None = None
        self.blocked_by: frozenset[TaskId] = frozenset()
        #: 統合に失敗したタスクを引き継いだなら、解き直す衝突したファイル
        self.conflict_files: tuple[str, ...] = ()
        #: 成果物の種類ごとに、最後に作ったもの
        self.artifacts: dict[ArtifactKind, ArtifactRef] = {}
        #: 最後に作った提案の版（計画タスク）。設計の段のステージが見た版になる
        self.proposal_version: DesignVersion | None = None
        self.flow: Flow | None = None
        #: 受け入れたフローの数（次のフローの版は、これに 1 を足す）
        self.flow_versions = 0
        self.cursor = Cursor()
        #: FlowFinished を出したフローの版
        self.finished_version: int | None = None
        self.executions: dict[ExecutionId, Execution] = {}
        #: (フローの版, 段, ステージ, ラウンド) → その位置で最後に完了した実行
        self.completed: dict[tuple[int, int, StageKind, int], ExecutionId] = {}
        self.escalations: dict[EventId, OpenEscalation] = {}
        self.notes: list[Decision] = []
        #: 落ちた Gate の実行。G- の指摘の判定（ConcludeGateRound）が届くまで次の一手を止める
        self.gate_failed: ExecutionId | None = None
        #: フローを捨てた（FlowAbandoned）。このフローは続けず、置き換え（AcceptFlow）を待つ
        self.halted = False
        #: 同じ位置（段・ステージ・ラウンド）で続けて落ちた回数（今のフローの分だけ）
        self.failures: dict[tuple[int, StageKind, int], int] = {}
        #: 同じ位置（段・ステージ・ラウンド）で軽い検査が落ちて差し戻した回数（今のフローの分だけ）
        self.send_backs: dict[tuple[int, StageKind, int], int] = {}
        #: 合成ステージを抜けたときのラウンド（段ごと）。Gate の不合格で戻るラウンドになる
        self.loop_rounds: dict[int, int] = {}
        #: 今のラウンドで走らせる直す役の並び（RoundConcluded.fixers）。空なら直す役の後は次のラウンドの頭
        self.fixers: tuple[StageKind, ...] = ()
        #: 今のフローで Rebase が衝突したファイル。Rebase がまだなら None
        self.conflicts: tuple[str, ...] | None = None
        self.gated = False
        self.stopped = False
        #: このタスクのブランチの根元（`git diff <根元>..HEAD` がこのタスクの変更）。切ったときの
        #: 元のコミットで、積み直しの Rebase で根元が動いたら、載せ直した先に替わる
        self.base_commit: CommitSha | None = None
        #: このタスクのために最後に切った worktree（ランディレクトリからのパス）。計画タスクなら、
        #: コードを読む所（初回は概要ブランチ、再計画は stack-top）
        self.code_tree: str | None = None

    # --- 読む ---

    @property
    def working_on(self) -> TaskId:
        """ステージが扱うタスク（名前の付いた規則）。git 管理タスクのフローなら処理している仕事の
        相手、そうでなければ自分。"""
        job = self.flow.job if self.flow is not None else None
        if job is not None and job.task is not None:
            return job.task
        return self.id

    @property
    def current_step(self) -> FlowStep | None:
        return self.flow.at(self.cursor) if self.flow is not None else None

    @property
    def flow_finished(self) -> bool:
        """今のフローを最後まで終えた（FlowFinished を出した）。"""
        return self.flow is not None and self.finished_version == self.flow.version

    def step_states(self) -> tuple[StepState, ...]:
        """今のフローの段ごとの進み具合（Flow.steps の順）。フローが無ければ空。"""
        flow = self.flow
        if flow is None:
            return ()
        ran = {step for (version, step, _, _) in self.completed if version == flow.version}
        states: list[StepState] = []
        for index in range(len(flow.steps)):
            if index < self.cursor.step:
                states.append(StepState.DONE if index in ran else StepState.SKIPPED)
            elif index == self.cursor.step:
                states.append(StepState.CURRENT)
            else:
                states.append(StepState.PENDING)
        return tuple(states)

    def current_executions(self) -> list[Execution]:
        """今のフローの実行と、書き直す前のフローでまだ走っている実行（始めた順）。

        書き直す前のフローの実行も、走っている間は claude と worktree を使っているので外さない。
        """
        return [
            e
            for e in self.executions.values()
            if not self._is_obsolete(e) or e.status is _X.RUNNING
        ]

    def executions_at(self, step: int) -> list[Execution]:
        """今のフローの版で、その段の実行（始めた順）。"""
        return [e for e in self.executions.values() if not self._is_obsolete(e) and e.step == step]

    def earlier_executions(self) -> list[Execution]:
        """書き直す前のフローの版で、まだ走っている実行（始めた順）。"""
        return [e for e in self.current_executions() if self._is_obsolete(e)]

    def unplaced_executions(self) -> list[Execution]:
        """今のフローの版の実行で、step が flow.steps のどの添字にも当たらないもの（始めた順）。"""
        if self.flow is None:
            return []
        steps = range(len(self.flow.steps))
        return [
            e for e in self.executions.values() if not self._is_obsolete(e) and e.step not in steps
        ]

    def running_executions(self) -> list[ExecutionId]:
        """running のまま残っている実行（メインループが起動時とパニックのときに聞く）。"""
        return sorted((e.id for e in self.executions.values() if e.status is _X.RUNNING), key=str)

    def session_to_continue(self, execution: ExecutionId, fresh: bool = False) -> SessionId | None:
        """これから始める実行が続けるセッション（StageSpec.session の宣言から）。None なら新しく立てる。

        TASK・RUN は同じ種類のステージの、FOLLOWS は `follows` のステージの、最後に始めた実行の
        セッションを続ける。RUN のステージ（DesignJudge）は計画タスクにしか無いので、タスクの中を
        見れば足りる。`fresh`（FlowStep.fresh_session）なら続けない。続けるセッションが続けて
        SESSION_FAILURES_BEFORE_FRESH 回落ちていたら、捨てて新しく立てる。
        """
        spec = STAGE_SPECS[execution.stage]
        if fresh or spec.session in (SessionScope.NONE, SessionScope.FRESH):
            return None
        kinds = spec.follows if spec.session is SessionScope.FOLLOWS else (execution.stage,)
        started = [
            e
            for e in self.executions.values()
            if e.id != execution and e.id.stage in kinds and e.session is not None
        ]
        # `--resume` で続けられなかった（restarted）セッションは、もう続けない
        if not started or started[-1].status is _X.RESTARTED:
            return None
        session = started[-1].session
        used = [e for e in self.executions.values() if e.session == session and e.id != execution]
        recent = used[-SESSION_FAILURES_BEFORE_FRESH:]
        if len(recent) == SESSION_FAILURES_BEFORE_FRESH and all(
            e.status in (_X.FAILED, _X.RESTARTED) for e in recent
        ):
            return None
        return session

    def how_to_start(self, execution: ExecutionId, *, agent_started: bool = False) -> StageStart:
        """走っている実行を、実行器がどう起こすか。

        `agent_started` は、この実行の claude をもう起こした跡（ログ）があるか。初めて始めたはずの
        実行に跡があるのは、StageStarted を配り直して走らせ直すときで、前の起動が同じセッション id を
        使っている。新しく立てると重なるので、止めた実行として `--resume` で続ける。続けられなければ、
        結果の証拠を見て作り直す（`_session_lost`）。
        """
        record = self.executions[execution]
        mode = _START_MODES.get(record.resumed_from, StartMode.FRESH)
        if mode is StartMode.FRESH and agent_started:
            mode = StartMode.INTERRUPTED
        session = record.session
        continued = any(
            e.id != execution and e.session == session and e.start_commit is not None
            for e in self.executions.values()
        )
        resume = session is not None and (mode is not StartMode.FRESH or continued)
        return StageStart(mode, resume)

    def reset_before_start(self, execution: ExecutionId) -> CommitSha | None:
        """これから始める実行の前に、worktree を戻すコミット。戻さないなら None。

        同じ位置の 1 つ前の試みを作り直した（restarted）なら、その試みが始めた時点へ戻す。作り直しの
        反応が戻す前に driver が落ちると、反応は配り直されず、呼び直した driver が戻していない HEAD から
        次の試みを始めてしまう。始める前に戻し直せば、何度戻しても同じ結果になる。

        始めていない試み（begin が落ちた。`start_commit` が無い）は worktree に触れていないので、飛ばして
        遡る。飛ばさないと、作り直しの反応が戻すのを待ち切れず、続く試みの begin も落ちたときに、その
        次の試みが戻していない HEAD から始まる。
        """
        record = self.executions.get(execution)
        if record is None or record.status is not _X.REQUESTED:
            return None
        earlier = [
            e
            for e in self.executions.values()
            if e.position == record.position
            and e.flow_version == record.flow_version
            and e.id.attempt < execution.attempt
            and e.start_commit is not None
        ]
        if not earlier:
            return None
        previous = max(earlier, key=lambda e: e.id.attempt)
        return previous.start_commit if previous.status is _X.RESTARTED else None

    def baseline_of(self, execution: ExecutionId) -> ExecutionId:
        """今のフローで、同じ位置（段・ステージ・ラウンド）の最初の試み。知らない実行なら KeyError。

        軽い検査の baseline は、この試みを始める前にだけ取る。差し戻した後の試みで取り直すと、差し戻した
        試みが入れた落ちが「始める前から落ちていた」に入る。
        """
        record = self.executions[execution]
        same = (
            e
            for e in self.executions.values()
            if e.position == record.position and e.flow_version == record.flow_version
        )
        return min(same, key=lambda e: e.id.attempt).id

    def requested_executions(self) -> list[ExecutionId]:
        """走らせると決めて、まだ始めていない今のフローの実行（BeginStage を待っている）。

        StageRequested を配った後、実行器の BeginStage がメインループに届く前に driver が止まる
        （パニック・落ちた）と、その実行は誰にも始められないまま残る。呼び直された driver がこれを
        聞いて、始めるのを頼み直す。止めたタスクと書き直す前のフローの実行は、もう始めない。
        """
        if self.stopped or self.halted:
            return []
        return sorted(
            (
                e.id
                for e in self.executions.values()
                if e.status is _X.REQUESTED and not self._is_obsolete(e)
            ),
            key=str,
        )

    def _is_obsolete(self, execution: Execution) -> bool:
        """書き直す前のフロー（範囲が変わった後も含む）の実行か。"""
        return self.flow is None or execution.flow_version != self.flow.version

    def _is_live(self, execution: Execution) -> bool:
        """生きているフローの実行か（名前の付いた規則）。今のフローの実行で、そのフローを捨てていない。

        生きていない実行は、始めない・再開しない・やり直さない。完了の報告は作った成果物だけを残し、
        受け渡しの答えも cursor を動かさない。捨てたフローの続きを走らせると、置き換えのフローと
        同時に走るか、捨てた理由（タスクを止めた・回答以外で閉じた）を無視して進む。
        """
        return not self._is_obsolete(execution) and not self.halted

    def _blocking(self) -> list[Execution]:
        """次のステージを始めるのを止める実行。結果を渡した先の答えを待つ実行も止める。"""
        return [
            e
            for e in self.executions.values()
            if (e.status is _X.RUNNING)
            or (not self._is_obsolete(e) and (e.status in _ACTIVE or e.awaiting is not None))
        ]

    def _completed_at(self, step: int, stage: StageKind, round: int) -> Execution | None:
        """今のフローで、その位置で最後に完了した実行（渡した先の答えを待っているものも含む）。"""
        if self.flow is None:
            return None
        found = self.completed.get((self.flow.version, step, stage, round))
        return self.executions[found] if found is not None else None

    def _settled_at(self, step: int, stage: StageKind, round: int) -> bool:
        """今のフローで、その位置の実行が完了し、渡した先も受けた。"""
        execution = self._completed_at(step, stage, round)
        return execution is not None and execution.awaiting is None

    def _awaiting_judgement(self) -> Execution | None:
        """合成ステージの判定を終え、ほかの集約の結果を待っているなら、その判定の実行。"""
        inner = self.cursor.inner
        if inner is None or STAGE_SPECS[inner].role is not _R.JUDGE:
            return None
        return self._completed_at(self.cursor.step, inner, self.cursor.round)

    def _waiting_answer(self, execution: ExecutionId) -> bool:
        return any(e.origin == execution for e in self.escalations.values())

    def _resumes_on_restart(self, execution: Execution) -> bool:
        """呼び直されたときに続きから再開する実行（名前の付いた規則）。止めた理由が driver の都合で、
        生きているフローの実行であること。"""
        return (
            execution.status is _X.INTERRUPTED
            and execution.interrupted_by is not None
            and execution.interrupted_by.resumes_on_restart
            and self._is_live(execution)
        )

    # --- 共通の検査 ---

    def _require_opened(self) -> TaskKind:
        if self.kind is None:
            raise Rejected(f"{self.id} はまだ開いていない")
        return self.kind

    def _require_live(self) -> TaskKind:
        kind = self._require_opened()
        if self.stopped:
            raise Rejected(f"{self.id} は止めたタスク")
        return kind

    def _execution(self, execution: ExecutionId, *statuses: ExecutionStatus) -> Execution:
        found = self.executions.get(execution)
        if found is None or found.status not in statuses:
            now = found.status.value if found else "無い"
            wanted = "・".join(s.value for s in statuses)
            raise Rejected(f"{execution} は {wanted} でない（{now}）")
        return found

    def _resumable(self, execution: ExecutionId) -> Execution:
        """止まった実行を、続きから再開するか、新しい実行でやり直してよいか。"""
        self._require_live()
        found = self._execution(execution, _X.INTERRUPTED, _X.DEFERRED)
        if not self._is_live(found):
            # 新しいフローと同時に走らせない。新しいフローの次の一手を止めることもない
            raise Rejected(f"{execution} は書き直す前か捨てたフローの実行で、再開しない")
        if found.status is _X.DEFERRED and self._waiting_answer(found.id):
            raise Rejected(f"{execution} の ask の回答がまだ届いていない")
        return found

    # --- 次の一手 ---

    def _moves(self) -> list[Move]:  # noqa: PLR0911  止める理由ごとの分岐
        """今走らせてよい次のステージ。"""
        flow = self.flow
        if self.kind is None or self.stopped or flow is None or self.escalations:
            return []
        if self.halted or self.gate_failed is not None or self.cursor.is_done(flow):
            return []
        step = flow.steps[self.cursor.step]
        active = self._blocking()
        here, round = self.cursor.step, self.cursor.round
        if self.cursor.inner is None:
            return [] if active else [Move(step.stage, here, 0)]
        if STAGE_SPECS[self.cursor.inner].role is _R.LOOKER:
            # 見る役は並列に走らせる。同じラウンドの見る役のほかが走っていれば待つ
            lookers = _lookers(step, round)
            busy = set()
            for execution in active:
                if (
                    self._is_obsolete(execution)
                    or execution.position[0::2] != (here, round)
                    or execution.id.stage not in lookers
                ):
                    return []
                busy.add(execution.id.stage)
            return [
                Move(kind, here, round)
                for kind in lookers
                if kind not in busy and self._completed_at(here, kind, round) is None
            ]
        if active or self._awaiting_judgement() is not None:
            return []
        return [Move(self.cursor.inner, here, round)]

    def _cursor_after(  # noqa: PLR0911  合成ステージの中の役ごとの分岐
        self,
        execution: Execution,
        artifacts: Collection[ArtifactKind],
        conflicts: tuple[str, ...] | None,
    ) -> Cursor | None:
        """cursor が指す位置のステージを終えた（渡した先も受けた）後の cursor。動かないなら None。"""
        flow = self.flow
        if self._is_obsolete(execution) or flow is None:
            return None
        cursor = self.cursor
        if (execution.step, execution.id.round) != (cursor.step, cursor.round):
            return None
        step = flow.steps[cursor.step]
        stage = execution.id.stage
        if cursor.inner is None:
            if stage is not step.stage:
                return None
            return _settle(flow, cursor.next_step(), artifacts, conflicts)
        role = STAGE_SPECS[stage].role
        round = cursor.round
        if role is _R.LOOKER and STAGE_SPECS[cursor.inner].role is _R.LOOKER:
            lookers = _lookers(step, round)
            done = {k for k in lookers if self._settled_at(cursor.step, k, round)} | {stage}
            if done >= set(lookers):
                return cursor.enter(_one(step.stage, _R.JUDGE), round)
            return None
        if stage is not cursor.inner:
            return None
        if role is _R.HEAD:
            return cursor.enter(_lookers(step, round)[0], round)
        if role is _R.FIXER:
            # そのラウンドの直す役が残っていれば次へ、最後なら次のラウンドの頭へ
            if stage in self.fixers and stage is not self.fixers[-1]:
                return cursor.enter(self.fixers[self.fixers.index(stage) + 1], round)
            return cursor.enter(_round_head(step, round + 1, artifacts), round + 1)
        # 判定の役は、ほかの集約の結果が届くまで動かない
        return None

    def _request(self, move: Move) -> StageRequested:
        # 試行の回数はタスクの一生で数える。フローを書き直してラウンドが r1 からやり直しても、
        # 同じ実行の id を使い回さない（ReviewLedger・Design はこれを前提にする）
        attempt = 1 + sum(
            1 for e in self.executions if e.stage is move.stage and e.round == move.round
        )
        return StageRequested(ExecutionId(self.id, move.stage, move.round, attempt), move.step)

    def _follow_up(self, events: Sequence[Event]) -> list[Event]:
        """`events` を当てた後の続き。フローを終えたなら FlowFinished、そうでなければ次のステージ。"""
        after = self.preview(events)
        flow = after.flow
        if flow is None:
            return []
        if after.cursor.is_done(flow):
            if after.flow_finished or after.halted:
                return []
            done: list[Event] = [FlowFinished(flow.version, flow.job)]
            if after.kind is TaskKind.IMPLEMENTATION:
                done.append(TaskGated(after.branch))
            return done
        requests: list[Event] = []
        for move in after._moves():
            request = after._request(move)
            requests.append(request)
            after = after.preview([request])
        return requests

    # --- 開く・フロー ---

    @handles(OpenTask)
    def _open(self, command: OpenTask) -> list[Event]:
        if self.kind is not None:
            raise Rejected(f"{self.id} はもう開いている")
        if command.kind is not self.id.kind:
            raise Rejected(
                f"{self.id} の種類は {self.id.kind.value} で、{command.kind.value} ではない"
            )
        if (command.kind is TaskKind.IMPLEMENTATION) != (command.spec is not None):
            raise Rejected("中身（TaskSpec）を持つのは実装タスクだけ")
        return [
            TaskOpened(
                command.kind,
                command.spec,
                command.artifacts,
                command.blocked_by,
                command.branch,
                command.conflicts,
            )
        ]

    @handles(AcceptFlow)
    def _accept_flow(self, command: AcceptFlow) -> list[Event]:
        kind = self._require_live()
        if command.responds_to is not None and command.responds_to not in self.escalations:
            raise Rejected(f"応える先の {command.responds_to} は開いているエスカレーションではない")
        if self.escalations and command.responds_to is None:
            raise Rejected(
                "回答を待っているエスカレーションがある。それに応えるフローなら responds_to を書く"
            )
        if self.gated:
            raise Rejected(
                "フローを終えて積む列に入っている。範囲が変わる（ScopeChanged）までフローを受けない"
            )
        if command.responds_to is None and not self._flow_replaceable():
            raise Rejected("フローの途中で、応えるエスカレーションも範囲の変更も無い")
        reasons = FlowValidator.check(command.steps, kind, self.artifacts.keys(), command.job)
        if reasons:
            return [FlowRejected(command.steps, reasons)]
        flow = Flow(command.steps, self.flow_versions + 1, command.job)
        cursor = _settle(flow, Cursor(), self.artifacts.keys(), None)
        events: list[Event] = [FlowAccepted(flow, closes=command.responds_to, cursor=cursor)]
        return events + self._follow_up(events)

    def _flow_replaceable(self) -> bool:
        """フローを置き換えてよいか（名前の付いた規則）。

        まだ無い・範囲が変わって捨てた・最後まで終えた・捨てた（FlowAbandoned）、のどれか。最後の
        ものは、計画タスクの ask を replan で閉じた後の再計画のフローや、git 管理タスクの統合の失敗を
        閉じた後の次の仕事のフローを受けるためである。
        """
        return self.flow is None or self.flow_finished or self.halted

    # --- 実行 ---

    @handles(BeginStage)
    def _begin(self, command: BeginStage) -> list[Event]:
        self._require_live()
        execution = self._execution(command.execution, _X.REQUESTED)
        if not self._is_live(execution):
            raise Rejected(f"{execution.id} は書き直す前か捨てたフローで決めた実行で、始めない")
        is_llm = STAGE_SPECS[command.execution.stage].mode is StageMode.LLM
        if is_llm != (command.session is not None):
            raise Rejected("セッションを持つのは LLM のステージだけ")
        return [StageStarted(command.execution, command.head, command.session)]

    @handles(ReportBeginFailure)
    def _begin_failed(self, command: ReportBeginFailure) -> list[Event]:
        """実行器が始められなかった。走らせて落ちたときと同じ規則で、やり直すか上げる。"""
        self._require_live()
        execution = self._execution(command.execution, _X.REQUESTED)
        if not self._is_live(execution):
            # 書き直す前・捨てたフローの実行は、始めないので失敗にも数えない
            return []
        reason = f"始められなかった: {command.error.strip() or '理由が書かれていない'}"
        return self._fail(execution, Pointers(), reason)

    def _interrupt(self, execution: ExecutionId, cause: InterruptCause) -> list[Event]:
        self._execution(execution, _X.RUNNING)
        return [StageInterrupted(execution, cause)]

    @handles(InterruptStage)
    def _interrupt_stage(self, command: InterruptStage) -> list[Event]:
        if command.cause not in _INTERRUPTED_BY_POLICY:
            raise Rejected(f"ポリシーが止める理由は panic か requested（{command.cause.value}）")
        return self._interrupt(command.execution, command.cause)

    @handles(MarkInterrupted)
    def _mark_interrupted(self, command: MarkInterrupted) -> list[Event]:
        if command.cause not in _MARKED_BY_DRIVER:
            raise Rejected(f"driver が止める理由は startup か panic（{command.cause.value}）")
        return self._interrupt(command.execution, command.cause)

    @handles(ResumeStage)
    def _resume(self, command: ResumeStage) -> list[Event]:
        execution = self._resumable(command.execution)
        assert execution.start_commit is not None
        return [StageStarted(execution.id, execution.start_commit, execution.session)]

    @handles(ResumeInterrupted)
    def _resume_interrupted(self, command: ResumeInterrupted) -> list[Event]:
        if self.kind is None or self.stopped:
            # 計画にあって始めていないタスク（ストリームが無い）・止めたタスク。RunResumed を受けた
            # ポリシーは、どのタスクを始めたかを知らずに出す
            return []
        events: list[Event] = []
        for execution in self.executions.values():
            if self._resumes_on_restart(execution):
                assert execution.start_commit is not None
                events.append(StageStarted(execution.id, execution.start_commit, execution.session))
        return events

    @handles(ReportStageResult)
    def _report(self, command: ReportStageResult) -> list[Event]:
        self._require_live()
        execution = self._execution(command.execution, _X.RUNNING)
        evidence = command.evidence
        if not self._is_live(execution):
            # 書き直す前のフロー・捨てたフローの実行は、作った成果物だけを残す。上げも、やり直しも、
            # 渡しもしない。終わるのを待っていた新しいフローの最初のステージは、ここから始まる
            # 決定的なステージが期待する証拠が外れたら、作ったことにしない（落ちた Gate の gated・
            # 全部通った ConfirmRed の red-tests を、書き直した後のフローに残さない）
            events: list[Event]
            spec = STAGE_SPECS[execution.id.stage]
            expected = spec.expects is None or _check(spec.expects, evidence) is _Verdict.HOLDS
            quick_failed = [r for r in evidence.verify if not r.passed] if spec.checks_after else []
            if evidence.exit is StageExit.OK and evidence.result_valid and quick_failed:
                # 終わった後の軽い検査が落ちた成果物は、書き直した後のフローに残さない。差し戻しも上げもしない
                events = [StageFailed(execution.id, _describe_failures(quick_failed))]
            elif evidence.exit is StageExit.OK and evidence.result_valid and expected:
                events = [self._completed(execution, evidence, None, current=False)]
            elif evidence.exit is StageExit.OK and evidence.result_valid:
                assert spec.expects is not None
                events = [StageFailed(execution.id, _mismatch(spec.expects, evidence))]
            else:
                events = [StageFailed(execution.id, evidence.error or "結果が空か、形が違う")]
            return events + self._follow_up(events)
        return self._judge_result(execution, command)

    def _judge_result(  # noqa: PLR0911, PLR0912  確かめる順の段ごとの分岐
        self, execution: Execution, command: ReportStageResult
    ) -> list[Event]:
        """ステージの結果を、⓪ からの順で確かめる。何を確かめるかは StageSpec の宣言から決まる。"""
        evidence = command.evidence
        spec = STAGE_SPECS[execution.id.stage]
        # ⓪ `--resume` で続けられなかった。失敗に数えず、始めた時点から新しい実行で作り直す
        if _session_lost(evidence):
            reason = evidence.error or "--resume で続けられなかった"
            events: list[Event] = [ExecutionRestarted(execution.id, reason, execution.start_commit)]
            return events + self._follow_up(events)
        # ① defer で止まった。止まると結果は空なので、形より先に見る
        if evidence.deferred is not None:
            if spec.guard is None or not spec.guard.can_ask:
                return self._failed(execution, command, "ask で聞けないステージが defer で止まった")
            return self._reported(
                execution,
                command,
                _E.ASK,
                reason=evidence.deferred.question,
                stage_event=StageDeferred(execution.id, evidence.deferred.tool_use_id),
            )
        # ② エラーで終わった・結果が空か形が違う
        if evidence.exit is StageExit.ERROR or not evidence.result_valid:
            return self._failed(execution, command, evidence.error or "結果が空か、形が違う")
        # ③ 報告。報告を返したステージは何も作らないことがあるので、実物を確かめる前に見る
        if has_report(spec, command.result):
            try:
                kind, reason = read_report(spec, command.result)
            except InvalidValue as e:
                return self._failed(execution, command, str(e))
            if kind not in spec.reports:
                return self._failed(
                    execution, command, f"{spec.kind.value} は {kind.value} を報告しない"
                )
            return self._reported(execution, command, kind, reason=reason)
        # ③' 前後に軽い検査を流すステージで、終わった後の軽い検査が落ちた
        if spec.checks_after and any(not result.passed for result in evidence.verify):
            return self._quick_checks_failed(execution, command)
        # ④ 決定的なステージが期待する証拠（Gate の完了チェックもここ）
        if spec.expects is not None:
            verdict = _check(spec.expects, evidence)
            if verdict is _Verdict.MISSING:
                return self._failed(execution, command, _missing(spec))
            if verdict is _Verdict.MISMATCH:
                if spec.expects is EvidenceCheck.GATE_PASSES:
                    assert evidence.gate is not None
                    return self._gate_failed(execution, command, evidence.gate)
                reason = _mismatch(spec.expects, evidence)
                if spec.on_mismatch is None:
                    return self._failed(execution, command, reason)
                return self._reported(execution, command, spec.on_mismatch, reason=reason)
        # ⑤ 結果の中身と実物。成果物は、それを produces に持つステージが完了したときだけ増える
        try:
            result = parse_result(spec, command.result, evidence)
        except InvalidValue as e:
            return self._failed(execution, command, f"結果の形が違う: {e}")
        # 変えないと返したのに作っていたら、どちらが本当か分からない（前の成果物を使うか、新しい方か）
        kept = spec.can_keep and result.unchanged
        if kept and (
            made := sorted(p.kind.value for p in evidence.products if p.kind in spec.produces)
        ):
            return self._failed(
                execution,
                command,
                f"結果の形が違う: unchanged なのに {', '.join(made)} の実物を作った",
            )
        if missing := self._unproven(spec.kind, evidence, result):
            names = ", ".join(sorted(kind.value for kind in missing))
            reason = f"{names} の実物が無い"
            if kept and evidence.dirty:
                reason += "（コミットしていない変更がある: " + ", ".join(evidence.dirty) + "）"
            return self._failed(execution, command, reason)
        # ⑥ 完了
        events: list[Event] = [
            self._completed(execution, evidence, result, current=True, pointers=command.pointers)
        ]
        job = self.flow.job if self.flow is not None else None
        if (cut := result.worktree) is not None:
            events.append(WorktreeReady(cut.task, cut.tree, cut.branch, job, cut.base))
        onto = self._rebased_onto(spec, evidence, result)
        if onto is not None and job is not None and job.task is not None:
            events.append(BranchRebased(job.task, onto, job))
        return events + self._follow_up(events)

    def _quick_checks_failed(self, execution: Execution, command: ReportStageResult) -> list[Event]:
        """終わった後の軽い検査が落ちた。このステージのせいなら同じ位置をもう一度走らせ、続けて落ちたら上げる。

        始める前（baseline）から落ちていたコマンドは、テストしか書けないステージには直せない。差し戻さず
        に上げる。baseline が取れていなければ、始める前は通っていたとみなす。
        """
        evidence = command.evidence
        failed = [result for result in evidence.verify if not result.passed]
        before = {result.command for result in evidence.baseline or () if not result.passed}
        if inherited := [result for result in failed if result.command in before]:
            reason = "ステージを始める前から落ちていた: " + _describe_failures(inherited)
            return self._reported(execution, command, _E.QUICK_CHECK_FAILED, reason=reason)
        reason = _describe_failures(failed)
        if self.send_backs.get(execution.position, 0) >= SEND_BACKS_BEFORE_ESCALATION:
            return self._reported(execution, command, _E.QUICK_CHECK_FAILED, reason=reason)
        events: list[Event] = [StageSentBack(execution.id, reason)]
        return events + self._follow_up(events)

    def _rebased_onto(
        self, spec: StageSpec, evidence: Evidence, result: StageResult
    ) -> CommitSha | None:
        """このステージの完了で、タスクのブランチが載った先。rebase を終えたときだけ返す。

        衝突で止まった Rebase はまだ載っていない。衝突を解いて続けたステージ（`finishes_rebase`）が
        完了したときに、同じフローの Rebase が返した先へ載る。統合に失敗して取りやめた道では載らない。
        """
        if result.onto is not None:
            return result.onto if not evidence.conflicts else None
        if not spec.finishes_rebase or self.flow is None:
            return None
        version = self.flow.version
        returned = [
            e.result.onto
            for e in self.executions.values()
            if e.flow_version == version and e.result is not None and e.result.onto is not None
        ]
        return returned[-1] if returned else None

    def _unproven(
        self, stage: StageKind, evidence: Evidence, result: StageResult
    ) -> frozenset[ArtifactKind]:
        """作るはずなのに実物が無い成果物。変えないと返したなら、前に作ったものを使う（can_keep）。"""
        spec = STAGE_SPECS[stage]
        made = {product.kind for product in evidence.products}
        if spec.can_keep and result.unchanged:
            missing = spec.produces - self.artifacts.keys()
            # 前の成果物が無くても、汚れていない HEAD は在りかになる
            if evidence.head is not None and evidence.dirty == ():
                return frozenset(k for k in missing if not k.committed)
            return frozenset(missing)
        return frozenset(spec.produces - made)

    def _sent_back_commits(
        self,
        execution: Execution,
        declared: frozenset[ArtifactKind],
        produced: tuple[ArtifactRef, ...],
    ) -> tuple[ArtifactRef, ...]:
        """差し戻した試みが作ったコミットの成果物。

        差し戻した試みの成果物は残らず、worktree も戻さない。次の試みがコミットを足さずに完了すると、
        その試みの開始時点から HEAD が動かず、成果物が空になる。同じ位置の最初の試みの開始時点から
        HEAD が進んでいるなら、この試みの開始時点（= 差し戻した試みの終わり）を成果物の在りかにする。
        """
        if execution.start_commit is None:
            return ()
        earlier = [
            e
            for e in self.executions.values()
            if e.position == execution.position
            and e.flow_version == execution.flow_version
            and e.id.attempt < execution.id.attempt
            and e.start_commit is not None
        ]
        if not any(e.status is _X.SENT_BACK for e in earlier):
            return ()
        first = min(earlier, key=lambda e: e.id.attempt)
        if first.start_commit == execution.start_commit:
            return ()
        made = {p.kind for p in produced}
        return tuple(
            ArtifactRef(kind, str(execution.start_commit))
            for kind in sorted(declared - made, key=str)
            if kind.committed
        )

    def _completed(
        self,
        execution: Execution,
        evidence: Evidence,
        result: StageResult | None,
        *,
        current: bool,
        pointers: Pointers | None = None,
    ) -> StageCompleted:
        """完了。作った成果物のうち宣言したものを足し、外す成果物を外し、cursor の行き先を決める。

        結果を渡す先があれば、cursor は動かさず、渡した先の答え（ConfirmHandoff）を待つ。
        """
        spec = STAGE_SPECS[execution.id.stage]
        declared = spec.produces | spec.may_produce
        produced = tuple(p for p in evidence.products if p.kind in declared)
        if result is not None and result.unchanged:
            produced = ()
            if evidence.head is not None and evidence.dirty == ():
                produced = tuple(
                    ArtifactRef(kind, str(evidence.head))
                    for kind in sorted(spec.produces - self.artifacts.keys(), key=str)
                    if kind.committed
                )
        elif inherited := self._sent_back_commits(execution, declared, produced):
            produced += inherited
        made = {p.kind for p in produced}
        removed = tuple(
            sorted((spec.consumes & self.artifacts.keys()) - made, key=lambda k: k.value)
        )
        artifacts = (self.artifacts.keys() - set(removed)) | made
        conflicts = evidence.conflicts if execution.id.stage is _S.REBASE else self.conflicts
        handoff = spec.hands_to if current else None
        moved = (
            self._cursor_after(execution, artifacts, conflicts)
            if current and handoff is None
            else None
        )
        reviewed = (
            self.proposal_version
            if handoff in _TO_REVIEW and self.kind is TaskKind.PLANNING
            else None
        )
        shared: tuple[ArtifactRef, ...] = ()
        if handoff is Handoff.PROPOSAL:
            held = {**self.artifacts, **{p.kind: p for p in produced}}
            shared = tuple(held[k] for k in sorted(RUN_SHARED_ARTIFACTS & held.keys(), key=str))
        return StageCompleted(
            execution.id,
            produced,
            evidence.conflicts,
            removed,
            moved,
            result=result,
            handoff=handoff,
            reviewed=reviewed,
            shared=shared,
            job=self.flow.job if current and self.flow is not None else None,
            pointers=pointers if handoff is not None else None,
        )

    def _raise(
        self,
        kind: EscalationKind,
        pointers: Pointers,
        hint: Hint,
        origin: ExecutionId | None,
        reason: str = "",
        *,
        question: str | None = None,
        answer_only: bool = False,
    ) -> EscalationRaised:
        return EscalationRaised(
            kind,
            pointers,
            hint,
            task=self.id,
            origin=origin,
            reason=reason,
            question=question,
            answer_only=answer_only,
        )

    def _reported(
        self,
        execution: Execution,
        command: ReportStageResult,
        kind: EscalationKind,
        *,
        hint: Hint | None = None,
        stage_event: Event | None = None,
        reason: str = "",
    ) -> list[Event]:
        """ステージの報告から上げる。タスクの中で上げてよい種類かは EscalationRouter に聞く。"""
        assert self.kind is not None
        if why := EscalationRouter.why_not_raise(self.kind, kind):
            return self._failed(execution, command, why)
        job = self.flow.job if self.flow is not None else None
        return [
            stage_event or StageReported(execution.id, kind, reason, job),
            self._raise(kind, command.pointers, hint or Hint(), execution.id, reason),
        ]

    def _failed(self, execution: Execution, command: ReportStageResult, reason: str) -> list[Event]:
        return self._fail(execution, command.pointers, reason)

    def _fail(self, execution: Execution, pointers: Pointers, reason: str) -> list[Event]:
        """失敗。1 回はやり直し、続けて FAILURES_BEFORE_ESCALATION 回落ちたら stage-errors で上げる。"""
        events: list[Event] = [StageFailed(execution.id, reason)]
        if self.failures.get(execution.position, 0) + 1 >= FAILURES_BEFORE_ESCALATION:
            events.append(self._raise(_E.STAGE_ERRORS, pointers, Hint(), execution.id, reason))
        return events + self._follow_up(events)

    def _gate_failed(
        self, execution: Execution, command: ReportStageResult, gate: GateReport
    ) -> list[Event]:
        """Gate の項目の落ち。コードで直せない項目があれば上げ、無ければ Fix へ戻る。"""
        if kind := GateEvaluator.escalation_for(gate.failed):
            failed = [r for r in gate.failed if r.item.escalation is not None]
            items = tuple(r.item for r in failed)
            reason = "; ".join(r.reason for r in failed if r.reason)
            hint = Hint(gate_items=items)
            return self._reported(execution, command, kind, hint=hint, reason=reason)
        assert self.flow is not None
        returns_to = STAGE_SPECS[execution.id.stage].returns_to
        assert returns_to is not None
        loop = self.flow.last_index_of(returns_to, before=execution.step)
        assert loop is not None  # FlowValidator が戻り先を Gate より前に求める
        fix = Cursor(loop).enter(
            _fixer_for(returns_to, FindingTarget.IMPL), self.loop_rounds.get(loop, 1)
        )
        return [GateFailed(execution.id, gate.failed, fix, command.pointers)]

    # --- 結果の受け渡し ---

    @handles(ConfirmHandoff)
    def _confirm(self, command: ConfirmHandoff) -> list[Event]:
        """渡した先の答え。受けたら cursor を進め、受けなかったら result-refused で上げる。"""
        self._require_opened()
        execution = self.executions.get(command.execution)
        if execution is None:
            raise Rejected(f"{command.execution} という実行は無い")
        if self.stopped or execution.awaiting is None or not self._is_live(execution):
            # 止めたタスク・もう締めた（判定を締めた）・生きていないフローの実行。遅れて届いた
            # 知らせで、上げも cursor の移動もしない
            return []
        if command.refused is not None:
            reason = command.refused.strip() or "受け取る側が受けなかった"
            return [
                HandoffFailed(execution.id, reason),
                self._raise(
                    _E.RESULT_REFUSED,
                    execution.pointers or Pointers(),
                    Hint(),
                    execution.id,
                    reason,
                ),
            ]
        if execution.awaiting is Handoff.JUDGEMENT:
            raise Rejected("判定の結果を受けたことは、判定を締めた知らせ（Conclude*Round）で受ける")
        cursor = self._cursor_after(execution, self.artifacts.keys(), self.conflicts)
        events: list[Event] = [HandoffConfirmed(execution.id, cursor)]
        return events + self._follow_up(events)

    # --- 合成ステージの判定 ---

    def _judged(self, loop: StageKind, judge: ExecutionId) -> tuple[int, int]:
        """`loop` の判定を待っていて、`judge` が今のラウンドの判定の実行か。"""
        step = self.current_step
        waiting = self._awaiting_judgement()
        if step is None or step.stage is not loop or waiting is None:
            raise Rejected(f"{loop.value} の判定を待っていない")
        if waiting.id != judge:
            raise Rejected(f"{judge} は、今のラウンドの判定の実行（{waiting.id}）ではない")
        return self.cursor.step, self.cursor.round

    def _round(
        self,
        step: int,
        round: int,
        judge: ExecutionId,
        *,
        finished: bool,
        produced: tuple[ArtifactRef, ...] = (),
        raised: EscalationRaised | None = None,
        fixers: tuple[StageKind, ...] = (),
    ) -> list[Event]:
        """1 ラウンドの判定の後の行き先を決める。抜けるなら次の段、そうでなければ直す役へ。

        `fixers` が空で、`raised` が統括へ回す指摘（supervisor-finding）なら、直す役は走らず
        次のラウンドの頭へ進む。`fixers` が空でそうでなければ、直す役が 1 つの合成ステージ（DesignLoop）。
        """
        assert self.flow is not None
        composite = self.flow.steps[step].stage
        artifacts = self.artifacts.keys() | {p.kind for p in produced}
        if finished:
            cursor = _settle(self.flow, Cursor(step).next_step(), artifacts, self.conflicts)
        elif fixers:
            cursor = Cursor(step).enter(fixers[0], round)
        elif raised is not None and raised.kind is _E.SUPERVISOR_FINDING:
            head = _round_head(self.flow.steps[step], round + 1, artifacts)
            cursor = Cursor(step).enter(head, round + 1)
        else:
            cursor = Cursor(step).enter(_one(composite, _R.FIXER), round)
        events: list[Event] = [
            RoundConcluded(step, round, finished, cursor, produced, judge, fixers)
        ]
        if raised is not None:
            events.append(raised)
        return events + self._follow_up(events)

    def _findings_round(
        self,
        step: int,
        round: int,
        unresolved: tuple[FindingId, ...],
        stalled: tuple[FindingId, ...],
        *,
        routes: tuple[FindingRoute, ...],
        cause: StallCause | None,
        pointers: Pointers,
        origin: ExecutionId,
        produced: tuple[ArtifactRef, ...],
    ) -> list[Event]:
        """指摘の台帳の判定の結果から行き先を決める。

        統括へ回す指摘（supervisor）があれば上げ、停滞は上げない。無ければ停滞を上げ、open が残れば
        宛先ごとの直す役（TestFix → Fix）、無ければ抜ける。
        """
        if not set(stalled) <= set(unresolved):
            raise Rejected("停滞した指摘は、open の指摘のうちから挙げる")
        assert self.flow is not None
        composite = self.flow.steps[step].stage
        fixers = _fixers_for(composite, routes, unresolved) if unresolved else ()
        targets = {r.finding: r.target for r in routes}
        for_supervisor = tuple(f for f in unresolved if targets.get(f) is FindingTarget.SUPERVISOR)
        where, result = self._judged_context(origin, pointers)
        if for_supervisor:
            hint = Hint(finding_ids=for_supervisor)
            reason = "統括が決める指摘: " + ", ".join(str(f) for f in for_supervisor)
            raised = self._raise(_E.SUPERVISOR_FINDING, where, hint, origin, reason)
            return self._round(step, round, origin, finished=False, raised=raised, fixers=fixers)
        if stalled:
            # 停滞のエスカレーションは、判定の後に 1 回だけ
            hint = Hint(finding_ids=stalled, stall_cause=cause)
            reason = result.stall_reason if result is not None else None
            stall = self._raise(_E.STALL, where, hint, origin, reason or "")
            return self._round(step, round, origin, finished=False, raised=stall, fixers=fixers)
        if unresolved:
            return self._round(step, round, origin, finished=False, fixers=fixers)
        return self._round(step, round, origin, finished=True, produced=produced)

    @handles(ConcludeReviewRound)
    def _conclude_review(self, command: ConcludeReviewRound) -> list[Event]:
        """ReviewLoop の判定の後。"""
        self._require_live()
        step, round = self._judged(_S.REVIEW_LOOP, command.judge)
        reviewed = (ArtifactRef(_A.REVIEWED, str(command.judge)),)
        return self._findings_round(
            step,
            round,
            command.unresolved,
            command.stalled,
            routes=command.routes,
            cause=command.cause,
            pointers=command.pointers,
            origin=command.judge,
            produced=reviewed,
        )

    @handles(ConcludeGateRound)
    def _conclude_gate(self, command: ConcludeGateRound) -> list[Event]:
        """Gate の不合格の後。G- の指摘が残れば Fix へ、停滞なら上げる。

        止めた次の一手を外すのは、これだけである。残った指摘が無ければ、ReviewLoop を抜けた所
        （Gate）からもう一度走らせる。
        """
        self._require_live()
        if self.gate_failed != command.gate:
            gate = self.executions.get(command.gate)
            if gate is not None and gate.status is _X.COMPLETED and gate.id.stage is _S.GATE:
                # Gate が通った後の判定の知らせ（RecordGateResult は通ったときも出す）
                return []
            raise Rejected(f"{command.gate} は、落ちて判定を待っている Gate の実行ではない")
        return self._findings_round(
            self.cursor.step,
            self.cursor.round,
            command.unresolved,
            command.stalled,
            routes=command.routes,
            cause=None,
            pointers=command.pointers,
            origin=command.gate,
            produced=(),
        )

    @handles(ConcludeDesignRound)
    def _conclude_design(self, command: ConcludeDesignRound) -> list[Event]:
        """DesignLoop の判定の後。設計が確定したら抜け、そうでなければ Revise へ。"""
        self._require_live()
        step, round = self._judged(_S.DESIGN_LOOP, command.judge)
        if command.settled is None:
            return self._round(step, round, command.judge, finished=False)
        design = (ArtifactRef(_A.DESIGN, str(command.settled.value)),)
        return self._round(step, round, command.judge, finished=True, produced=design)

    # --- エスカレーション ---

    def _judged_context(
        self, origin: ExecutionId | None, pointers: Pointers
    ) -> tuple[Pointers, StageResult | None]:
        """判定の後に上げるエスカレーションへ添える、調べる先と判定の結果（名前の付いた規則）。

        調べる先は、判定した実行の結果と同じ所（実行器がその結果に添えたもの）。ポリシーはイベントの
        欄しか知らないので、コマンドの `pointers` は空のことが多い。空でなければ、それを使う。
        """
        execution = self.executions.get(origin) if origin is not None else None
        if execution is None:
            return pointers, None
        if pointers == Pointers() and execution.pointers is not None:
            pointers = execution.pointers
        return pointers, execution.result

    @handles(Escalate)
    def _escalate(self, command: Escalate) -> list[Event]:
        """ポリシーが上げる（設計の回答待ち・戻し続けた git の仕事）。

        理由を書いていなければ、理由と問いは判定した実行の結果（designCause）から読む。
        """
        kind = self._require_live()
        if why := EscalationRouter.why_not_raise(kind, command.kind):
            raise Rejected(why)
        where, result = self._judged_context(command.origin, command.pointers)
        judged = result.design_cause if result is not None else None
        reason = command.reason or (judged.reason if judged is not None else "")
        question = judged.question if judged is not None else None
        raised = self._raise(
            command.kind,
            where,
            command.hint,
            command.origin,
            reason,
            question=question,
            answer_only=command.answer_only,
        )
        return [raised]

    @handles(ResolveEscalation)
    def _resolve(self, command: ResolveEscalation) -> list[Event]:
        self._require_live()
        escalation = self.escalations.get(command.escalation)
        if escalation is None:
            raise Rejected(f"{command.escalation} は未処理のエスカレーションではない")
        if not command.answer.strip():
            raise Rejected("回答が空")
        # defer で止まった計画ステージは、回答を受けて同じ呼び出しから続ける
        origin = self.executions.get(escalation.origin) if escalation.origin else None
        resume = (
            origin
            if origin is not None and origin.status is _X.DEFERRED and not self._is_obsolete(origin)
            else None
        )
        events: list[Event] = [
            EscalationResolved(
                command.escalation,
                command.answer,
                escalation.kind,
                escalation.origin,
                command.question,
                resume=resume.id if resume else None,
                tool_use_id=resume.tool_use_id if resume else None,
            ),
            # 回答は出どころ付きで notes に残し、以後のステージに毎回渡す（Decision）
            NoteAdded(_decision(command.answer, command.question)),
        ]
        return events + self._follow_up(events)

    @handles(CloseEscalation)
    def _close(self, command: CloseEscalation) -> list[Event]:
        """回答以外で閉じる。今のフローで上げたものなら、そのフローは捨てて統括の置き換えを待つ。"""
        self._require_opened()
        closed = self.escalations.get(command.escalation)
        if closed is None:
            # もう閉じた（範囲の変更・止めた・応えたフロー）。Run の側から遅れて届いた知らせ
            return []
        events: list[Event] = [_closed(closed, command.reason)]
        flow = self.flow
        if flow is not None and closed.flow_version == flow.version and not self.halted:
            events += self._abandon_flow(command.reason, closing=command.escalation)
        return events

    @handles(AbandonFlow)
    def _abandon(self, command: AbandonFlow) -> list[Event]:
        """今のフローを捨てる。統括の次のフローを待つ。"""
        self._require_opened()
        if self.flow is None or self.halted or self.flow_finished:
            return []
        return self._abandon_flow(command.reason)

    def _abandon_flow(self, reason: str, closing: EventId | None = None) -> list[Event]:
        """今のフローを捨てる（名前の付いた規則）。どの道で捨てても、同じものを片付ける。

        走っている実行は止め、走らせると決めてまだ始めていない実行はやめる（StageCancelled）。
        **そのフローで上げたエスカレーションは、フローと一緒に閉じる。** 残すと、次のフローが「回答を
        待っているエスカレーションがある」で受けられず、Run の側の中継も開いたままになる（タスクの側の
        EscalationClosed を受けたポリシーが Run の側を閉じる）。`closing` は、同じコマンドでもう閉じた
        エスカレーション。
        """
        flow = self.flow
        assert flow is not None
        current = [e for e in self.executions.values() if not self._is_obsolete(e)]
        events: list[Event] = [
            StageInterrupted(e.id, _C.REQUESTED) for e in current if e.status is _X.RUNNING
        ]
        events += [StageCancelled(e.id, reason) for e in current if e.status is _X.REQUESTED]
        events += [
            _closed(e, reason)
            for e in self.escalations.values()
            if e.flow_version == flow.version and e.id != closing
        ]
        events.append(FlowAbandoned(flow.version, reason, flow.job))
        return events

    @handles(ChangeScope)
    def _change_scope(self, command: ChangeScope) -> list[Event]:
        """範囲が変わった。待っているエスカレーションは閉じ（StopTask と同じ形）、今のフローは捨てて、
        統括が組み直すのを待つ。走っているステージは終わるまで走らせ、成果物は残す。"""
        kind = self._require_live()
        if kind is not TaskKind.IMPLEMENTATION:
            raise Rejected("範囲を変えるのは実装タスクだけ")
        events: list[Event] = [_closed(e, "範囲が変わった") for e in self.escalations.values()]
        events.append(
            ScopeChanged(command.spec, command.artifacts, command.pointers, command.branch)
        )
        return events

    @handles(AddNote)
    def _add_note(self, command: AddNote) -> list[Event]:
        self._require_opened()
        return [NoteAdded(command.decision)]

    @handles(RecordBase)
    def _record_base(self, command: RecordBase) -> list[Event]:
        # 止めた・積んだタスクでも覚える（積み直しの Rebase は、積んだタスクのブランチを動かす）
        self._require_opened()
        if command.base == self.base_commit and command.tree in (None, self.code_tree):
            return []
        return [BaseRecorded(command.base, command.tree)]

    @handles(StopTask)
    def _stop(self, command: StopTask) -> list[Event]:
        if self.kind is None or self.stopped:
            # 計画にあって始めていないタスク（ストリームが無い）・もう止めた。TasksStopped を受けた
            # ポリシーは、どのタスクを始めたかを知らずに出す
            return []
        # 走っている実行だけを中断する。BeginStage を待つ実行は、止めたタスクでは始まらない
        events: list[Event] = [
            StageInterrupted(e.id, _C.STOPPED)
            for e in self.executions.values()
            if e.status is _X.RUNNING
        ]
        events += [_closed(e, command.reason) for e in self.escalations.values()]
        events.append(TaskStopped(command.reason))
        return events

    # --- apply ---

    def _close_escalation(self, escalation: EventId) -> OpenEscalation | None:
        """閉じる。defer で止まっていた実行は、回答以外で片付いたので abandoned にする。"""
        closed = self.escalations.pop(escalation, None)
        if closed is None or closed.origin is None:
            return closed
        execution = self.executions.get(closed.origin)
        if execution is not None and execution.status is _X.DEFERRED:
            self.executions[execution.id] = replace(execution, status=_X.ABANDONED)
        return closed

    def _mark(self, execution: ExecutionId, status: ExecutionStatus, **changes: Any) -> Execution:
        updated = replace(self.executions[execution], status=status, **changes)
        self.executions[execution] = updated
        if status is _X.COMPLETED:
            key = (updated.flow_version, *updated.position)
            self.completed[key] = updated.id
        return updated

    @applies(TaskOpened)
    def _on_opened(self, event: TaskOpened) -> None:
        self.kind = event.kind
        self.spec = event.spec
        self.branch = event.branch
        self.blocked_by = event.blocked_by
        self.conflict_files = event.conflicts
        self.artifacts.update({artifact.kind: artifact for artifact in event.artifacts})

    @applies(FlowAccepted)
    def _on_flow_accepted(self, event: FlowAccepted) -> None:
        if event.closes is not None:
            self._close_escalation(event.closes)
        self.flow = event.flow
        self.flow_versions = event.flow.version
        self.cursor = event.cursor
        self.gate_failed = None
        self.halted = False
        self.failures = {}
        self.send_backs = {}
        self.conflicts = None

    @applies(FlowRejected)
    def _on_flow_rejected(self, event: FlowRejected) -> None:
        pass

    @applies(StageRequested)
    def _on_requested(self, event: StageRequested) -> None:
        assert self.flow is not None
        self.executions[event.execution] = Execution(event.execution, event.step, self.flow.version)

    @applies(StageStarted)
    def _on_started(self, event: StageStarted) -> None:
        before = self.executions[event.execution].status
        self._mark(
            event.execution,
            _X.RUNNING,
            start_commit=event.start_commit,
            session=event.session,
            interrupted_by=None,
            end_reason=None,
            resumed_from=before if before in _RESUMED_FROM else None,
        )

    @applies(ExecutionRestarted)
    def _on_restarted(self, event: ExecutionRestarted) -> None:
        self._mark(event.execution, _X.RESTARTED, end_reason=event.reason)

    @applies(StageCompleted)
    def _on_completed(self, event: StageCompleted) -> None:
        execution = self._mark(
            event.execution,
            _X.COMPLETED,
            awaiting=event.handoff,
            pointers=event.pointers,
            result=event.result,
        )
        for kind in event.removed:
            self.artifacts.pop(kind, None)
        self.artifacts.update({artifact.kind: artifact for artifact in event.produced})
        if event.result is not None and event.result.proposal is not None:
            self.proposal_version = event.result.proposal.design
        if self._is_obsolete(execution):
            return
        self.failures.pop(execution.position, None)
        self.send_backs.pop(execution.position, None)
        if execution.id.stage is _S.REBASE:
            self.conflicts = event.conflicts
        if event.cursor is not None:
            self.cursor = event.cursor

    @applies(HandoffConfirmed)
    def _on_handoff_confirmed(self, event: HandoffConfirmed) -> None:
        self.executions[event.execution] = replace(self.executions[event.execution], awaiting=None)
        if event.cursor is not None:
            self.cursor = event.cursor

    @applies(HandoffFailed)
    def _on_handoff_failed(self, event: HandoffFailed) -> None:
        execution = self._mark(event.execution, _X.REFUSED, awaiting=None, end_reason=event.reason)
        key = (execution.flow_version, *execution.position)
        if self.completed.get(key) == execution.id:
            del self.completed[key]

    @applies(StageFailed)
    def _on_failed(self, event: StageFailed) -> None:
        execution = self._mark(event.execution, _X.FAILED, end_reason=event.reason)
        if not self._is_obsolete(execution):
            self.failures[execution.position] = self.failures.get(execution.position, 0) + 1

    @applies(StageSentBack)
    def _on_sent_back(self, event: StageSentBack) -> None:
        execution = self._mark(event.execution, _X.SENT_BACK, end_reason=event.reason)
        if not self._is_obsolete(execution):
            self.send_backs[execution.position] = self.send_backs.get(execution.position, 0) + 1

    @applies(StageInterrupted)
    def _on_interrupted(self, event: StageInterrupted) -> None:
        self._mark(event.execution, _X.INTERRUPTED, interrupted_by=event.cause)

    @applies(StageDeferred)
    def _on_deferred(self, event: StageDeferred) -> None:
        self._mark(event.execution, _X.DEFERRED, tool_use_id=event.tool_use_id)

    @applies(StageReported)
    def _on_reported(self, event: StageReported) -> None:
        reason = event.reason or event.kind.value
        execution = self._mark(event.execution, _X.REPORTED, end_reason=reason)
        if not self._is_obsolete(execution):
            self.failures.pop(execution.position, None)

    @applies(GateFailed)
    def _on_gate_failed(self, event: GateFailed) -> None:
        # 調べる先は、G- の指摘が停滞したときの上げに添える
        self._mark(
            event.execution, _X.COMPLETED, pointers=event.pointers, gate_failures=event.failed
        )
        self.cursor = event.cursor
        self.gate_failed = event.execution

    @applies(RoundConcluded)
    def _on_round_concluded(self, event: RoundConcluded) -> None:
        self.gate_failed = None
        self.cursor = event.cursor
        self.fixers = event.fixers
        if event.judge is not None and (judge := self.executions.get(event.judge)) is not None:
            self.executions[judge.id] = replace(judge, awaiting=None)
        if event.finished:
            self.artifacts.update({artifact.kind: artifact for artifact in event.produced})
            self.loop_rounds[event.step] = event.round

    @applies(WorktreeReady)
    def _on_worktree_ready(self, event: WorktreeReady) -> None:
        pass

    @applies(BranchRebased)
    def _on_rebased(self, event: BranchRebased) -> None:
        pass

    @applies(BaseRecorded)
    def _on_base_recorded(self, event: BaseRecorded) -> None:
        self.base_commit = event.base
        if event.tree is not None:
            self.code_tree = event.tree

    @applies(FlowFinished)
    def _on_flow_finished(self, event: FlowFinished) -> None:
        self.finished_version = event.version

    @applies(FlowAbandoned)
    def _on_flow_abandoned(self, event: FlowAbandoned) -> None:
        self.halted = True

    @applies(StageCancelled)
    def _on_cancelled(self, event: StageCancelled) -> None:
        self._mark(event.execution, _X.ABANDONED, end_reason=event.reason)

    @applies(TaskGated)
    def _on_gated(self, event: TaskGated) -> None:
        self.gated = True

    @applies(TaskStopped)
    def _on_stopped(self, event: TaskStopped) -> None:
        self.stopped = True

    @applies(EscalationRaised)
    def _on_escalated(self, event: EscalationRaised) -> None:
        version = self.flow.version if self.flow is not None else None
        self.escalations[self.event_id] = OpenEscalation(
            self.event_id, event.kind, event.origin, version, event.reason, event.question
        )
        origin = self.executions.get(event.origin) if event.origin is not None else None
        if origin is not None and not self._is_obsolete(origin):
            # 上げた後に解けたら、同じ位置は 1 回目から数え直す
            self.failures.pop(origin.position, None)
            self.send_backs.pop(origin.position, None)

    @applies(EscalationResolved)
    def _on_resolved(self, event: EscalationResolved) -> None:
        # defer で止まった実行は deferred のまま残り、回答のファイルを書いた反応の ResumeStage で続く
        self.escalations.pop(event.escalation, None)

    @applies(EscalationClosed)
    def _on_closed(self, event: EscalationClosed) -> None:
        self._close_escalation(event.escalation)

    @applies(ScopeChanged)
    def _on_scope_changed(self, event: ScopeChanged) -> None:
        # 待っていたエスカレーションは、同じコマンドの EscalationClosed で閉じてある
        self.spec = event.spec
        if event.branch is not None:
            self.branch = event.branch
        self.artifacts.update({artifact.kind: artifact for artifact in event.artifacts})
        self.flow = None
        self.cursor = Cursor()
        self.finished_version = None
        self.gate_failed = None
        self.halted = False
        self.gated = False

    @applies(NoteAdded)
    def _on_note(self, event: NoteAdded) -> None:
        self.notes.append(event.decision)


def _closed(escalation: OpenEscalation, reason: str) -> EscalationClosed:
    """回答以外で閉じた。どの上げを閉じたかをポリシーが見分けられるように、種類と起きた実行を載せる。"""
    return EscalationClosed(escalation.id, reason, kind=escalation.kind, origin=escalation.origin)


def _check(check: EvidenceCheck, evidence: Evidence) -> _Verdict:
    """決定的なステージが期待する証拠を、集めた証拠と照らす（StageSpec の expects）。"""
    if check is EvidenceCheck.VERIFY_FAILS:
        holds = any(not outcome.passed for outcome in evidence.verify)
    elif check is EvidenceCheck.RED_AND_CLEAN:
        # 種類の無い結果（種類を付ける前の証拠）は、タスクのテストとして読む
        tests = [o for o in evidence.verify if o.kind is not VerifyKind.QUICK_CHECKS]
        quick = [o for o in evidence.verify if o.kind is VerifyKind.QUICK_CHECKS]
        holds = any(not o.passed for o in tests) and all(o.passed for o in quick)
    elif check is EvidenceCheck.VERIFY_PASSES:
        # 0 件なら通す。計画はラン共通の軽い検査・回帰テストを空にしてよい（schemas の
        # quickChecks・regressionTests に minItems は無い）。空で落とすと、どのタスクも積めなくなる
        holds = all(outcome.passed for outcome in evidence.verify)
    elif check is EvidenceCheck.UNION_KEPT:
        if evidence.union is None:
            return _Verdict.MISSING
        holds = evidence.union.passed
    elif check is EvidenceCheck.GATE_PASSES:
        if evidence.gate is None:
            return _Verdict.MISSING
        holds = evidence.gate.passed
    elif check is EvidenceCheck.OWN_COMMITS:
        if evidence.commits is None:
            return _Verdict.MISSING
        holds = has_own_commits(evidence.commits)
    else:
        raise AssertionError(f"照らし方を書いていない: {check}")
    return _Verdict.HOLDS if holds else _Verdict.MISMATCH


def _session_lost(evidence: Evidence) -> bool:
    """`--resume` で起こした claude が、続けるセッションを開けなかったか。

    印は、init を出さずに自分で終わり、ターンを 1 つも進めていないこと。claude 2.1.288 は
    `error_during_execution`・`num_turns: 0` の result を返して終わる。result が無ければターンの数は
    見ない。init を出した後に落ちたのは続けた後で落ちたので、作り直すとその実行の仕事を捨てる。
    defer の再開では止めた呼び出しが init より前に走るので、もう一度 defer すると init を出さずに
    終わる。init の前にこちらが止めたのでは、セッションが在ったかが分からない。標準エラーと
    `errors` の文言は版で変わりうるので見ない。フックに止められ続けて打ち切ったのなら、続けられ
    なかったのではない。
    """
    return (
        evidence.resumed
        and not evidence.initialized
        and not evidence.stopped_by_us
        and evidence.deferred is None
        and (evidence.num_turns is None or evidence.num_turns == 0)
        and not cut_off_by_denials(evidence.hook_denials)
    )


def _missing(spec: StageSpec) -> str:
    """期待した証拠が無かった理由（失敗の理由）。"""
    if spec.expects is EvidenceCheck.OWN_COMMITS:
        return "タスクのブランチの根元が無く、根元から上のコミットを数えられない"
    return f"{spec.kind.value} の結果が無い"


def _mismatch(check: EvidenceCheck, evidence: Evidence) -> str:  # noqa: PLR0911  証拠の種類ごとの分岐
    """期待した証拠と食い違った所（報告の中身）。本文や出力そのものは載せない。"""
    if check is EvidenceCheck.OWN_COMMITS:
        return "根元から上のコミットが無い（0 件）。ブランチは動かしていない"
    if check is EvidenceCheck.UNION_KEPT and evidence.union is not None:
        files = [f.path for f in evidence.union.files if not f.kept_both]
        return "両側の変更を残していない: " + ", ".join(files)
    if check in (EvidenceCheck.UNION_KEPT, EvidenceCheck.GATE_PASSES) and (
        evidence.union is None and evidence.gate is None
    ):
        return f"{check.value} の結果が無い"
    if check is EvidenceCheck.GATE_PASSES and evidence.gate is not None:
        return "完了チェックが落ちた: " + ", ".join(r.item.value for r in evidence.gate.failed)
    failed = [str(o.command) for o in evidence.verify if not o.passed]
    if check is EvidenceCheck.RED_AND_CLEAN:
        quick = [
            str(o.command)
            for o in evidence.verify
            if o.kind is VerifyKind.QUICK_CHECKS and not o.passed
        ]
        if quick:
            return "軽い検査が落ちた: " + ", ".join(quick)
        if all(o.kind is VerifyKind.QUICK_CHECKS for o in evidence.verify):
            return "タスクのテストが無く、実装の前に落ちることを確かめられない"
        return "検証コマンドが実装の前に全部通った"
    if check is EvidenceCheck.VERIFY_FAILS:
        return "検証コマンドが実装の前に全部通った"
    return "検証コマンドが落ちた: " + ", ".join(failed)


def _describe_failures(failed: Sequence[VerifyResult]) -> str:
    """落ちた検証コマンドごとに、終了コードと出力の末尾を並べる（差し戻し・上げの理由）。"""
    return "\n".join(
        f"`{result.command}` が終了コード {result.exit_code} で落ちた\n{result.tail}".rstrip()
        for result in failed
    )
