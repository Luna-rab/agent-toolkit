"""集約どうしのつなぎ目（イベント → 次のコマンド）を、イベントの欄だけで組めるかを確かめる。

次の段のポリシーは状態を持たず、受けたイベントとその id（どのストリームか）だけからコマンドを返す。
ここではポリシーを仮に書かず、つなぎ目ごとに「このイベントから、このコマンドを、この欄で組む」を
表（`SEAMS`）にし、主な流れを本物の集約で通す。表の `build` はイベントとその id しか受け取らない
ので、流れが進めば、イベントの欄だけで組めたことになる。組んだコマンドは、どれも拒まれずに受け
られること（拒否は出した者の取り違えだけ）も確かめる。次の段は、この表をポリシーとして書き写せばよい。

表の外の「外からの入力」は、流れごとに台本として渡す: ステージの実行（実行器）・LLM の統括
（ラン統括と実装タスクの統括）・`/autodev` の CLI・driver（パニックと起動時の後始末）。計画タスクと
git 管理タスクの統括はプログラムで、決まった並びを組むだけなので、表に入れる（`by="supervisor"`）。
"""

from __future__ import annotations

import itertools
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import pytest
from autodev_samples import stage_result

from autodev.domain.aggregates.base import Aggregate, Rejected
from autodev.domain.aggregates.design import DESIGN_WAITS, Design
from autodev.domain.aggregates.questions import Questions
from autodev.domain.aggregates.review_ledger import JudgeCapability, ReviewLedger
from autodev.domain.aggregates.run import Run
from autodev.domain.aggregates.stack import Stack
from autodev.domain.aggregates.task import ExecutionStatus, Task
from autodev.domain.commands.base import Command
from autodev.domain.commands.design import (
    DiscardProposal,
    MarkAmbiguous,
    MarkReverted,
    ProposeDesign,
    ResumeDesign,
    ReviseDesign,
    SettleDesign,
)
from autodev.domain.commands.questions import AnswerQuestion, PostQuestion, WithdrawQuestions
from autodev.domain.commands.review_ledger import (
    CarryFinding,
    CommentFinding,
    CountFix,
    JudgeFinding,
    RaiseFinding,
    RecordFindings,
    RecordGateResult,
    RecordJudgement,
    TrackProposal,
)
from autodev.domain.commands.run import (
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
    RequestReplan,
    ResumeRun,
    ReturnToQueue,
    StartReadyTasks,
    StartRun,
    StartTask,
    StopTasks,
    UpdateTaskStatus,
)
from autodev.domain.commands.stack import (
    AppendEntry,
    DropGitJob,
    EnqueueGitJob,
    EnqueueStack,
    FinishGitJob,
    PauseStacking,
    RecordConflict,
    RecordOverview,
    RejectRequest,
    ResumeStacking,
    RetryGitJob,
    RetryIntegration,
    TakeNextGitJob,
    UnstackFrom,
    WithdrawRequest,
)
from autodev.domain.commands.task import (
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
    MarkInterrupted,
    OpenTask,
    RecordBase,
    ReportStageResult,
    ResolveEscalation,
    ResumeInterrupted,
    ResumeStage,
    StopTask,
)
from autodev.domain.events.base import Event
from autodev.domain.events.design import (
    DesignAmbiguous,
    DesignProposed,
    DesignReverted,
    DesignRevisionStarted,
    DesignRoundsExhausted,
    DesignSettled,
)
from autodev.domain.events.questions import QuestionAnswered, QuestionPosted, QuestionWithdrawn
from autodev.domain.events.review_ledger import (
    FindingCarried,
    FindingsEvaluated,
    FixCounted,
    ResultReceived,
    ResultRefused,
)
from autodev.domain.events.run import (
    AllTasksSettled,
    AnswerRecorded,
    EscalationAnswered,
    EscalationRaised,
    ReplanRequested,
    RunFinished,
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
)
from autodev.domain.events.stack import (
    GitJobDropped,
    GitJobFinished,
    GitJobQueued,
    GitJobRetried,
    GitJobTaken,
    GitJobWithdrawn,
    IntegrationFailed,
    IntegrationRetried,
    RebaseConflicted,
    StackCutBack,
    StackingResumed,
    TaskStacked,
)
from autodev.domain.events.task import (
    BranchRebased,
    EscalationClosed,
    EscalationResolved,
    FlowAbandoned,
    FlowAccepted,
    FlowFinished,
    GateFailed,
    NoteAdded,
    ScopeChanged,
    StageCompleted,
    StageReported,
    StageRequested,
    StageStarted,
    TaskGated,
    WorktreeReady,
)
from autodev.domain.flow.flow import FlowStep, Reviewers
from autodev.domain.flow.standard import git_job_flow, planning_flow
from autodev.domain.services.escalation_router import task_of_stream
from autodev.domain.stages.catalog import STAGE_SPECS
from autodev.domain.stages.kinds import Handoff, StageMode
from autodev.domain.supervision import wake_for
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.artifact_ref import ArtifactRef
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.commit_sha import CommitSha
from autodev.domain.value_objects.decision import Decision
from autodev.domain.value_objects.decision_origin import DecisionOrigin
from autodev.domain.value_objects.deferred_call import DeferredCall
from autodev.domain.value_objects.design_cause import DesignCause
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.evidence import Evidence
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.finding_id import FindingId
from autodev.domain.value_objects.finding_origin import FindingOrigin
from autodev.domain.value_objects.finding_route import FindingRoute
from autodev.domain.value_objects.finding_status import FindingStatus
from autodev.domain.value_objects.flow_ending import FlowEnding
from autodev.domain.value_objects.gate_item import GateItem
from autodev.domain.value_objects.gate_item_result import GateItemResult
from autodev.domain.value_objects.gate_report import GateReport
from autodev.domain.value_objects.git_job_kind import GitJobKind
from autodev.domain.value_objects.git_job_outcome import GitJobOutcome
from autodev.domain.value_objects.hint import Hint
from autodev.domain.value_objects.instruction import Instruction
from autodev.domain.value_objects.interrupt_cause import InterruptCause
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.limits import MAX_JOB_RETURNS
from autodev.domain.value_objects.parallel_limit import ParallelLimit
from autodev.domain.value_objects.pointers import Pointers
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.rating import Rating
from autodev.domain.value_objects.repository import Repository
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.session_id import SessionId
from autodev.domain.value_objects.stack_entry import StackEntry
from autodev.domain.value_objects.stage_exit import StageExit
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_kind import TaskKind
from autodev.domain.value_objects.task_spec import TaskSpec
from autodev.domain.value_objects.task_status import TaskStatus
from autodev.domain.value_objects.union_file_verdict import UnionFileVerdict
from autodev.domain.value_objects.union_verdict import UnionVerdict
from autodev.domain.value_objects.verify_command import VerifyCommand
from autodev.domain.value_objects.verify_result import VerifyResult

S = StageKind
J = GitJobKind
E = EscalationKind
PLANNING = TaskId.planning()
GIT = TaskId.git()
NAME = RunName("add-cache")
BASE = BranchName("main")
SESSION = SessionId("0f8fad5b-d9cb-469f-a165-70867728950e")
RUN_SUPERVISOR = Issuer.run_supervisor(SESSION)
HEAD = CommitSha("a" * 40)

#: 組むコマンド（クラスと、command_id・issuer を除く欄）
Draft = tuple[type[Command], dict[str, Any]]


def C(cls: type[Command], **fields: Any) -> Draft:
    return (cls, fields)


def task_of(source: EventId) -> TaskId:
    """イベントの id から、そのイベントを出したタスク（task/<TaskId> のストリーム）。"""
    return task_of_stream(source.stream)


def ledger_of(task: TaskId) -> StreamId:
    return JudgeCapability.ledger_of(task)


def is_task_stream(source: EventId) -> bool:
    return source.stream.is_task


def is_git_stream(source: EventId) -> bool:
    return source.stream == StreamId.task(GIT)


# =====================================================================================
# 表: イベント → コマンド。build はイベントとその id だけを受け取る
# =====================================================================================

# --- Run のイベント ---


def start_planning_and_git(e: RunStarted, src: EventId) -> list[Draft]:
    return [
        C(StartTask, task=PLANNING),
        C(StartTask, task=GIT),
        C(
            EnqueueGitJob,
            kind=J.CUT_OVERVIEW,
            task=PLANNING,
            branch=BranchName.overview(e.name),
            base=e.base,
        ),
    ]


def open_task(e: TaskStarted, src: EventId) -> list[Draft]:
    if e.reopened:
        return []
    return [
        C(
            OpenTask,
            task=e.task,
            kind=e.kind,
            spec=e.spec,
            artifacts=e.artifacts,
            blocked_by=e.blocked_by,
            branch=e.branch,
            conflicts=e.conflicts,
        )
    ]


def rescope_reopened(e: TaskStarted, src: EventId) -> list[Draft]:
    if not e.reopened:
        return []
    assert e.spec is not None
    return [
        C(
            ChangeScope,
            task=e.task,
            spec=e.spec,
            artifacts=e.artifacts,
            pointers=Pointers(),
            branch=e.branch,
        )
    ]


def cut_task_branch(e: TaskStarted, src: EventId) -> list[Draft]:
    if e.kind is not TaskKind.IMPLEMENTATION:
        return []
    return [C(EnqueueGitJob, kind=J.CUT_TASK, task=e.task, branch=e.branch)]


def apply_first_plan(e: SettledPlanRecorded, src: EventId) -> list[Draft]:
    # 再計画（replan）なら、ラン統括を起こして apply-plan を返させる（表の外）
    return [] if e.replan else [C(ApplyPlan, design=e.proposal.design)]


def start_ready(e: Event, src: EventId) -> list[Draft]:
    return [C(StartReadyTasks)]


#: 並列の上限に数える状態（TaskScheduler.OCCUPYING）
OCCUPYING = frozenset({TaskStatus.RUNNING, TaskStatus.ESCALATED})


def start_ready_after_slot_freed(e: TaskStatusChanged, src: EventId) -> list[Draft]:
    if e.from_status not in OCCUPYING or e.to_status in OCCUPYING:
        return []
    return [C(StartReadyTasks)]


def write_overview(e: TasksPlanned, src: EventId) -> list[Draft]:
    return [C(EnqueueGitJob, kind=J.REWRITE_OVERVIEW if e.replan else J.OPEN_OVERVIEW)]


def change_scope(e: TasksPlanned, src: EventId) -> list[Draft]:
    specs = {task.id: task.spec for task in e.tasks}
    return [
        C(ChangeScope, task=task, spec=specs[task], artifacts=e.artifacts, pointers=Pointers())
        for task in sorted(e.scope_changed, key=str)
    ]


def withdraw_rescoped(e: TasksPlanned, src: EventId) -> list[Draft]:
    return [C(WithdrawRequest, task=task) for task in sorted(e.withdraw, key=str)]


def carry_findings(e: TasksPlanned, src: EventId) -> list[Draft]:
    return [
        C(
            CarryFinding,
            ledger=StreamId.review(c.from_task),
            finding=c.finding,
            to_task=c.to_task,
        )
        for c in e.carry
    ]


def resume_stacking(e: TasksPlanned, src: EventId) -> list[Draft]:
    return [C(ResumeStacking)]


def discard_proposal(e: ReplanRequested, src: EventId) -> list[Draft]:
    return [C(DiscardProposal, reason=e.reason)]


def cut_stack_top(e: ReplanRequested, src: EventId) -> list[Draft]:
    return [C(EnqueueGitJob, kind=J.CUT_STACK_TOP)] if e.settled_before else []


def plan_again(e: ReplanRequested, src: EventId) -> list[Draft]:
    """設計が一度も確定していなければ、trees/overview のまま Prepare → Plan から組み直す。"""
    if e.settled_before:
        return []  # stack-top を切り直した WorktreeReady で組む
    return [C(AcceptFlow, task=PLANNING, steps=planning_flow(settled_before=False))]


def pause_stacking(e: ReplanRequested, src: EventId) -> list[Draft]:
    return [C(PauseStacking)]


def stop_tasks(e: TasksStopped, src: EventId) -> list[Draft]:
    drafts: list[Draft] = []
    for task in sorted(e.tasks, key=str):
        drafts += [C(StopTask, task=task, reason="タスクを止めた"), C(WithdrawRequest, task=task)]
    return drafts


