"""実行器が 1 つの実行に使う事実のスナップショット（`StageContext`）を、集約から組む。

集約を読み書きするのはメインループだけなので、実行器はメインループのスレッドで呼ばれたときに
ここで写し取り、別のスレッドへはスナップショットだけを渡す。ここに書くのは「どの集約のどの欄を
写すか」だけで、何を走らせるか・通すかは決めない。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...domain.aggregates.base import Aggregate
from ...domain.aggregates.design import Design
from ...domain.aggregates.review_ledger import ReviewLedger
from ...domain.aggregates.run import Run
from ...domain.aggregates.stack import Stack
from ...domain.aggregates.task import ExecutionStatus, StartMode, Task
from ...domain.events.run import RunStarted
from ...domain.flow.flow import FlowStep
from ...domain.stages.catalog import STAGE_SPECS, StageSpec
from ...domain.stages.kinds import StepArgument
from ...domain.value_objects.artifact_kind import ArtifactKind
from ...domain.value_objects.artifact_ref import ArtifactRef
from ...domain.value_objects.branch_name import BranchName
from ...domain.value_objects.commit_sha import CommitSha
from ...domain.value_objects.decision import Decision
from ...domain.value_objects.execution_id import ExecutionId
from ...domain.value_objects.finding_id import FindingId
from ...domain.value_objects.git_job import GitJob
from ...domain.value_objects.glob_pattern import DEFAULT_TEST_GLOBS, GlobPattern
from ...domain.value_objects.model_class import ModelClasses
from ...domain.value_objects.pr_number import PrNumber
from ...domain.value_objects.session_id import SessionId
from ...domain.value_objects.stack_entry import StackEntry
from ...domain.value_objects.stage_kind import StageKind
from ...domain.value_objects.stream_id import StreamId
from ...domain.value_objects.task_id import TaskId
from ...domain.value_objects.task_spec import TaskSpec
from ...domain.value_objects.task_status import TaskStatus
from ...domain.value_objects.verify_command import VerifyCommand
from ...infra.paths import RunPaths

if TYPE_CHECKING:
    from ..driving.mainloop import Delivery


#: 実行をどう始めるか。どれにするかは Task が決める（`Task.how_to_start`）
ResumeMode = StartMode


@dataclass(frozen=True)
class RunSetting:
    """ランの間変わらない値。組み立ての根が、起動時の引数とリポジトリごとの設定から作る。"""

    paths: RunPaths
    #: 対象リポジトリの手元の checkout（worktree を足す元。ステージには書かせない）
    repository: Path
    #: ランの base
    base: BranchName
    instruction: str
    #: リポジトリごとの設定の軽い検査・回帰テスト（ブリーフに載せる。ランの値は計画が決める）
    quick_checks: tuple[VerifyCommand, ...] = ()
    regression_tests: tuple[VerifyCommand, ...] = ()
    test_globs: tuple[GlobPattern, ...] = DEFAULT_TEST_GLOBS
    protected_globs: tuple[GlobPattern, ...] = ()
    #: テストが要らないパス（TestGen を置かないフローで、Gate が変更をここに収まるか見る）
    untested_globs: tuple[GlobPattern, ...] = ()
    #: ランが記録したクラスごとのモデルと effort
    models: ModelClasses = field(default_factory=ModelClasses.default)


@dataclass(frozen=True)
class StagePrompt:
    #: 標準入力から渡すプロンプト
    text: str
    #: `--append-system-prompt` に置く、破ると取り返しがつかない決まり
    system_append: str | None = None


@dataclass(frozen=True)
class GateFacts:
    """Gate の証拠のうち、集約から写すもの（GateEvidence の残りは git と検証コマンドから集める）。"""

    #: 指摘の台帳で open の指摘
    open_findings: tuple[FindingId, ...]
    #: フローの最後の ReviewLoop の、最後のラウンドで reviewers に挙げたレビューと、走り終えたもの
    reviewers_expected: tuple[StageKind, ...]
    reviewers_completed: tuple[StageKind, ...]
    has_test_gen: bool


@dataclass(frozen=True)
class TaskRow:
    """概要 PR のタスクの一覧の 1 行。"""

    task: TaskId
    title: str
    status: TaskStatus
    pr: PrNumber | None = None


@dataclass(frozen=True)
class WaitingRow:
    """概要 PR の回答待ちの 1 行。"""

    #: 上げたタスク。ラン全体のものは None
    task: TaskId | None
    kind: str
    reason: str = ""


@dataclass(frozen=True)
class OverviewFacts:
    """RefreshOverview が概要 PR のマーカーを埋める材料（数と状態は毎回ここから組む）。"""

    tasks: tuple[TaskRow, ...] = ()
    #: 回答を待っているエスカレーション
    waiting: tuple[WaitingRow, ...] = ()
    #: 計画が決めたことと、回答で決めたこと
    decisions: tuple[str, ...] = ()
    notes: tuple[Decision, ...] = ()
    deferrals: tuple[str, ...] = ()


@dataclass(frozen=True)
class StackFacts:
    overview: StackEntry | None = None
    #: 積んだタスク（下から。概要 PR は入れない）
    entries: tuple[StackEntry, ...] = ()


@dataclass(frozen=True)
class TargetFacts:
    """git 管理タスクの仕事の相手のタスク（積む・切る）の中身。"""

    title: str | None = None
    pr_body: ArtifactRef | None = None


@dataclass(frozen=True)
class StageContext:
    execution: ExecutionId
    #: Flow.steps の段と、ステージの cwd（絶対パス。CutBranch では、切る worktree）。プロンプトの
    #: 組み立て（`app/stages/prompts/`）が読むのはこの 3 つだけで、位置で渡されることがあるので先頭に置く
    step: FlowStep
    tree: Path
    #: Flow.steps の添字
    step_index: int = 0
    artifacts: Mapping[ArtifactKind, ArtifactRef] = field(default_factory=dict)
    #: 実装タスクの中身（git 管理タスクなら、仕事の相手のタスクの中身）
    task_spec: TaskSpec | None = None
    branch: BranchName | None = None
    #: ステージが手を入れるタスクのブランチの根元（`Task.base_commit`。git 管理タスクなら仕事の相手の
    #: もの）。そのブランチだけにあるコミットは、ここから HEAD まで
    base_commit: CommitSha | None = None
    job: GitJob | None = None
    #: そのタスクに届いた回答（決めたこと）
    notes: tuple[Decision, ...] = ()
    #: 使うセッション。begin では続けるセッション（無ければ実行器が新しく立てる）、run では実行のもの
    session: SessionId | None = None
    #: そのセッションが、この実行の前にもう始まっているか（`--resume` で続ける）
    session_started: bool = False
    start_commit: CommitSha | None = None
    #: begin で、HEAD を取る前に worktree を戻すコミット（`Task.reset_before_start`）
    reset_to: CommitSha | None = None
    resume: ResumeMode = ResumeMode.FRESH
    tool_use_id: str | None = None
    #: WriteScope.LISTED のステージが書いてよいファイル
    listed: tuple[str, ...] = ()
    #: 同じフローの前の Rebase が衝突したファイル（CheckUnion が見る）
    conflicts: tuple[str, ...] = ()
    #: ラン共通の軽い検査・回帰テスト（最後に反映した計画のもの）
    run_quick_checks: tuple[VerifyCommand, ...] = ()
    run_regression_tests: tuple[VerifyCommand, ...] = ()
    #: 軽い検査の baseline を取った実行（同じ位置の最初の試み）。`checks_after` のステージだけ
    baseline_of: ExecutionId | None = None
    #: 実行の状態（begin・run の呼び直しで、もう済んだ実行を走らせないために見る）
    status: ExecutionStatus | None = None
    gate: GateFacts | None = None
    stack: StackFacts = StackFacts()
    target: TargetFacts = TargetFacts()
    overview: OverviewFacts = OverviewFacts()

    @property
    def spec(self) -> StageSpec:
        return STAGE_SPECS[self.execution.stage]

    @property
    def task(self) -> TaskId:
        return self.execution.task


def _get(aggregates: Mapping[StreamId, Aggregate], stream: StreamId, kind: type) -> object | None:
    found = aggregates.get(stream)
    return found if isinstance(found, kind) else None


def _number(task: TaskId) -> tuple[int, str]:
    digits = "".join(ch for ch in task.value if ch.isdigit())
    return (int(digits) if digits else 0, task.value)


def _gate_facts(task: Task, ledger: ReviewLedger | None, step_index: int) -> GateFacts:
    flow = task.flow
    assert flow is not None
    open_findings = tuple(f.id for f in ledger.open_findings) if ledger is not None else ()
    expected: tuple[StageKind, ...] = ()
    completed: list[StageKind] = []
    loop = flow.last_index_of(StageKind.REVIEW_LOOP, before=step_index)
    if loop is not None:
        step = flow.steps[loop]
        round = task.loop_rounds.get(loop, 1)
        if StepArgument.REVIEWERS in STAGE_SPECS[step.stage].arguments and step.reviewers:
            expected = step.reviewers.for_round(round)
        for kind in expected:
            done = task.completed.get((flow.version, loop, kind, round))
            if done is not None and task.executions[done].awaiting is None:
                completed.append(kind)
    # TestGen の無いフローでも、TestFix がテストを書いたら、そのコミットを基準に tests-unchanged を見る
    has_test_gen = (
        any(s.stage is StageKind.TEST_GEN for s in flow.steps)
        or ArtifactKind.TESTS in task.artifacts
    )
    return GateFacts(open_findings, expected, tuple(completed), has_test_gen)


def _overview_facts(
    run: Run | None, stack: Stack | None, design: Design | None, planning: Task | None
) -> OverviewFacts:
    prs = {entry.task: entry.pr for entry in stack.entries} if stack is not None else {}
    rows: list[TaskRow] = []
    waiting: list[WaitingRow] = []
    if run is not None:
        for entry in sorted(run.tasks.values(), key=lambda e: _number(e.id)):
            if not entry.is_implementation:
                continue
            title = entry.spec.title if entry.spec is not None else entry.id.value
            rows.append(TaskRow(entry.id, title, entry.status, prs.get(entry.id)))
        waiting = [WaitingRow(e.task, e.kind.value, e.reason) for e in run.escalations.values()]
    return OverviewFacts(
        tasks=tuple(rows),
        waiting=tuple(waiting),
        decisions=design.decisions if design is not None else (),
        notes=tuple(planning.notes) if planning is not None else (),
        deferrals=design.deferrals if design is not None else (),
    )


def snapshot(
    aggregates: Mapping[StreamId, Aggregate],
    execution: ExecutionId,
    setting: RunSetting,
    *,
    agent_started: bool = False,
) -> StageContext:
    """`execution` を走らせる事実を写す。Task に無い実行なら KeyError。

    `agent_started` は、この実行の claude をもう起こした跡があるか（`Task.how_to_start` に渡す）。
    """
    task = _get(aggregates, StreamId.task(execution.task), Task)
    if not isinstance(task, Task) or execution not in task.executions or task.flow is None:
        raise KeyError(f"{execution} は Task に無い")
    run = _get(aggregates, StreamId.run(), Run)
    stack = _get(aggregates, StreamId.stack(), Stack)
    design = _get(aggregates, StreamId.design(), Design)
    ledger = _get(aggregates, StreamId.review(execution.task), ReviewLedger)
    planning = _get(aggregates, StreamId.task(TaskId.planning()), Task)
    assert run is None or isinstance(run, Run)
    assert stack is None or isinstance(stack, Stack)
    assert design is None or isinstance(design, Design)
    assert ledger is None or isinstance(ledger, ReviewLedger)
    assert planning is None or isinstance(planning, Task)

    record = task.executions[execution]
    job = task.flow.job
    # git 管理タスクは、仕事の相手のタスクの worktree で動く（概要 PR の仕事は概要ブランチの worktree）
    where = job.task if job is not None and job.task is not None else execution.task
    target_task = _get(aggregates, StreamId.task(where), Task) if job is not None else None
    assert target_task is None or isinstance(target_task, Task)
    paths = setting.paths

    session: SessionId | None
    resume = ResumeMode.FRESH
    if record.status is ExecutionStatus.REQUESTED:
        session = task.session_to_continue(execution, task.flow.steps[record.step].fresh_session)
        started = session is not None
    else:
        start = task.how_to_start(execution, agent_started=agent_started)
        session, started, resume = record.session, start.resume_session, start.mode

    step = task.flow.steps[record.step]
    spec_source = target_task if target_task is not None else task
    return StageContext(
        execution=execution,
        step_index=record.step,
        step=step,
        tree=paths.tree_of(where),
        artifacts=dict(task.artifacts),
        task_spec=spec_source.spec,
        branch=task.branch,
        base_commit=spec_source.base_commit,
        job=job,
        notes=tuple(task.notes),
        session=session,
        session_started=started,
        start_commit=record.start_commit,
        reset_to=task.reset_before_start(execution),
        resume=resume,
        tool_use_id=record.tool_use_id,
        listed=task.conflicts or task.conflict_files,
        conflicts=task.conflicts or (),
        baseline_of=task.baseline_of(execution)
        if STAGE_SPECS[execution.stage].checks_after
        else None,
        run_quick_checks=run.quick_checks if run is not None else (),
        run_regression_tests=run.regression_tests if run is not None else (),
        status=record.status,
        gate=_gate_facts(task, ledger, record.step) if execution.stage is StageKind.GATE else None,
        stack=StackFacts(stack.overview, tuple(stack.entries))
        if stack is not None
        else StackFacts(),
        target=TargetFacts(
            spec_source.spec.title if spec_source.spec is not None else None,
            spec_source.artifacts.get(ArtifactKind.PR_BODY),
        ),
        overview=_overview_facts(run, stack, design, planning),
    )


def run_setting(paths: RunPaths, history: Iterable[Delivery], **repository: Any) -> RunSetting:
    """確定したイベントの列の RunStarted から、ランの間変わらない値を組む。`repository` はリポジトリ
    ごとの設定（`quick_checks`・`regression_tests`・`test_globs`・`protected_globs`・`untested_globs`）。まだ無ければ LookupError。"""
    for delivery in history:
        if isinstance(delivery.event, RunStarted):
            started = delivery.event
            return RunSetting(
                paths=paths,
                repository=Path(started.repository.value),
                base=started.base,
                instruction=started.instruction.value,
                models=started.models or ModelClasses.default(),
                **repository,
            )
    raise LookupError("RunStarted がまだ無い")