def discard_job(e: TasksDiscarded, src: EventId) -> list[Draft]:
    return [C(EnqueueGitJob, kind=J.DISCARD, discarded=e.tasks)]


def requeue(e: TasksReturnedToQueue, src: EventId) -> list[Draft]:
    return [
        C(EnqueueStack, task=task, branch=branch)
        for task, branch in zip(e.tasks, e.branches, strict=True)
    ]


def close_task_side(e: EscalationClosed, src: EventId) -> list[Draft]:
    if src.stream != StreamId.run() or e.task is None or e.source is None:
        return []
    return [C(CloseEscalation, task=e.task, escalation=e.source, reason=e.reason)]


def resolve_task_side(e: EscalationAnswered, src: EventId) -> list[Draft]:
    if e.task is None or e.source is None:
        return []
    return [
        C(ResolveEscalation, task=e.task, escalation=e.source, answer=e.answer, question=e.question)
    ]


def note_answer(e: EscalationAnswered, src: EventId) -> list[Draft]:
    # 統括が応じなかった上げ（failed_notice がある）への回答は、起こし直す知らせに載るので残さない
    if e.task is None or e.source is not None or e.failed_notice is not None:
        return []
    origin = DecisionOrigin.USER if e.question is not None else DecisionOrigin.RUN_SUPERVISOR
    return [C(AddNote, task=e.task, decision=Decision(e.answer, origin, e.question))]


def ask_user_for_supervisor(e: EscalationRaised, src: EventId) -> list[Draft]:
    """ラン統括が応じなかった（上げてきたタスクが無い supervisor-failed）なら、ユーザーに聞く。"""
    if src.stream != StreamId.run() or e.kind is not E.SUPERVISOR_FAILED or e.task is not None:
        return []
    body = (
        f"ラン統括が知らせ {e.failed_notice} に応じなかった（{e.reason}）。原因を取り除いてから"
        "答えると、回答を添えて、ラン統括を新しいセッションで同じ知らせから起こし直す。"
    )
    question = QuestionId(f"supervisor-failed-{src.version}")
    return [C(PostQuestion, question=question, body=body, escalation=src)]


def withdraw_questions(e: EscalationClosed, src: EventId) -> list[Draft]:
    """Run の側で閉じたエスカレーションを経路に持つ、回答待ちの質問を取り下げる。"""
    if src.stream != StreamId.run():
        return []
    return [C(WithdrawQuestions, escalation=e.escalation, reason=e.reason)]


def finish_job(e: RunFinished, src: EventId) -> list[Draft]:
    return [C(EnqueueGitJob, kind=J.FINISH, ready_overview=e.ready_overview)]


def resume_tasks(e: RunResumed, src: EventId) -> list[Draft]:
    return [C(ResumeInterrupted, task=task) for task in e.tasks]


# --- Task のイベント ---


def hand_findings(e: StageCompleted, src: EventId) -> list[Draft]:
    if e.handoff is not Handoff.FINDINGS:
        return []
    assert e.result is not None
    return [
        C(
            RecordFindings,
            ledger=ledger_of(e.execution.task),
            source=e.execution,
            findings=e.result.findings,
            design=e.reviewed,
        )
    ]


def hand_judgement(e: StageCompleted, src: EventId) -> list[Draft]:
    if e.handoff is not Handoff.JUDGEMENT:
        return []
    assert e.result is not None
    return [
        C(
            RecordJudgement,
            ledger=ledger_of(e.execution.task),
            execution=e.execution,
            verdicts=e.result.verdicts,
            stall_cause=e.result.stall_cause,
            design=e.reviewed,
            design_cause=e.result.design_cause,
        )
    ]


def hand_proposal(e: StageCompleted, src: EventId) -> list[Draft]:
    if e.handoff is not Handoff.PROPOSAL:
        return []
    assert e.result is not None and e.result.proposal is not None
    return [C(ProposeDesign, proposal=e.result.proposal, execution=e.execution, artifacts=e.shared)]


def hand_overview(e: StageCompleted, src: EventId) -> list[Draft]:
    if e.handoff is not Handoff.OVERVIEW:
        return []
    assert e.result is not None and e.result.pr is not None and e.job is not None
    assert e.job.branch is not None and e.job.base is not None
    entry = StackEntry(GIT, e.job.branch, e.result.pr, e.job.base)
    return [C(RecordOverview, entry=entry, source=e.execution)]


def hand_entry(e: StageCompleted, src: EventId) -> list[Draft]:
    if e.handoff is not Handoff.ENTRY:
        return []
    job = e.job
    assert e.result is not None and e.result.pr is not None and job is not None
    assert job.task is not None and job.branch is not None and job.base is not None
    entry = StackEntry(job.task, job.branch, e.result.pr, job.base)
    return [C(AppendEntry, entry=entry, source=e.execution)]


def hand_cut_back(e: StageCompleted, src: EventId) -> list[Draft]:
    if e.handoff is not Handoff.CUT_BACK:
        return []
    assert e.job is not None
    return [C(UnstackFrom, entry=e.job.cut_from, discarded=e.job.discarded, source=e.execution)]


def record_conflict(e: StageCompleted, src: EventId) -> list[Draft]:
    if not e.conflicts or e.job is None or e.job.task is None:
        return []
    return [C(RecordConflict, task=e.job.task, files=e.conflicts)]


def count_fix(e: StageCompleted, src: EventId) -> list[Draft]:
    if STAGE_SPECS[e.execution.stage].fixes is None:
        return []
    return [C(CountFix, ledger=ledger_of(e.execution.task), execution=e.execution)]


def comment_findings(e: StageCompleted, src: EventId) -> list[Draft]:
    if e.result is None:
        return []
    return [
        C(
            CommentFinding,
            ledger=ledger_of(e.execution.task),
            finding=c.finding,
            body=c.body,
            author=e.execution,
        )
        for c in e.result.comments
    ]


def gate_passed(e: StageCompleted, src: EventId) -> list[Draft]:
    if e.execution.stage is not S.GATE:
        return []
    return [C(RecordGateResult, ledger=ledger_of(e.execution.task), execution=e.execution)]


def gate_failed(e: GateFailed, src: EventId) -> list[Draft]:
    return [
        C(
            RecordGateResult,
            ledger=ledger_of(e.execution.task),
            execution=e.execution,
            failed=e.failed,
        )
    ]


def reject_integration(e: StageReported, src: EventId) -> list[Draft]:
    if e.kind is not E.INTEGRATION_FAILED or e.job is None or e.job.task is None:
        return []
    return [C(RejectRequest, task=e.job.task, reason=e.reason or e.kind.value)]


def relay_to_run(e: EscalationRaised, src: EventId) -> list[Draft]:
    """計画タスクと git 管理タスクの統括（プログラム）は、受けたものをそのまま上げる。"""
    if not is_task_stream(src) or task_of(src).kind is TaskKind.IMPLEMENTATION:
        return []
    return [
        C(
            EscalateToRun,
            task=task_of(src),
            kind=e.kind,
            reason=e.reason or e.kind.value,
            pointers=e.pointers,
            hint=e.hint,
            source=src,
            question=e.question,
            answer_only=e.answer_only,
        )
    ]


def close_run_side(e: EscalationClosed, src: EventId) -> list[Draft]:
    if not is_task_stream(src):
        return []
    return [C(CloseRelayedEscalation, source=e.escalation, reason=e.reason)]


def resume_design(e: EscalationResolved, src: EventId) -> list[Draft]:
    if e.kind not in DESIGN_WAITS:
        return []
    return [C(ResumeDesign, kind=e.kind, answer=e.answer)]


def resume_ask(e: EscalationResolved, src: EventId) -> list[Draft]:
    """反応: answers/<tool_use_id>.json に回答を書き終えてから、止まった実行を続きから再開する。"""
    if e.resume is None or e.tool_use_id is None:
        return []
    return [C(ResumeStage, task=task_of(src), execution=e.resume)]


def finish_git_job(e: FlowFinished | FlowAbandoned, src: EventId) -> list[Draft]:
    if e.job is None:
        return []
    ending = FlowEnding.FINISHED if isinstance(e, FlowFinished) else FlowEnding.ABANDONED
    return [C(FinishGitJob, job=e.job.id, ending=ending)]


def git_finished(e: FlowFinished, src: EventId) -> list[Draft]:
    if e.job is None or e.job.kind is not J.FINISH:
        return []
    return [C(UpdateTaskStatus, task=GIT, to_status=TaskStatus.FINISHED, cause="FlowFinished")]


def enqueue_gated(e: TaskGated, src: EventId) -> list[Draft]:
    assert e.branch is not None
    return [C(EnqueueStack, task=task_of(src), branch=e.branch)]


def record_cut_base(e: WorktreeReady, src: EventId) -> list[Draft]:
    """積み直すタスクを前に積んだブランチから切り直したなら、切った元は根元ではない。"""
    point = e.job.cut_point if e.job is not None else None
    if e.base is None or (point is not None and not point.roots_branch):
        return []
    return [C(RecordBase, task=e.task, base=e.base, tree=e.tree)]


def record_rebased_base(e: BranchRebased, src: EventId) -> list[Draft]:
    return [C(RecordBase, task=e.task, base=e.onto)]


def plan_on_worktree(e: WorktreeReady, src: EventId) -> list[Draft]:
    """計画タスクの統括（プログラム）: 切った仕事の種類で、初回か再計画かの並びを組む。"""
    if e.task != PLANNING or e.job is None:
        return []
    settled_before = e.job.kind is J.CUT_STACK_TOP
    return [C(AcceptFlow, task=PLANNING, steps=planning_flow(settled_before=settled_before))]


def status(to: TaskStatus) -> Callable[[Event, EventId], list[Draft]]:
    """Task・Stack のイベントで、Run が持つタスクの状態を動かす。"""

    def build(e: Event, src: EventId) -> list[Draft]:
        if isinstance(e, GitJobTaken):
            if e.job.kind is not J.STACK or e.job.task is None:
                return []
            task = e.job.task
        elif not is_task_stream(src):
            return []
        else:
            task = task_of(src)
        if isinstance(e, FlowAccepted) and e.closes is None:
            return []
        return [C(UpdateTaskStatus, task=task, to_status=to, cause=type(e).__name__)]

    return build


# --- ReviewLedger のイベント ---


def conclude_round(e: FindingsEvaluated, src: EventId) -> list[Draft]:
    judge = e.execution
    unresolved = tuple(f.finding for f in e.open_findings)
    routes = tuple(FindingRoute(f.finding, f.target) for f in e.open_findings)
    if judge.stage is S.JUDGE:
        return [
            C(
                ConcludeReviewRound,
                task=judge.task,
                judge=judge,
                unresolved=unresolved,
                stalled=e.stalled,
                cause=e.stall_cause,
                routes=routes,
            )
        ]
    if judge.stage is S.GATE:
        return [
            C(
                ConcludeGateRound,
                task=judge.task,
                gate=judge,
                unresolved=unresolved,
                stalled=e.stalled,
                routes=routes,
            )
        ]
    # 設計の台帳: 前の版に戻った・曖昧なら回答を待つ。must-fix が残れば直す。無ければ確定する
    cause = e.design_cause
    if cause is not None and cause.cause is DesignCause.REVERTED:
        assert cause.reverted_to is not None
        return [C(MarkReverted, to_version=cause.reverted_to, execution=judge)]
    if cause is not None and cause.cause is DesignCause.AMBIGUOUS:
        return [C(MarkAmbiguous, execution=judge)]
    if any(f.rating is Rating.MUST_FIX for f in e.open_findings):
        return [C(ReviseDesign, execution=judge)]
    assert e.design is not None
    return [C(SettleDesign, execution=judge, design=e.design, open_findings=e.open_findings)]


def confirm_received(e: ResultReceived, src: EventId) -> list[Draft]:
    return [C(ConfirmHandoff, task=e.source.task, execution=e.source)]


def confirm_refused(e: ResultRefused, src: EventId) -> list[Draft]:
    return [C(ConfirmHandoff, task=e.source.task, execution=e.source, refused=e.reason)]


def raise_carried(e: FindingCarried, src: EventId) -> list[Draft]:
    return [
        C(
            RaiseFinding,
            ledger=StreamId.review(e.to_task),
            rating=e.rating,
            body=e.body,
            location=e.location,
            finding_target=e.target,
            carried_from=FindingOrigin(src.stream, e.finding),
        )
    ]


# --- Design のイベント ---


def track_proposal(e: DesignProposed, src: EventId) -> list[Draft]:
    return [C(TrackProposal, ledger=StreamId.design_review(), design=e.proposal.design)]


def record_settled(e: DesignSettled, src: EventId) -> list[Draft]:
    return [C(RecordSettledPlan, proposal=e.proposal, artifacts=e.artifacts)]


def conclude_settled(e: DesignSettled, src: EventId) -> list[Draft]:
    return [C(ConcludeDesignRound, task=PLANNING, judge=e.execution, settled=e.proposal.design)]


def reject_appendix(e: DesignSettled, src: EventId) -> list[Draft]:
    return [
        C(
            JudgeFinding,
            ledger=StreamId.design_review(),
            finding=f.finding,
            to=FindingStatus.REJECTED,
            comment="must-fix 以外なので、設計ファイルの末尾に書き足した",
            execution=e.execution,
        )
        for f in e.appendix
    ]


def conclude_revision(e: DesignRevisionStarted, src: EventId) -> list[Draft]:
    if e.execution is None:
        return []
    return [C(ConcludeDesignRound, task=PLANNING, judge=e.execution)]


def escalate_design_wait(kind: EscalationKind) -> Callable[[Event, EventId], list[Draft]]:
    def build(e: Event, src: EventId) -> list[Draft]:
        execution = getattr(e, "execution", None)
        cause = {E.DESIGN_REVERTED: DesignCause.REVERTED, E.DESIGN_AMBIGUOUS: DesignCause.AMBIGUOUS}
        hint = Hint(design_cause=cause.get(kind))
        return [
            C(Escalate, task=PLANNING, kind=kind, pointers=Pointers(), hint=hint, origin=execution)
        ]

    return build


# --- Stack のイベント ---


def take_next(e: Event, src: EventId) -> list[Draft]:
    return [C(TakeNextGitJob)]


def flow_for_job(e: GitJobTaken, src: EventId) -> list[Draft]:
    """git 管理タスクの統括（プログラム）: 取り出した仕事から決まった並びを組む。"""
    return [C(AcceptFlow, task=GIT, steps=git_job_flow(e.job), job=e.job)]


def abandon_withdrawn(e: GitJobWithdrawn, src: EventId) -> list[Draft]:
    return [C(AbandonFlow, task=GIT, reason="相手のタスクを止めた")] if e.in_progress else []


def mark_stacked(e: TaskStacked, src: EventId) -> list[Draft]:
    return [C(MarkStacked, task=e.entry.task, pr=e.entry.pr)]


def return_to_queue(e: StackCutBack, src: EventId) -> list[Draft]:
    return [C(ReturnToQueue)]


def record_integration_failure(e: IntegrationFailed, src: EventId) -> list[Draft]:
    return [C(RecordIntegrationFailure, task=e.task, reason=e.reason, files=e.files)]


def clear_integration_failure(e: IntegrationRetried, src: EventId) -> list[Draft]:
    return [C(ClearIntegrationFailure, task=e.task)]


def regate_kept_stack_job(e: GitJobFinished, src: EventId) -> list[Draft]:
    if not e.outcome.keeps_job or e.job.kind is not J.STACK or e.job.task is None:
        return []
    return [
        C(UpdateTaskStatus, task=e.job.task, to_status=TaskStatus.GATED, cause="GitJobFinished")
    ]


def escalate_stuck_job(e: GitJobFinished, src: EventId) -> list[Draft]:
    """やめるとランが終わらない仕事は、答え以外では閉じられない上げにする。"""
    if e.outcome is not GitJobOutcome.STUCK:
        return []
    stuck = (
        f"git の仕事 {e.job.id}（{e.job.kind.value}）のフローを捨てて {MAX_JOB_RETURNS} 回戻したが"
        "、済んでいない。"
    )
    if e.job.kind.can_drop:
        reason = stuck + "続けるなら答え、やめるなら答え以外の判断で閉じる"
    else:
        reason = (
            stuck
            + "この仕事はやめるとランが終わらないので、答えて続ける（答え以外では閉じられない）"
        )
    return [
        C(
            Escalate,
            task=GIT,
            kind=E.STAGE_ERRORS,
            pointers=Pointers(),
            reason=reason,
            answer_only=not e.job.kind.can_drop,
        )
    ]


def is_stuck_job_escalation(kind: EscalationKind | None, origin: ExecutionId | None) -> bool:
    """止めた仕事への上げ: git 管理タスクの、上げ元の実行が無い stage-errors。"""
    return kind is E.STAGE_ERRORS and origin is None


def retry_stuck_job(e: EscalationResolved, src: EventId) -> list[Draft]:
    if not is_git_stream(src) or not is_stuck_job_escalation(e.kind, e.origin):
        return []
    return [C(RetryGitJob)]


def drop_stuck_job(e: EscalationClosed, src: EventId) -> list[Draft]:
    if not is_git_stream(src) or not is_stuck_job_escalation(e.kind, e.origin):
        return []
    return [C(DropGitJob)]


def retry_integration(e: EscalationResolved, src: EventId) -> list[Draft]:
    if not is_git_stream(src) or e.kind is not E.INTEGRATION_FAILED:
        return []
    return [C(RetryIntegration)]


# --- Questions のイベント ---


def record_answer(e: QuestionAnswered, src: EventId) -> list[Draft]:
    return [C(RecordAnswer, question=e.question, answer=e.answer, escalation=e.escalation)]


@dataclass(frozen=True)
class Seam:
    #: 受け手の名前（CommandId と Issuer に入る）
    name: str
    on: type[Event] | tuple[type[Event], ...]
    build: Callable[[Any, EventId], list[Draft]]
    #: 組むのに読む欄（表として読む人のため）
    reads: str
    #: 出す者。計画タスクと git 管理タスクの統括（プログラム）は supervisor、副作用の後の続きは reaction
    by: Literal["policy", "supervisor", "reaction"] = "policy"


SEAMS: tuple[Seam, ...] = (
    # Run
    Seam("start-planning-and-git", RunStarted, start_planning_and_git, "name・base"),
    Seam(
        "open-task",
        TaskStarted,
        open_task,
        "task・kind・spec・artifacts・blocked_by・branch・conflicts（reopened でない）",
    ),
    Seam(
        "rescope-reopened",
        TaskStarted,
        rescope_reopened,
        "task・spec・artifacts・branch（reopened）",
    ),
    Seam("cut-task-branch", TaskStarted, cut_task_branch, "task・kind・branch"),
    Seam("apply-first-plan", SettledPlanRecorded, apply_first_plan, "replan・proposal.design"),
    Seam("start-ready-after-plan", TasksPlanned, start_ready, "（イベントだけ）"),
    Seam("write-overview", TasksPlanned, write_overview, "replan"),
    Seam("change-scope", TasksPlanned, change_scope, "scope_changed・tasks[].spec・artifacts"),
    Seam("withdraw-rescoped", TasksPlanned, withdraw_rescoped, "withdraw"),
    Seam("carry-findings", TasksPlanned, carry_findings, "carry[]（finding・from_task・to_task）"),
    Seam("resume-stacking", TasksPlanned, resume_stacking, "（イベントだけ）"),
    Seam("discard-proposal", ReplanRequested, discard_proposal, "reason"),
    Seam("cut-stack-top", ReplanRequested, cut_stack_top, "settled_before"),
    Seam("plan-again", ReplanRequested, plan_again, "settled_before", by="supervisor"),
    Seam("pause-stacking", ReplanRequested, pause_stacking, "（イベントだけ）"),
    Seam("stop-tasks", TasksStopped, stop_tasks, "tasks"),
    Seam("discard-job", TasksDiscarded, discard_job, "tasks"),
    Seam("requeue", TasksReturnedToQueue, requeue, "tasks・branches"),
    Seam("start-ready-after-stacked", TaskMarkedStacked, start_ready, "（イベントだけ）"),
    Seam("start-ready-after-inserted", TaskInserted, start_ready, "（イベントだけ）"),
    Seam("start-ready-after-stopped", TasksStopped, start_ready, "（イベントだけ）"),
    Seam(
        "start-ready-after-slot-freed",
        TaskStatusChanged,
        start_ready_after_slot_freed,
        "from_status・to_status（枠を空ける遷移）",
    ),
    Seam("close-task-side", EscalationClosed, close_task_side, "task・source・reason（run の）"),
    Seam(
        "resolve-task-side",
        EscalationAnswered,
        resolve_task_side,
        "task・source・answer・question",
    ),
    Seam(
        "note-answer",
        EscalationAnswered,
        note_answer,
        "task・answer・question（source も failed_notice も無い）",
    ),
    Seam(
        "ask-user-for-supervisor",
        EscalationRaised,
        ask_user_for_supervisor,
        "kind・task・failed_notice・reason（Run のストリーム）",
    ),
    Seam(
        "withdraw-questions",
        EscalationClosed,
        withdraw_questions,
        "escalation・reason（Run のストリーム）",
    ),
    Seam("finish-job", RunFinished, finish_job, "ready_overview"),
    Seam("resume-tasks", RunResumed, resume_tasks, "tasks"),
    Seam("start-ready-after-resumed", RunResumed, start_ready, "（イベントだけ）"),
    # Task
    Seam("hand-findings", StageCompleted, hand_findings, "handoff・result.findings・reviewed"),
    Seam(
        "hand-judgement",
        StageCompleted,
        hand_judgement,
        "handoff・result.verdicts・stall_cause・design_cause・reviewed",
    ),
    Seam("hand-proposal", StageCompleted, hand_proposal, "handoff・result.proposal・shared"),
    Seam(
        "hand-overview", StageCompleted, hand_overview, "handoff・result.pr・job.branch・job.base"
    ),
    Seam(
        "hand-entry",
        StageCompleted,
        hand_entry,
        "handoff・result.pr・job.task・job.branch・job.base",
    ),
    Seam("hand-cut-back", StageCompleted, hand_cut_back, "handoff・job.cut_from・job.discarded"),
    Seam("record-conflict", StageCompleted, record_conflict, "conflicts・job.task"),
    Seam("count-fix", StageCompleted, count_fix, "execution"),
    Seam("comment-findings", StageCompleted, comment_findings, "result.comments・execution"),
    Seam("gate-passed", StageCompleted, gate_passed, "execution"),
    Seam("gate-failed", GateFailed, gate_failed, "execution・failed"),
    Seam("reject-integration", StageReported, reject_integration, "kind・job.task・reason"),
    Seam(
        "relay-to-run",
        EscalationRaised,
        relay_to_run,
        "kind・reason・pointers・hint・question・answer_only（id を source に）",
        by="supervisor",
    ),
    Seam("close-run-side", EscalationClosed, close_run_side, "escalation・reason（タスクの側の）"),
    Seam("resume-design", EscalationResolved, resume_design, "kind・answer"),
    Seam("resume-ask", EscalationResolved, resume_ask, "resume・tool_use_id", by="reaction"),
    Seam(
        "finish-git-job",
        (FlowFinished, FlowAbandoned),
        finish_git_job,
        "job.id（終えたか捨てたかはイベントの種類）",
    ),
    Seam("git-finished", FlowFinished, git_finished, "job.kind"),
    Seam("enqueue-gated", TaskGated, enqueue_gated, "branch（ストリームのタスク）"),
    Seam("record-cut-base", WorktreeReady, record_cut_base, "task・tree・base・job.cut_point"),
    Seam("record-rebased-base", BranchRebased, record_rebased_base, "task・onto"),
    Seam("plan-on-worktree", WorktreeReady, plan_on_worktree, "task・job.kind", by="supervisor"),
    Seam("status-gated", TaskGated, status(TaskStatus.GATED), "（ストリームのタスク）"),
    Seam("status-stacking", GitJobTaken, status(TaskStatus.STACKING), "job.kind・job.task"),
    Seam(
        "status-escalated", EscalationRaised, status(TaskStatus.ESCALATED), "（タスクのストリーム）"
    ),
    Seam(
        "status-running",
        (EscalationResolved, EscalationClosed, ScopeChanged, FlowAccepted),
        status(TaskStatus.RUNNING),
        "（タスクのストリーム。FlowAccepted は closes があるときだけ）",
    ),
    # ReviewLedger
    Seam(
        "conclude-round",
        FindingsEvaluated,
        conclude_round,
        "execution・open_findings・stalled・stall_cause・design・design_cause",
    ),
    Seam("confirm-received", ResultReceived, confirm_received, "source"),
    Seam("confirm-refused", ResultRefused, confirm_refused, "source・reason"),
    Seam(
        "raise-carried",
        FindingCarried,
        raise_carried,
        "to_task・rating・body・location・target・finding",
    ),
    # Design
    Seam("track-proposal", DesignProposed, track_proposal, "proposal.design"),
    Seam("record-settled", DesignSettled, record_settled, "proposal・artifacts"),
    Seam("conclude-settled", DesignSettled, conclude_settled, "execution・proposal.design"),
    Seam("reject-appendix", DesignSettled, reject_appendix, "appendix[].finding・execution"),
    Seam("conclude-revision", DesignRevisionStarted, conclude_revision, "execution"),
    Seam(
        "escalate-reverted",
        DesignReverted,
        escalate_design_wait(E.DESIGN_REVERTED),
        "execution",
    ),
    Seam(
        "escalate-ambiguous",
        DesignAmbiguous,
        escalate_design_wait(E.DESIGN_AMBIGUOUS),
        "execution",
    ),
    Seam(
        "escalate-rounds-exhausted",
        DesignRoundsExhausted,
        escalate_design_wait(E.DESIGN_ROUNDS_EXHAUSTED),
        "execution",
    ),
    # Stack
    Seam(
        "take-next",
        (
            GitJobQueued,
            GitJobFinished,
            StackingResumed,
            GitJobRetried,
            GitJobDropped,
            GitJobWithdrawn,
        ),
        take_next,
        "（イベントだけ）",
    ),
    Seam("flow-for-job", GitJobTaken, flow_for_job, "job", by="supervisor"),
    Seam("abandon-withdrawn", GitJobWithdrawn, abandon_withdrawn, "in_progress"),
    Seam("mark-stacked", TaskStacked, mark_stacked, "entry.task・entry.pr"),
    Seam("return-to-queue", StackCutBack, return_to_queue, "（イベントだけ）"),
    Seam(
        "record-integration-failure",
        IntegrationFailed,
        record_integration_failure,
        "task・reason・files",
    ),
    Seam("clear-integration-failure", IntegrationRetried, clear_integration_failure, "task"),
    Seam(
        "regate-kept-stack-job",
        GitJobFinished,
        regate_kept_stack_job,
        "outcome・job.kind・job.task（積む仕事を残した）",
    ),
    Seam(
        "escalate-stuck-job",
        GitJobFinished,
        escalate_stuck_job,
        "outcome・job.id・job.kind（can_drop が偽なら答え以外では閉じられない）",
    ),
    Seam(
        "retry-stuck-job",
        EscalationResolved,
        retry_stuck_job,
        "kind・origin（git 管理タスクの、上げ元の実行が無い stage-errors）",
    ),
    Seam(
        "drop-stuck-job",
        EscalationClosed,
        drop_stuck_job,
        "kind・origin（git 管理タスクの、上げ元の実行が無い stage-errors）",
    ),
    Seam(
        "retry-integration",
        EscalationResolved,
        retry_integration,
        "kind（git 管理タスクの integration-failed）",
    ),
    # Questions
    Seam("record-answer", QuestionAnswered, record_answer, "question・answer・escalation"),
)


# =====================================================================================
# 本物の集約で流れを通す道具
# =====================================================================================


def factory(stream: StreamId) -> Aggregate:
    if stream == StreamId.run():
        return Run(stream)
    if stream == StreamId.design():
        return Design(stream)
    if stream == StreamId.stack():
        return Stack(stream)
    if stream == StreamId.questions():
        return Questions(stream)
    if stream.is_review:
        return ReviewLedger(stream)
    return Task(stream)


@dataclass
class Outcome:
    """実行器がステージを走らせた結果（台本）。無い欄は、何も挙げない既定の結果になる。"""

    result: dict[str, Any] = field(default_factory=dict)
    products: tuple[ArtifactKind, ...] | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    #: 結果をこのまま渡す（既定の結果に重ねない）
    exact: bool = False


#: 走らせ始めて、まだ報告しない（パニック・止める流れで、走っている途中を作る）
HOLD = Outcome()
Script = Callable[["World", ExecutionId], Outcome | None]
Actor = Callable[["World", EventId, Event], list[Command]]


class World:
    """本物の集約と、表のつなぎ目と、外からの入力（台本）で流れを通す。"""

    #: 作った World（表のどのつなぎ目を通ったかを、流れをまたいで集める）
    created: ClassVar[list[World]] = []

    def __init__(self, *actors: Actor, script: Script | None = None) -> None:
        World.created.append(self)
        self.aggregates: dict[StreamId, Aggregate] = {}
        self.queue: deque[tuple[EventId, Event]] = deque()
        self.log: list[tuple[EventId, Event]] = []
        self.fired: Counter[str] = Counter()
        self.actors = list(actors)
        self.script = script
        #: 実行器が走らせると決めた（StageRequested）・続きから再開した（StageStarted）実行
        self.requested: list[ExecutionId] = []
        self.resumed: list[ExecutionId] = []
        self.held: list[ExecutionId] = []
        self._ids = itertools.count(1)
        self._versions = itertools.count(1)
        self._prs = itertools.count(100)
        self._task_prs: dict[TaskId, int] = {}

    # --- 集約 ---

    def get(self, stream: StreamId) -> Aggregate:
        if stream not in self.aggregates:
            self.aggregates[stream] = factory(stream)
        return self.aggregates[stream]

    @property
    def run(self) -> Run:
        run = self.get(StreamId.run())
        assert isinstance(run, Run)
        return run

    def task(self, task: TaskId) -> Task:
        found = self.get(StreamId.task(task))
        assert isinstance(found, Task)
        return found

    @property
    def stack(self) -> Stack:
        stack = self.get(StreamId.stack())
        assert isinstance(stack, Stack)
        return stack

    def events(self, cls: type[Event]) -> list[Any]:
        return [event for _, event in self.log if isinstance(event, cls)]

    def last_id(self, cls: type[Event], stream: StreamId | None = None) -> EventId:
        return next(
            eid
            for eid, event in reversed(self.log)
            if isinstance(event, cls) and (stream is None or eid.stream == stream)
        )

    # --- コマンドを処理し、表のつなぎ目と外からの入力に配る ---

    def process(self, command: Command) -> list[Event]:
        aggregate = self.get(command.target)
        try:
            events = aggregate.handle(command)
        except Rejected as rejected:
            # つなぎ目のコマンドは、どれも受けられなければならない（拒否は取り違えだけ）
            raise AssertionError(
                f"{type(command).__name__}（{command.issuer.name or command.issuer.kind.value}）"
                f"を拒んだ: {rejected.reason}"
            ) from rejected
        for event in events:
            aggregate.apply(event, command.command_id)
            entry = (aggregate.event_id, event)
            self.queue.append(entry)
            self.log.append(entry)
        return events

    def command_id(self) -> CommandId:
        return CommandId(f"outside-{next(self._ids)}")

    def pump(self) -> None:
        while self.queue:
            eid, event = self.queue.popleft()
            if isinstance(event, StageRequested):
                self.requested.append(event.execution)
            if isinstance(event, StageStarted) and self._was_started_before(event):
                self.resumed.append(event.execution)
            for seam in SEAMS:
                if not isinstance(event, seam.on):
                    continue
                drafts = seam.build(event, eid)
                for index, (cls, fields) in enumerate(drafts):
                    issuer = _issuer(seam, eid, fields)
                    command_id = CommandId.derived(eid, seam.name, index)
                    self.process(cls(command_id=command_id, issuer=issuer, **fields))
                if drafts:
                    self.fired[seam.name] += 1
            for actor in self.actors:
                for command in actor(self, eid, event):
                    self.process(command)

    def outside(self, command_type: type[Command], issuer: Issuer, **fields: Any) -> list[Event]:
        """外からの入力（CLI・driver・LLM の統括）を 1 つ処理して、配り切る。"""
        events = self.process(command_type(command_id=self.command_id(), issuer=issuer, **fields))
        self.pump()
        return events

    def _was_started_before(self, event: StageStarted) -> bool:
        starts = [e for _, e in self.log if isinstance(e, StageStarted)]
        return sum(1 for e in starts if e.execution == event.execution) > 1

    # --- 実行器 ---

    def run_stages(self, limit: int = 2000) -> None:
        """走らせると決めたステージを、台本どおり（無ければ何も挙げない結果で）走らせ切る。"""
        for _ in range(limit):
            if self.resumed:
                execution = self.resumed.pop(0)
                self.report(execution, self._outcome(execution) or Outcome())
            elif self.requested:
                execution = self.requested.pop(0)
                if not self._startable(execution):
                    continue
                outcome = self._outcome(execution)
                self.begin(execution)
                if outcome is HOLD:
                    self.held.append(execution)
                    continue
                self.report(execution, outcome or Outcome())
            else:
                return
            self.pump()
        raise AssertionError("流れが終わらない")

    def _outcome(self, execution: ExecutionId) -> Outcome | None:
        return self.script(self, execution) if self.script is not None else None

    def _startable(self, execution: ExecutionId) -> bool:
        task = self.task(execution.task)
        found = task.executions.get(execution)
        return (
            found is not None
            and found.status is ExecutionStatus.REQUESTED
            and not task.stopped
            and task.flow is not None
            and found.flow_version == task.flow.version
        )

    def begin(self, execution: ExecutionId) -> None:
        llm = STAGE_SPECS[execution.stage].mode is StageMode.LLM
        self.process(
            BeginStage(
                command_id=self.command_id(),
                issuer=Issuer.executor(execution),
                task=execution.task,
                execution=execution,
                head=HEAD,
                session=SESSION if llm else None,
            )
        )
        self.pump()

    def report(self, execution: ExecutionId, outcome: Outcome) -> None:
        spec = STAGE_SPECS[execution.stage]
        made = spec.produces if outcome.products is None else outcome.products
        products = tuple(self._product(kind) for kind in sorted(made, key=str))
        evidence: dict[str, Any] = {
            "exit": StageExit.OK,
            "result_valid": True,
            # 根元から上のコミットがある（Rebase は 0 件・数えられないと落ちる）
            "commits": 1,
            **self._evidence(execution),
        }
        evidence.update(outcome.evidence)
        result = (
            outcome.result
            if outcome.exact
            else stage_result(execution.stage, **{**self._result(execution), **outcome.result})
        )
        self.process(
            ReportStageResult(
                command_id=self.command_id(),
                issuer=Issuer.executor(execution),
                task=execution.task,
                execution=execution,
                evidence=Evidence(products=products, **evidence),
                pointers=Pointers(),
                result=result,
            )
        )

    def _product(self, kind: ArtifactKind) -> ArtifactRef:
        if kind is ArtifactKind.PROPOSAL:
            # 実行器は提案の本文を design/v<版>.md に書き出し、その版を在りかにする
            return ArtifactRef(kind, str(next(self._versions)))
        return ArtifactRef(kind, "x")

    def _evidence(self, execution: ExecutionId) -> dict[str, Any]:
        stage = execution.stage
        if stage is S.GATE:
            return {"gate": GateReport(tuple(GateItemResult(i, True) for i in GateItem))}
        if stage is S.CHECK_UNION:
            return {"union": UnionVerdict(())}
        if stage is S.CONFIRM_RED:
            return {"verify": (VerifyResult(VerifyCommand("pytest"), 1),)}
        return {}

    def _result(self, execution: ExecutionId) -> dict[str, Any]:
        """実行器が組む決定的なステージの結果（切った worktree・PR の番号）。"""
        stage = execution.stage
        job = self.task(execution.task).flow
        job = job.job if job is not None else None
        if stage is S.CUT_BRANCH and job is not None:
            user = job.task or PLANNING
            tree = {J.CUT_OVERVIEW: "trees/overview", J.CUT_STACK_TOP: "trees/stack-top"}.get(
                job.kind, f"trees/{user}"
            )
            branch = None if job.kind is J.CUT_STACK_TOP else job.branch
            return {"task": str(user), "tree": tree, "branch": str(branch) if branch else None}
        if stage is S.CREATE_OVERVIEW_PR:
            return {"pr": next(self._prs)}
        if stage in (S.CREATE_PR, S.STACK_LINK) and job is not None and job.task is not None:
            if stage is S.CREATE_PR:
                self._task_prs[job.task] = next(self._prs)
            return {"pr": self._task_prs[job.task]}
        return {}


def _issuer(seam: Seam, source: EventId, fields: dict[str, Any]) -> Issuer:
    if seam.by == "supervisor":
        return Issuer.task_supervisor(fields["task"])
    if seam.by == "reaction":
        return Issuer.reaction(seam.name, source)
    return Issuer.policy(seam.name, source)


# --- 外からの入力（LLM の統括） ---

IMPL_FLOW = (
    FlowStep(S.IMPL),
    FlowStep(S.REVIEW_LOOP, reviewers=Reviewers((S.REVIEW,))),
    FlowStep(S.GATE),
    FlowStep(S.WRITE_PR_BODY),
)


def impl_supervisor(world: World, eid: EventId, event: Event) -> list[Command]:
    """実装タスクの統括（LLM）の代わり: worktree を切ったら・範囲が変わったら、いつものフローを返す。

    新しいブランチで切り直す（ScopeChanged.branch がある）なら、切り直した worktree を待つ。
    """
    if isinstance(event, WorktreeReady):
        if event.task.kind is not TaskKind.IMPLEMENTATION or event.job is None:
            return []
        if event.job.kind is not J.CUT_TASK:
            return []  # 積み直すための切り直し（git 管理タスクのフローの中）
        task = event.task
    elif isinstance(event, ScopeChanged) and event.branch is None:
        task = task_of(eid)
    else:
        return []
    return [
        AcceptFlow(
            command_id=world.command_id(),
            issuer=Issuer.task_supervisor(task, SESSION),
            task=task,
            steps=IMPL_FLOW,
        )
    ]


def run_supervisor(*, replan_once: bool = False) -> Actor:
    """ラン統括（LLM）の代わり: 確定した再計画は提案どおりに反映し、全部終端になったら仕上げる。"""
    asked: list[bool] = []

    def act(world: World, eid: EventId, event: Event) -> list[Command]:
        if isinstance(event, SettledPlanRecorded) and event.replan:
            proposal = event.proposal
            return [
                ApplyReplan(
                    command_id=world.command_id(),
                    issuer=RUN_SUPERVISOR,
                    design=proposal.design,
                    stop=proposal.stop,
                    discard=proposal.discard,
                )
            ]
        if isinstance(event, AllTasksSettled):
            if replan_once and not asked:
                asked.append(True)
                return [
                    RequestReplan(
                        command_id=world.command_id(),
                        issuer=RUN_SUPERVISOR,
                        reason="task1 を作り直す",
                    )
                ]
            return [
                FinishRun(command_id=world.command_id(), issuer=RUN_SUPERVISOR, ready_overview=True)
            ]
        return []

    return act


def start(world: World) -> None:
    world.outside(
        StartRun,
        Issuer.cli(),
        name=NAME,
        instruction=Instruction("キャッシュを足す"),
        repository=Repository("/repo"),
        base=BASE,
    )


def planned(number: int, *deps: int, title: str | None = None) -> dict[str, Any]:
    """計画ステージの結果の tasks の 1 件（schemas の形）。"""
    return {
        "id": f"task{number}",
        "title": title or f"t{number}",
        "dod": "",
        "acceptance": [],
        "scope": [],
        "entryPoints": [],
        "boundary": "",
        "taskTests": [],
        "blockedBy": [f"task{d}" for d in deps],
    }


def must_fix(body: str = "境界の値で落ちる") -> dict[str, Any]:
    return {"rating": "must-fix", "body": body, "location": "src/a.py:3"}


def verdict(finding: str, to: str = "closed") -> dict[str, Any]:
    return {"finding": finding, "to": to, "comment": "確かめた"}


def stacked_tasks(world: World) -> list[TaskId]:
    return [entry.task for entry in world.stack.entries]


# =====================================================================================
# 流れ
# =====================================================================================


def main_script(world: World, execution: ExecutionId) -> Outcome | None:  # noqa: PLR0911  台本の段ごと
    """主な流れ: 計画（設計を 1 回直す）→ task1 → task2（task1 を待つ）→ 仕上げ。"""
    stage, round, task = execution.stage, execution.round, execution.task
    tasks = [planned(1), planned(2, 1)]
    if stage in (S.PLAN, S.REVISE):
        return Outcome(result={"tasks": tasks})
    if stage is S.DESIGN_REVIEW and round == 1:
        return Outcome(
            result={
                "findings": [must_fix("依存が輪になる"), {**must_fix("見出し"), "rating": "nit"}]
            }
        )
    if stage is S.DESIGN_JUDGE and round == 2:
        return Outcome(result={"verdicts": [verdict("D1")]})
    if stage is S.REVIEW and round == 1:
        return Outcome(result={"findings": [must_fix()]})
    if stage is S.FIX:
        return Outcome(result={"comments": [{"finding": "R1", "body": "境界を直した"}]})
    if stage is S.JUDGE and round == 2:
        return Outcome(result={"verdicts": [verdict("R1")]})
    if stage is S.GATE and task == TaskId("task2") and execution.attempt == 1:
        failing = GateReport(
            tuple(GateItemResult(i, i is not GateItem.VERIFY, "pytest が落ちた") for i in GateItem)
        )
        return Outcome(evidence={"gate": failing})
    flow = world.task(GIT).flow
    job = flow.job if flow is not None else None
    if stage is S.REBASE and job is not None and job.task == TaskId("task2"):
        return Outcome(evidence={"conflicts": ("tests/test_a.py",)})
    return None


def test_主な流れはランの開始から仕上げまでイベントの欄だけで進む():
    world = World(impl_supervisor, run_supervisor(), script=main_script)
    start(world)
    world.run_stages()
    run = world.run
    assert run.finished
    assert run.tasks[GIT].status is TaskStatus.FINISHED
    assert run.tasks[PLANNING].status is TaskStatus.FINISHED
    assert stacked_tasks(world) == [TaskId("task1"), TaskId("task2")]
    # 積んだ 1 本の base は、取り出したときのスタックの一番上
    first, second = world.stack.entries
    assert first.base == BranchName.overview(NAME)
    assert second.base == first.branch
    # 設計は 1 回直して確定し、must-fix 以外は末尾に回して rejected にした
    settled = world.events(DesignSettled)[0]
    assert settled.proposal.design.value == 2
    design_ledger = world.get(StreamId.design_review())
    assert isinstance(design_ledger, ReviewLedger)
    assert {str(f): v.status for f, v in design_ledger.findings.items()} == {
        "D1": FindingStatus.CLOSED,
        "D2": FindingStatus.REJECTED,
    }
    # ラン共通の成果物（brief・codemap・design）が、確定から Run を経て実装タスクへ渡った
    shared = {a.kind for a in world.task(TaskId("task1")).artifacts.values()}
    assert {ArtifactKind.BRIEF, ArtifactKind.CODEMAP, ArtifactKind.DESIGN} <= shared
    # Gate の落ちは G- の指摘を開き、直した後の Gate が閉じた
    ledger2 = world.get(StreamId.review(TaskId("task2")))
    assert isinstance(ledger2, ReviewLedger)
    assert {str(f): v.status for f, v in ledger2.findings.items()}[
        "G-verify"
    ] is FindingStatus.CLOSED
    # Rebase の衝突は、処理中の積む仕事（task2）に記録した
    (conflicted,) = world.events(RebaseConflicted)
    assert (conflicted.task, conflicted.files) == (TaskId("task2"), ("tests/test_a.py",))
    used = set(world.fired)
    assert {
        "start-planning-and-git",
        "plan-on-worktree",
        "hand-proposal",
        "hand-findings",
        "hand-judgement",
        "conclude-round",
        "conclude-revision",
        "record-settled",
        "apply-first-plan",
        "hand-overview",
        "hand-entry",
        "record-conflict",
        "gate-failed",
        "count-fix",
        "comment-findings",
        "reject-appendix",
        "git-finished",
    } <= used


def ask_script(world: World, execution: ExecutionId) -> Outcome | None:
    """計画ステージが ask で止まり、ユーザーの回答で続きから再開する。"""
    if execution.stage is S.PLAN and not world.events(EscalationResolved):
        return Outcome(
            products=(),
            result={},
            exact=True,
            evidence={"result_valid": False, "deferred": DeferredCall("toolu_1", "A か B か")},
        )
    if execution.stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1)]})
    return None


def asks_user(world: World, eid: EventId, event: Event) -> list[Command]:
    """ラン統括: 上がってきた ask をユーザーに聞き、届いた回答を渡す。"""
    if isinstance(event, EscalationRaised) and eid.stream == StreamId.run():
        return [
            PostQuestion(
                command_id=world.command_id(),
                issuer=RUN_SUPERVISOR,
                question=QuestionId("q-scope"),
                body="A か B か",
                escalation=eid,
            )
        ]
    if isinstance(event, AnswerRecorded) and event.escalation is not None:
        return [
            AnswerEscalation(
                command_id=world.command_id(),
                issuer=RUN_SUPERVISOR,
                escalation=event.escalation,
                question=event.question,
            )
        ]
    return []


def test_計画ステージのaskはユーザーの回答で同じ実行を続きから再開する():
    world = World(impl_supervisor, run_supervisor(), asks_user, script=ask_script)
    start(world)
    world.run_stages()
    assert world.events(QuestionAnswered) == []
    world.outside(AnswerQuestion, Issuer.cli(), question=QuestionId("q-scope"), answer="A")
    resolved = world.events(EscalationResolved)[0]
    # 続きから再開する実行と、回答を書くファイルの名前は Task が決めて載せた
    assert (resolved.kind, resolved.tool_use_id) == (E.ASK, "toolu_1")
    assert resolved.resume == ExecutionId(PLANNING, S.PLAN, 0, 1)
    world.run_stages()
    assert world.run.finished
    # 回答は出どころ（ユーザー）付きで notes に残った
    assert world.task(PLANNING).notes[0].is_human
    assert {"relay-to-run", "record-answer", "resolve-task-side", "resume-ask"} <= set(world.fired)


def replans_on_ask(world: World, eid: EventId, event: Event) -> list[Command]:
    """ラン統括: 計画ステージの ask に、答えずに再計画で応じる。"""
    if isinstance(event, EscalationRaised) and eid.stream == StreamId.run():
        return [
            RequestReplan(
                command_id=world.command_id(),
                issuer=RUN_SUPERVISOR,
                reason="指示を読み直して割り直す",
                trigger=eid,
            )
        ]
    return []


def replan_on_ask_script(world: World, execution: ExecutionId) -> Outcome | None:
    if execution.stage is S.PLAN and execution.attempt == 1:
        return ask_script(world, execution)
    if execution.stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1)]})
    return None


def test_計画ステージのaskに再計画で応じたら止まった実行を捨てPrepareから組み直す():
    world = World(impl_supervisor, run_supervisor(), replans_on_ask, script=replan_on_ask_script)
    start(world)
    world.run_stages()
    planning = world.task(PLANNING)
    asked = ExecutionId(PLANNING, S.PLAN, 0, 1)
    assert planning.executions[asked].status is ExecutionStatus.ABANDONED
    # 設計が一度も確定していないので、Prepare → Plan から組み直した
    (requested,) = world.events(ReplanRequested)
    assert not requested.settled_before
    assert world.run.finished
    assert {"plan-again", "close-task-side"} <= set(world.fired)


def asks_then_replans(world: World, eid: EventId, event: Event) -> list[Command]:
    """ラン統括: 上がってきた ask をユーザーに聞いたが、回答を待たずに再計画で応じる。"""
    if isinstance(event, EscalationRaised) and eid.stream == StreamId.run():
        return asks_user(world, eid, event)
    if isinstance(event, QuestionPosted) and event.escalation is not None:
        return [
            RequestReplan(
                command_id=world.command_id(),
                issuer=RUN_SUPERVISOR,
                reason="指示を読み直して割り直す",
                trigger=event.escalation,
            )
        ]
    return []


def test_ユーザーに聞いた後に回答以外で閉じたエスカレーションの質問は取り下げる():
    world = World(impl_supervisor, run_supervisor(), asks_then_replans, script=replan_on_ask_script)
    start(world)
    world.run_stages()
    assert world.run.finished
    (withdrawn,) = world.events(QuestionWithdrawn)
    assert withdrawn.question == QuestionId("q-scope")
    questions = world.get(StreamId.questions())
    assert isinstance(questions, Questions)
    assert not questions.awaiting_answer
    assert "withdraw-questions" in world.fired


def asks_user_when_woken(world: World, eid: EventId, event: Event) -> list[Command]:
    """ラン統括: asks_user と同じだが、回答には wake_for が起こすときだけ応じる（driver と同じ）。"""
    if isinstance(event, AnswerRecorded) and wake_for(event, eid) is None:
        return []
    return asks_user(world, eid, event)


def test_回答とラン統括の再計画が前後したら回答に閉じた印が立ちランは終わりまで進む():
    # 起こさないことそのものは、test_supervision.py の表と test_run.py で確かめている
    world = World(
        impl_supervisor, run_supervisor(), asks_user_when_woken, script=replan_on_ask_script
    )
    start(world)
    world.run_stages()
    escalation = world.last_id(EscalationRaised, StreamId.run())
    # ユーザーが答えた（QuestionAnswered）後、その回答が Run に届く前に、ラン統括が再計画で閉じた。
    # 質問はもう answered なので取り下げられない
    world.process(
        AnswerQuestion(
            command_id=world.command_id(),
            issuer=Issuer.cli(),
            question=QuestionId("q-scope"),
            answer="A",
        )
    )
    world.process(
        RequestReplan(
            command_id=world.command_id(),
            issuer=RUN_SUPERVISOR,
            reason="指示を読み直して割り直す",
            trigger=escalation,
        )
    )
    world.pump()
    world.run_stages()
    (recorded,) = world.events(AnswerRecorded)
    assert recorded.escalation_closed
    # ラン統括が回答に応じなくても、再計画からランが仕上げまで進む
    assert world.events(EscalationAnswered) == []
    assert world.run.finished


def refused_script(world: World, execution: ExecutionId) -> Outcome | None:
    if execution.stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1)]})
    if execution.stage is S.REVIEW and execution.attempt == 1:
        # 本文が空白だけの指摘（スキーマの minLength は通る）。台帳は受けない
        return Outcome(result={"findings": [{**must_fix(), "body": " "}]})
    return None


def flow_again_on_refused(world: World, eid: EventId, event: Event) -> list[Command]:
    """実装タスクの統括: 結果を受けなかったと上がってきたら、同じフローで応える（同じステージから）。"""
    if not (isinstance(event, EscalationRaised) and event.kind is E.RESULT_REFUSED):
        return []
    task = task_of(eid)
    return [
        AcceptFlow(
            command_id=world.command_id(),
            issuer=Issuer.task_supervisor(task, SESSION),
            task=task,
            steps=IMPL_FLOW,
            responds_to=eid,
        )
    ]


def test_受け取る側が受けなかった結果は上げて統括が応えたら走らせ直す():
    world = World(impl_supervisor, run_supervisor(), flow_again_on_refused, script=refused_script)
    start(world)
    world.run_stages()
    (refused,) = [e for e in world.events(EscalationRaised) if e.kind is E.RESULT_REFUSED]
    assert refused.reason == "本文が空の指摘がある"
    assert refused.origin == ExecutionId(TaskId("task1"), S.REVIEW, 1, 1)
    assert world.run.finished
    assert "confirm-refused" in world.fired


def reverted_script(world: World, execution: ExecutionId) -> Outcome | None:
    stage, round = execution.stage, execution.round
    if stage in (S.PLAN, S.REVISE):
        return Outcome(result={"tasks": [planned(1)]})
    if stage is S.DESIGN_REVIEW and round == 1:
        return Outcome(result={"findings": [must_fix("依存が輪になる")]})
    if stage is S.DESIGN_JUDGE and round == 2 and not world.events(DesignReverted):
        cause = {"kind": "reverted", "revertedTo": 1, "reason": "v1 に戻った", "question": None}
        return Outcome(result={"designCause": cause})
    if stage is S.DESIGN_JUDGE and world.events(DesignReverted):
        return Outcome(result={"verdicts": [verdict("D1")]})
    return None


def answers_itself(world: World, eid: EventId, event: Event) -> list[Command]:
    """ラン統括: 上がってきたエスカレーションに自分で答える。"""
    if isinstance(event, EscalationRaised) and eid.stream == StreamId.run():
        return [
            AnswerEscalation(
                command_id=world.command_id(),
                issuer=RUN_SUPERVISOR,
                escalation=eid,
                answer="v2 の形でよい",
            )
        ]
    return []


def test_設計が前の版に戻ったらラン統括の回答を持ってReviseから続ける():
    world = World(impl_supervisor, run_supervisor(), answers_itself, script=reverted_script)
    start(world)
    world.run_stages()
    (reverted,) = world.events(DesignReverted)
    assert reverted.to_version.value == 1
    # DesignJudge の理由は、計画タスクが判定の実行の結果から読んで載せ、ラン統括まで中継した
    raised = [e for e in world.events(EscalationRaised) if e.kind is E.DESIGN_REVERTED]
    assert [e.reason for e in raised] == ["v1 に戻った", "v1 に戻った"]
    resolved = world.events(EscalationResolved)[0]
    assert resolved.kind is E.DESIGN_REVERTED
    assert world.run.finished
    assert {"escalate-reverted", "resume-design"} <= set(world.fired)


def stall_script(world: World, execution: ExecutionId) -> Outcome | None:
    stage, round = execution.stage, execution.round
    if stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1)]})
    if stage is S.REVIEW and round == 1:
        return Outcome(result={"findings": [must_fix()]})
    if stage is S.JUDGE and round == 3:
        return Outcome(result={"stallCause": "tests", "stallReason": "テストが受入条件と合わない"})
    if stage is S.JUDGE and round > 3:
        return Outcome(result={"verdicts": [verdict("R1")]})
    return None


def target_script(*, retargeted_by_judge: bool) -> Script:
    """1 ラウンド目の Review の R1 がテストを足すしかない指摘（Judge が書き換える形もある）。"""

    def script(world: World, execution: ExecutionId) -> Outcome | None:
        stage, round = execution.stage, execution.round
        if stage is S.PLAN:
            return Outcome(result={"tasks": [planned(1)]})
        if stage is S.REVIEW and round == 1:
            finding = {
                **must_fix("_bash_patch を確かめるテストが無い"),
                "location": "src/autodev/adapters/claude/guard.py",
                "target": "impl" if retargeted_by_judge else "tests",
            }
            return Outcome(result={"findings": [finding]})
        if stage is S.JUDGE and round == 1 and retargeted_by_judge:
            moved = {**verdict("R1", "open"), "target": "tests"}
            return Outcome(result={"verdicts": [moved]})
        if stage is S.JUDGE and round > 1:
            return Outcome(result={"verdicts": [verdict("R1")]})
        return None

    return script


@pytest.mark.parametrize(
    "retargeted_by_judge", [False, True], ids=["Reviewが付けた", "Judgeが書き換えた"]
)
def test_テストを足すしかない指摘はFixを通らず1回の往復でTestFixへ回り修正はtestsで数える(
    retargeted_by_judge: bool,
):
    script = target_script(retargeted_by_judge=retargeted_by_judge)
    world = World(impl_supervisor, run_supervisor(), script=script)
    start(world)
    world.run_stages()
    task = TaskId("task1")
    asked = [(e.execution.stage, e.execution.round) for e in world.events(StageRequested)]
    assert (S.TEST_FIX, 1) in asked
    assert S.FIX not in {stage for stage, _ in asked}
    # 同じラウンドで TestFix が走り、その後に次のラウンドの頭が走る
    assert asked.index((S.TEST_FIX, 1)) < asked.index((S.REVIEW, 2))
    (counted,) = world.events(FixCounted)
    assert counted.execution == ExecutionId(task, S.TEST_FIX, 1, 1)
    assert counted.findings == (FindingId("R1"),)
    assert "count-fix" in world.fired
    assert world.run.finished


def asks_run(world: World, eid: EventId, event: Event) -> list[Command]:
    """実装タスクの統括: Review を始めたときに自分から相談し（上げ元が無い）、停滞はそれに応じて上げる。"""
    task = TaskId("task1")
    issuer = Issuer.task_supervisor(task, SESSION)
    if isinstance(event, StageStarted) and event.execution == ExecutionId(task, S.REVIEW, 1, 1):
        return [
            EscalateToRun(
                command_id=world.command_id(),
                issuer=issuer,
                task=task,
                kind=E.NEEDS_HUMAN,
                reason="受入条件の書き方を確かめたい",
                pointers=Pointers(),
            )
        ]
    if isinstance(event, EscalationRaised) and event.kind is E.STALL and is_task_stream(eid):
        return [
            EscalateToRun(
                command_id=world.command_id(),
                issuer=issuer,
                task=task,
                kind=E.NEEDS_HUMAN,
                reason=event.reason,
                pointers=event.pointers,
                hint=event.hint,
                source=eid,
            )
        ]
    return []


def test_停滞はJudgeの分類と理由を載せて上げ上げ元を書かない回答はnotesに残る():
    world = World(impl_supervisor, run_supervisor(), asks_run, answers_itself, script=stall_script)
    start(world)
    world.run_stages()
    assert world.run.finished
    (stalled,) = [e for e in world.events(EscalationRaised) if e.kind is E.STALL and e.task]
    # Judge の停滞の分類が、台帳の FindingsEvaluated → ConcludeReviewRound を経てヒントに載り、
    # 理由は Judge の結果から Task が読んだ
    assert stalled.hint.stall_cause.value == "tests"
    assert str(stalled.hint.finding_ids[0]) == "R1"
    assert stalled.reason == "テストが受入条件と合わない"
    # 上げ元を書かない相談への回答は notes に残り、停滞への回答はタスクへ下りた
    assert {"note-answer", "resolve-task-side"} <= set(world.fired)
    assert any(isinstance(e, NoteAdded) for e in world.events(NoteAdded))


def replan_script(world: World, execution: ExecutionId) -> Outcome | None:
    stage = execution.stage
    if stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1), planned(2)]})
    if stage is S.REPLAN:
        return Outcome(result={"tasks": [planned(2)], "discard": ["task1"]})
    if stage is S.REVISE:
        return Outcome(result={"tasks": [planned(2)], "discard": ["task1"]})
    return None


def test_積んだタスクを破棄する再計画は閉じ終えてから上のタスクを新しいブランチで積み直す():
    world = World(impl_supervisor, run_supervisor(replan_once=True), script=replan_script)
    start(world)
    world.run_stages()
    run = world.run
    assert run.finished
    assert run.tasks[TaskId("task1")].status is TaskStatus.DISCARDED
    (entry,) = world.stack.entries
    # 積み直した task2 は、新しい名前のブランチで、残した一番上（概要 PR）の上に載った
    assert entry.task == TaskId("task2")
    assert entry.branch == BranchName.for_task(NAME, 2, branch_round=1)
    assert entry.base == BranchName.overview(NAME)
    # 積む列へ戻す一覧は Run だけが持ち、閉じ終えた（StackCutBack）後に戻した
    (returned,) = world.events(TasksReturnedToQueue)
    assert returned.tasks == (TaskId("task2"),)
    assert {
        "cut-stack-top",
        "discard-proposal",
        "discard-job",
        "hand-cut-back",
        "return-to-queue",
        "requeue",
    } <= set(world.fired)


def panic_script(world: World, execution: ExecutionId) -> Outcome | None:
    if execution.stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1)]})
    if execution.stage is S.IMPL and not world.events(RunResumed):
        return HOLD
    return None


def test_パニックの後に呼び直されたら止めた実行を続きから再開する():
    world = World(impl_supervisor, run_supervisor(), script=panic_script)
    start(world)
    world.run_stages()
    task1 = TaskId("task1")
    (impl,) = world.task(task1).running_executions()
    world.outside(Panic, Issuer.driver(), cause="429")
    # driver はドメインに running の実行を聞き、パニックで止めたとして interrupted にする
    for execution in world.task(task1).running_executions():
        world.outside(
            MarkInterrupted,
            Issuer.driver(),
            task=task1,
            execution=execution,
            cause=InterruptCause.PANIC,
        )
    assert world.task(task1).running_executions() == []
    world.outside(ResumeRun, Issuer.cli())
    assert world.task(task1).running_executions() == [impl]
    world.run_stages()
    assert world.run.finished
    assert "resume-tasks" in world.fired


def integration_script(world: World, execution: ExecutionId) -> Outcome | None:
    if execution.stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1), planned(2)]})
    flow = world.task(GIT).flow
    job = flow.job if flow is not None else None
    if job is None or job.task != TaskId("task2") or world.events(TaskInserted):
        return None
    if execution.stage is S.REBASE:
        return Outcome(evidence={"conflicts": ("src/cache.py",)})
    if execution.stage is S.CHECK_UNION:
        verdict = UnionVerdict((UnionFileVerdict("src/cache.py", kept_both=False),))
        return Outcome(evidence={"union": verdict})
    return None


def takes_over(world: World, eid: EventId, event: Event) -> list[Command]:
    """ラン統括: 統合に失敗したタスクを引き継ぐタスクを差し込む。"""
    if not (isinstance(event, EscalationRaised) and eid.stream == StreamId.run()):
        return []
    if event.kind is not E.INTEGRATION_FAILED:
        return []
    return [
        InsertTask(
            command_id=world.command_id(),
            issuer=RUN_SUPERVISOR,
            spec=TaskSpec("task2 をやり直す"),
            takes_over=TaskId("task2"),
            responds_to=eid,
        )
    ]


def test_統合に失敗したら仕事を捨て引き継ぐタスクが衝突したファイルを持って始まる():
    world = World(impl_supervisor, run_supervisor(), takes_over, script=integration_script)
    start(world)
    world.run_stages()
    run = world.run
    assert run.tasks[TaskId("task2")].status is TaskStatus.SUPERSEDED
    task3 = world.task(TaskId("task3"))
    assert task3.conflict_files == ("src/cache.py",)
    assert ArtifactKind.CONFLICTS in task3.artifacts
    # git 管理タスクは統合の失敗のフローを捨てて、次の仕事へ進んだ
    assert world.events(FlowAbandoned)[0].job is not None
    assert run.finished
    assert stacked_tasks(world) == [TaskId("task1"), TaskId("task3")]
    assert {
        "reject-integration",
        "record-integration-failure",
        "close-task-side",
        "start-ready-after-inserted",
    } <= set(world.fired)


def stop_script(world: World, execution: ExecutionId) -> Outcome | None:
    if execution.stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1), planned(2, 1)]})
    if execution.stage is S.PUSH:
        return HOLD
    return None


def test_積んでいる途中のタスクを止めたら仕事を外しgit管理タスクのフローを捨てる():
    world = World(impl_supervisor, run_supervisor(), script=stop_script)
    start(world)
    world.run_stages()
    (push,) = world.held
    world.outside(
        StopTasks,
        RUN_SUPERVISOR,
        tasks=frozenset({TaskId("task1"), TaskId("task2")}),
    )
    git = world.task(GIT)
    assert git.executions[push].status is ExecutionStatus.INTERRUPTED
    # 積む仕事は捨てたフローとともに終わり、次の仕事（全部止めたので仕上げ）を取り出した
    (finished,) = [e.job for e in world.events(GitJobFinished) if e.job.kind is J.STACK]
    assert finished.task == TaskId("task1")
    world.run_stages()
    assert world.run.finished
    assert world.stack.entries == []
    assert {"stop-tasks", "abandon-withdrawn"} <= set(world.fired)


T2 = TaskId("task2")
T3 = TaskId("task3")


def rescope_script(world: World, execution: ExecutionId) -> Outcome | None:
    stage, round, task = execution.stage, execution.round, execution.task
    if stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1), planned(2)]})
    if stage in (S.REPLAN, S.REVISE):
        replanned = [planned(1), planned(2, title="変えた"), planned(3)]
        carry = [{"finding": "R1", "fromTask": "task2", "toTask": "task3"}]
        return Outcome(result={"tasks": replanned, "carry": carry})
    if stage is S.REVIEW and round == 1 and task == T2 and execution.attempt == 1:
        return Outcome(result={"findings": [must_fix()]})
    if stage is S.FIX and task == T2 and len(world.events(TasksPlanned)) == 1:
        return HOLD
    if stage is S.JUDGE and task == T3:
        # 移ってきた指摘は、移した先のタスクの Judge が判定する
        return Outcome(result={"verdicts": [verdict("R1")]})
    return None


def test_再計画で範囲が変わった走っているタスクに知らせ未解決の指摘を移す():
    world = World(impl_supervisor, run_supervisor(), script=rescope_script)
    start(world)
    world.run_stages()
    # task1 は積み終え、task2 は指摘を直している途中（Fix が走っている）
    (fix,) = world.held
    assert world.run.tasks[TaskId("task1")].status is TaskStatus.STACKED
    world.outside(RequestReplan, RUN_SUPERVISOR, reason="範囲を見直す")
    world.run_stages()
    replanned = world.events(TasksPlanned)[1]
    assert replanned.scope_changed == {T2}
    assert world.task(T2).spec == TaskSpec("変えた", acceptance=(), scope=())
    # 範囲が変わる前のフローの Fix は、終わるまで走らせてから新しいフローへ進む
    world.held.clear()
    world.report(fix, Outcome())
    world.pump()
    world.run_stages()
    assert world.run.finished
    # 移した指摘は、移す元で carried になり、移した先の台帳に移管元つきで立った
    ledger2 = world.get(StreamId.review(T2))
    ledger3 = world.get(StreamId.review(T3))
    assert isinstance(ledger2, ReviewLedger) and isinstance(ledger3, ReviewLedger)
    (origin,) = ledger2.findings.values()
    assert origin.status is FindingStatus.CARRIED
    (carried,) = ledger3.findings.values()
    assert carried.carried_from == FindingOrigin(StreamId.review(T2), origin.id)
    assert {"change-scope", "carry-findings", "raise-carried", "status-running"} <= set(world.fired)


T1 = TaskId("task1")


def gated_in_replan_script(world: World, execution: ExecutionId) -> Outcome | None:
    stage, task = execution.stage, execution.task
    if stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1), planned(2), planned(3)]})
    if stage in (S.REPLAN, S.REVISE):
        return Outcome(result={"tasks": [planned(1), planned(2, title="変えた"), planned(3)]})
    if stage is S.WRITE_PR_BODY and task in (T2, T3) and not world.events(ReplanRequested):
        return HOLD
    return None


def test_再計画が進んでいる間にフローを終えたタスクは積まずに待ち範囲が変われば始め直す():
    world = World(impl_supervisor, run_supervisor(), script=gated_in_replan_script)
    start(world)
    world.run_stages()
    # task1 は積み終え、task2・task3 はフローの最後のステージを走らせている
    assert world.run.tasks[T1].status is TaskStatus.STACKED
    assert len(world.held) == 2
    world.outside(RequestReplan, RUN_SUPERVISOR, reason="範囲を見直す")
    for held in world.held:
        world.report(held, Outcome())
    world.held.clear()
    world.pump()
    world.run_stages()
    assert world.run.finished
    replanned = world.events(TasksPlanned)[1]
    # 範囲が変わった task2 は積まずに列から外し、新しいブランチで始め直してから積んだ
    assert replanned.withdraw == {T2}
    assert stacked_tasks(world) == [T1, T3, T2]
    assert world.stack.entries[-1].branch == BranchName.for_task(NAME, 2, branch_round=1)
    (reopened,) = [e for e in world.events(TaskStarted) if e.reopened]
    assert reopened.task == T2
    # stack-top を切り直す仕事を終えた時、task3 の積む仕事は列にあったが、反映するまで取り出さなかった
    order = [eid for eid, _ in world.log]

    def at(cls: type[Event], match: Callable[[Any], bool]) -> int:
        return order.index(next(eid for eid, e in world.log if isinstance(e, cls) and match(e)))

    queued3 = at(GitJobQueued, lambda e: e.job.kind is J.STACK and e.job.task == T3)
    cut_top = at(GitJobFinished, lambda e: e.job.kind is J.CUT_STACK_TOP)
    taken3 = at(GitJobTaken, lambda e: e.job.kind is J.STACK and e.job.task == T3)
    assert queued3 < cut_top < order.index(world.last_id(TasksPlanned)) < taken3
    assert {"pause-stacking", "resume-stacking", "withdraw-rescoped", "rescope-reopened"} <= set(
        world.fired
    )


def stops_failed_task(world: World, eid: EventId, event: Event) -> list[Command]:
    """ラン統括: 統合の失敗に、エスカレーションに応えずに（responds_to なしで）タスクを止めて応じる。"""
    if not (isinstance(event, EscalationRaised) and eid.stream == StreamId.run()):
        return []
    if event.kind is not E.INTEGRATION_FAILED:
        return []
    return [StopTasks(command_id=world.command_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({T2}))]


def test_捨てたフローで上げたエスカレーションはフローと一緒に閉じRunの側の中継も閉じる():
    world = World(impl_supervisor, run_supervisor(), stops_failed_task, script=integration_script)
    start(world)
    world.run_stages()
    # 止めたタスクの積む仕事を外して git 管理タスクのフローを捨て、そのフローで上げた統合の失敗も閉じた
    assert world.run.finished
    assert world.run.tasks[GIT].status is TaskStatus.FINISHED
    assert world.run.tasks[T2].status is TaskStatus.DROPPED
    assert world.task(GIT).escalations == {}
    assert world.run.escalations == {}
    (finished,) = [
        e for e in world.events(GitJobFinished) if e.job.task == T2 and e.job.kind is J.STACK
    ]
    assert finished.outcome is GitJobOutcome.WITHDRAWN
    assert "close-run-side" in world.fired


def failing_close_script(world: World, execution: ExecutionId) -> Outcome | None:
    # 破棄した PR を閉じる CloseRPs が 2 回続けて落ちて上げる。仕事を戻した後は通る
    if execution.stage is S.CLOSE_PRS and execution.attempt <= 2:
        return Outcome(evidence={"exit": StageExit.ERROR, "error": "gh が落ちた"})
    return replan_script(world, execution)


def answers_git_by_insert(world: World, eid: EventId, event: Event) -> list[Command]:
    """ラン統括: git 管理タスクから上がってきたものに、別のタスクを差し込んで応える（仕事のフローを捨てる）。"""
    if not (isinstance(event, EscalationRaised) and eid.stream == StreamId.run()):
        return []
    if event.task != GIT:
        return []
    return [
        InsertTask(
            command_id=world.command_id(),
            issuer=RUN_SUPERVISOR,
            spec=TaskSpec("別の作業"),
            responds_to=eid,
        )
    ]


def test_取り下げでも統合の失敗でもない仕事のフローを捨てたら仕事を列の先頭へ戻す():
    world = World(
        impl_supervisor,
        run_supervisor(replan_once=True),
        answers_git_by_insert,
        script=failing_close_script,
    )
    start(world)
    world.run_stages()
    # 破棄する仕事は消えずにやり直され、閉じ終えてランが終わった
    assert world.run.finished
    assert world.stack.cuts_pending == 0
    discards = [e for e in world.events(GitJobFinished) if e.job.kind is J.DISCARD]
    assert [e.outcome for e in discards] == [GitJobOutcome.ABANDONED, GitJobOutcome.DONE]
    assert discards[0].job.id == discards[1].job.id


def panic_push_script(world: World, execution: ExecutionId) -> Outcome | None:
    if execution.stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1), planned(2, 1)]})
    if execution.stage is S.PUSH and not world.run.panicked and not world.events(RunResumed):
        return HOLD
    return None


def test_パニックの間に積み終えたタスクの後は呼び直されてから次のタスクを始める():
    world = World(impl_supervisor, run_supervisor(), script=panic_push_script)
    start(world)
    world.run_stages()
    (push,) = world.held
    world.outside(Panic, Issuer.driver(), cause="429")
    # driver が止める前に、積む途中の Push が終わった。task2 はパニックの間は始めない
    world.held.clear()
    world.report(push, Outcome())
    world.pump()
    world.run_stages()
    assert world.run.tasks[T1].status is TaskStatus.STACKED
    assert world.run.tasks[T2].status is TaskStatus.PENDING
    world.outside(ResumeRun, Issuer.cli())
    world.run_stages()
    assert world.run.finished
    assert "start-ready-after-resumed" in world.fired


STUCK_PREFIX = "git の仕事"


def always_failing_close_script(world: World, execution: ExecutionId) -> Outcome | None:
    # 1 つのフローで 2 回落ちて上げる。戻すたびに落ち、上限まで戻した後に続けると答えたら通る
    if execution.stage is S.CLOSE_PRS and execution.attempt <= 2 * (MAX_JOB_RETURNS + 1):
        return Outcome(evidence={"exit": StageExit.ERROR, "error": "gh が落ちた"})
    return replan_script(world, execution)


def continues_stuck_job(world: World, eid: EventId, event: Event) -> list[Command]:
    """ラン統括: 止めた仕事には続けると答え、ほかの git 管理タスクの上げには差し込んで応える。"""
    if not (isinstance(event, EscalationRaised) and eid.stream == StreamId.run()):
        return []
    if event.task != GIT:
        return []
    if event.reason.startswith(STUCK_PREFIX):
        return [
            AnswerEscalation(
                command_id=world.command_id(),
                issuer=RUN_SUPERVISOR,
                escalation=eid,
                answer="続ける",
            )
        ]
    return answers_git_by_insert(world, eid, event)


def test_戻した回数が上限に達した仕事は止めてラン統括が続けると答えたらまた戻す():
    world = World(
        impl_supervisor,
        run_supervisor(replan_once=True),
        continues_stuck_job,
        script=always_failing_close_script,
    )
    start(world)
    world.run_stages()
    assert world.run.finished
    discards = [e.outcome for e in world.events(GitJobFinished) if e.job.kind is J.DISCARD]
    returned = [GitJobOutcome.ABANDONED] * MAX_JOB_RETURNS
    assert discards == [*returned, GitJobOutcome.STUCK, GitJobOutcome.DONE]
    (retried,) = world.events(GitJobRetried)
    assert retried.job.kind is J.DISCARD
    # 破棄はやめるとランが終わらないので、答え以外では閉じられない上げにした（Run の側にも写す）
    stuck = [e for e in world.events(EscalationRaised) if e.reason.startswith(STUCK_PREFIX)]
    assert len(stuck) == 2 and all(e.answer_only for e in stuck)
    assert {"escalate-stuck-job", "retry-stuck-job"} <= set(world.fired)
    # ほかの上げ（上げ元の実行がある stage-errors）を答え以外で閉じても、止めた仕事はやめない
    assert "drop-stuck-job" not in world.fired


def always_failing_push_script(world: World, execution: ExecutionId) -> Outcome | None:
    # task1 を積む仕事の Push が、1 つのフローで 2 回落ちて上げる。戻すたびに落ちる
    flow = world.task(GIT).flow
    job = flow.job if flow is not None else None
    if execution.stage is S.PUSH and job is not None and job.task == T1:
        return Outcome(evidence={"exit": StageExit.ERROR, "error": "push が落ちた"})
    return main_script(world, execution)


def test_止めた仕事がやめてよい仕事なら答え以外の判断で閉じるとやめる():
    world = World(
        impl_supervisor, run_supervisor(), answers_git_by_insert, script=always_failing_push_script
    )
    start(world)
    world.run_stages()
    stuck = [e for e in world.events(EscalationRaised) if e.reason.startswith(STUCK_PREFIX)]
    # 積む仕事は、やめても相手のタスクを止めるか再計画すれば済むので、答え以外でも閉じられる
    assert len(stuck) == 2 and not any(e.answer_only for e in stuck)
    (dropped,) = world.events(GitJobDropped)
    assert (dropped.job.kind, dropped.job.task) == (J.STACK, T1)
    assert world.fired["drop-stuck-job"] == 1


def union_once_script(world: World, execution: ExecutionId) -> Outcome | None:
    if execution.stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1), planned(2)]})
    flow = world.task(GIT).flow
    job = flow.job if flow is not None else None
    if job is None or job.task != T2:
        return None
    if execution.stage is S.REBASE:
        return Outcome(evidence={"conflicts": ("src/cache.py",)})
    if execution.stage is S.CHECK_UNION and not world.events(EscalationResolved):
        verdict = UnionVerdict((UnionFileVerdict("src/cache.py", kept_both=False),))
        return Outcome(evidence={"union": verdict})
    return None


def test_統合の失敗に答えて同じ仕事でやり直したら失敗の印を下ろす():
    world = World(impl_supervisor, run_supervisor(), answers_itself, script=union_once_script)
    start(world)
    world.run_stages()
    assert world.run.finished
    assert stacked_tasks(world) == [TaskId("task1"), T2]
    # 統合の失敗は開いている間だけ印を立て、答えてやり直したので、もう引き継げない
    assert not world.run.tasks[T2].integration_failed
    assert world.events(IntegrationRetried) == [IntegrationRetried(T2)]
    assert {"retry-integration", "clear-integration-failure"} <= set(world.fired)


def push_fails_in_replan_script(world: World, execution: ExecutionId) -> Outcome | None:
    stage = execution.stage
    if stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1)]})
    if stage in (S.REPLAN, S.REVISE):
        return Outcome(result={"tasks": [planned(1, title="変えた")]})
    if stage is S.PUSH and not world.events(ReplanRequested):
        return HOLD
    if stage is S.PUSH and execution.attempt == 2:
        return Outcome(evidence={"exit": StageExit.ERROR, "error": "push が落ちた"})
    return None


def test_再計画の間に積む仕事のフローを捨てて残したら相手をgatedに戻し範囲が変われば外す():
    world = World(
        impl_supervisor, run_supervisor(), answers_git_by_insert, script=push_fails_in_replan_script
    )
    start(world)
    world.run_stages()
    (push,) = world.held
    world.outside(RequestReplan, RUN_SUPERVISOR, reason="範囲を見直す")
    # 再計画の間に、積む途中の Push が 2 回続けて落ちて上げ、ラン統括が差し込んで応えた
    world.held.clear()
    world.report(push, Outcome(evidence={"exit": StageExit.ERROR, "error": "push が落ちた"}))
    world.pump()
    world.run_stages()
    assert world.run.finished
    replanned = world.events(TasksPlanned)[1]
    assert replanned.withdraw == {TaskId("task1")}
    task1 = next(e for e in world.stack.entries if e.task == TaskId("task1"))
    assert task1.branch == BranchName.for_task(NAME, 1, branch_round=1)
    assert "regate-kept-stack-job" in world.fired


def start_one_at_a_time(world: World) -> None:
    world.outside(
        StartRun,
        Issuer.cli(),
        name=NAME,
        instruction=Instruction("キャッシュを足す"),
        repository=Repository("/repo"),
        base=BASE,
        limit=ParallelLimit(1),
    )


def slot_script(world: World, execution: ExecutionId) -> Outcome | None:
    if execution.stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1), planned(2)]})
    # task1 を積む途中で止めておく。枠が空いたことで task2 が始まらなければ、ここで流れが止まる
    if execution.stage is S.PUSH and T2 not in {e.task for e in world.events(TaskStarted)}:
        return HOLD
    return None


def test_フローを終えて並列の枠が空いたら積み終える前に次のタスクを始める():
    world = World(impl_supervisor, run_supervisor(), script=slot_script)
    start_one_at_a_time(world)
    world.run_stages()
    assert world.held == []
    assert world.run.finished
    started2 = world.last_id(TaskStarted)
    stacked1 = next(eid for eid, e in world.log if isinstance(e, TaskMarkedStacked))
    order = [eid for eid, _ in world.log]
    assert order.index(started2) < order.index(stacked1)
    assert "start-ready-after-slot-freed" in world.fired


def stop_running_script(world: World, execution: ExecutionId) -> Outcome | None:
    if execution.stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1), planned(2)]})
    if execution.stage is S.IMPL and execution.task == TaskId("task1"):
        return HOLD
    return None


def test_走っているタスクを止めたら空いた枠で次のタスクを始める():
    world = World(impl_supervisor, run_supervisor(), script=stop_running_script)
    start_one_at_a_time(world)
    world.run_stages()
    assert world.run.tasks[T2].status is TaskStatus.PENDING
    world.outside(StopTasks, RUN_SUPERVISOR, tasks=frozenset({TaskId("task1")}))
    world.run_stages()
    assert world.run.finished
    assert stacked_tasks(world) == [T2]
    assert "start-ready-after-stopped" in world.fired


# =====================================================================================
# 表そのもの
# =====================================================================================

FLOWS: tuple[Callable[[], None], ...] = (
    test_主な流れはランの開始から仕上げまでイベントの欄だけで進む,
    test_計画ステージのaskはユーザーの回答で同じ実行を続きから再開する,
    test_計画ステージのaskに再計画で応じたら止まった実行を捨てPrepareから組み直す,
    test_受け取る側が受けなかった結果は上げて統括が応えたら走らせ直す,
    test_設計が前の版に戻ったらラン統括の回答を持ってReviseから続ける,
    test_停滞はJudgeの分類と理由を載せて上げ上げ元を書かない回答はnotesに残る,
    test_積んだタスクを破棄する再計画は閉じ終えてから上のタスクを新しいブランチで積み直す,
    test_パニックの後に呼び直されたら止めた実行を続きから再開する,
    test_統合に失敗したら仕事を捨て引き継ぐタスクが衝突したファイルを持って始まる,
    test_積んでいる途中のタスクを止めたら仕事を外しgit管理タスクのフローを捨てる,
    test_再計画で範囲が変わった走っているタスクに知らせ未解決の指摘を移す,
    test_再計画が進んでいる間にフローを終えたタスクは積まずに待ち範囲が変われば始め直す,
    test_捨てたフローで上げたエスカレーションはフローと一緒に閉じRunの側の中継も閉じる,
    test_取り下げでも統合の失敗でもない仕事のフローを捨てたら仕事を列の先頭へ戻す,
    test_パニックの間に積み終えたタスクの後は呼び直されてから次のタスクを始める,
    test_フローを終えて並列の枠が空いたら積み終える前に次のタスクを始める,
    test_走っているタスクを止めたら空いた枠で次のタスクを始める,
    test_戻した回数が上限に達した仕事は止めてラン統括が続けると答えたらまた戻す,
    test_止めた仕事がやめてよい仕事なら答え以外の判断で閉じるとやめる,
    test_統合の失敗に答えて同じ仕事でやり直したら失敗の印を下ろす,
    test_再計画の間に積む仕事のフローを捨てて残したら相手をgatedに戻し範囲が変われば外す,
)

#: 表のうち、上の流れでは通らないもの（と、通らない理由）。次の段で流れを足したら消す
NOT_IN_FLOWS: dict[str, str] = {
    "ask-user-for-supervisor": "driver が出す ReportSupervisorFailure（test_driver で通す）",
    "escalate-ambiguous": "DesignJudge の designCause の ambiguous（escalate-reverted と同じ形）",
    "escalate-rounds-exhausted": "MAX_DESIGN_ROUNDS 回の設計の往復（escalate-reverted と同じ形）",
}


def test_表のつなぎ目はどれも流れのどこかで使われる():
    World.created.clear()
    for flow in FLOWS:
        flow()
    fired = set().union(*(world.fired for world in World.created))
    unused = {seam.name for seam in SEAMS} - fired - set(NOT_IN_FLOWS)
    assert unused == set()
    assert not set(NOT_IN_FLOWS) & fired, "流れで通るようになったら NOT_IN_FLOWS から消す"


def test_表の名前は重ならずイベントの欄の説明を持つ():
    names = [seam.name for seam in SEAMS]
    assert len(names) == len(set(names))
    assert all(seam.reads for seam in SEAMS)
    assert set(NOT_IN_FLOWS) <= set(names)
