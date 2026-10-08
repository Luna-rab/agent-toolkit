"""ポリシーの表。受け手として登録する行（`RECEIVERS`）と、反応の続き（`FOLLOW_UPS`）。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from ..commands.base import Command
from ..events.base import Event
from ..events.design import (
    DesignAmbiguous,
    DesignProposed,
    DesignReverted,
    DesignRevisionStarted,
    DesignRoundsExhausted,
    DesignSettled,
)
from ..events.questions import QuestionAnswered
from ..events.review_ledger import FindingCarried, FindingsEvaluated, ResultReceived, ResultRefused
from ..events.run import (
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
from ..events.stack import (
    GitJobDropped,
    GitJobFinished,
    GitJobQueued,
    GitJobRetried,
    GitJobTaken,
    GitJobWithdrawn,
    IntegrationFailed,
    IntegrationRetried,
    StackCutBack,
    StackingResumed,
    TaskStacked,
)
from ..events.task import (
    BranchRebased,
    EscalationClosed,
    EscalationResolved,
    FlowAbandoned,
    FlowAccepted,
    FlowFinished,
    GateFailed,
    ScopeChanged,
    StageCompleted,
    StageReported,
    TaskGated,
    WorktreeReady,
)
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.event_id import EventId
from ..value_objects.task_status import TaskStatus
from .base import By, Policy, Rule
from .design import (
    conclude_revision,
    conclude_settled,
    escalate_design_wait,
    record_settled,
    reject_appendix,
    track_proposal,
)
from .questions import record_answer
from .review_ledger import conclude_round, confirm_received, confirm_refused, raise_carried
from .run import (
    apply_first_plan,
    ask_user_for_supervisor,
    carry_findings,
    change_scope,
    close_task_side,
    cut_stack_top,
    cut_task_branch,
    discard_job,
    discard_proposal,
    finish_job,
    note_answer,
    open_task,
    pause_stacking,
    plan_again,
    requeue,
    rescope_reopened,
    resolve_task_side,
    resume_stacking,
    resume_tasks,
    start_planning_and_git,
    start_ready,
    start_ready_after_slot_freed,
    stop_tasks,
    withdraw_questions,
    withdraw_rescoped,
    write_overview,
)
from .stack import (
    abandon_withdrawn,
    clear_integration_failure,
    drop_stuck_job,
    escalate_stuck_job,
    flow_for_job,
    mark_stacked,
    record_integration_failure,
    regate_kept_stack_job,
    retry_integration,
    retry_stuck_job,
    return_to_queue,
    take_next,
)
from .task import (
    close_run_side,
    comment_findings,
    count_fix,
    enqueue_gated,
    finish_git_job,
    gate_failed,
    gate_passed,
    git_finished,
    hand_cut_back,
    hand_entry,
    hand_findings,
    hand_judgement,
    hand_overview,
    hand_proposal,
    plan_on_worktree,
    record_conflict,
    record_cut_base,
    record_rebased_base,
    reject_integration,
    relay_to_run,
    resume_ask,
    resume_design,
    status,
)

_E = EscalationKind


_SUPERVISOR = By.SUPERVISOR


def _row(
    name: str, on: type[Event] | tuple[type[Event], ...], rule: Rule, by: By = By.POLICY
) -> Policy:
    return Policy(name, on if isinstance(on, tuple) else (on,), rule, by)


#: 受け手として登録する行（この順に登録する）。行の名前と並びは test_seams.py の SEAMS と同じ
POLICIES: tuple[Policy, ...] = (
    # Run
    _row("start-planning-and-git", RunStarted, start_planning_and_git),
    _row("open-task", TaskStarted, open_task),
    _row("rescope-reopened", TaskStarted, rescope_reopened),
    _row("cut-task-branch", TaskStarted, cut_task_branch),
    _row("apply-first-plan", SettledPlanRecorded, apply_first_plan),
    _row("start-ready-after-plan", TasksPlanned, start_ready),
    _row("write-overview", TasksPlanned, write_overview),
    _row("change-scope", TasksPlanned, change_scope),
    _row("withdraw-rescoped", TasksPlanned, withdraw_rescoped),
    _row("carry-findings", TasksPlanned, carry_findings),
    _row("resume-stacking", TasksPlanned, resume_stacking),
    _row("discard-proposal", ReplanRequested, discard_proposal),
    _row("cut-stack-top", ReplanRequested, cut_stack_top),
    _row("plan-again", ReplanRequested, plan_again, _SUPERVISOR),
    _row("pause-stacking", ReplanRequested, pause_stacking),
    _row("stop-tasks", TasksStopped, stop_tasks),
    _row("discard-job", TasksDiscarded, discard_job),
    _row("requeue", TasksReturnedToQueue, requeue),
    _row("start-ready-after-stacked", TaskMarkedStacked, start_ready),
    _row("start-ready-after-inserted", TaskInserted, start_ready),
    _row("start-ready-after-stopped", TasksStopped, start_ready),
    _row("start-ready-after-slot-freed", TaskStatusChanged, start_ready_after_slot_freed),
    _row("close-task-side", EscalationClosed, close_task_side),
    _row("resolve-task-side", EscalationAnswered, resolve_task_side),
    _row("note-answer", EscalationAnswered, note_answer),
    _row("ask-user-for-supervisor", EscalationRaised, ask_user_for_supervisor),
    _row("withdraw-questions", EscalationClosed, withdraw_questions),
    _row("finish-job", RunFinished, finish_job),
    _row("resume-tasks", RunResumed, resume_tasks),
    _row("start-ready-after-resumed", RunResumed, start_ready),
    # Task
    _row("hand-findings", StageCompleted, hand_findings),
    _row("hand-judgement", StageCompleted, hand_judgement),
    _row("hand-proposal", StageCompleted, hand_proposal),
    _row("hand-overview", StageCompleted, hand_overview),
    _row("hand-entry", StageCompleted, hand_entry),
    _row("hand-cut-back", StageCompleted, hand_cut_back),
    _row("record-conflict", StageCompleted, record_conflict),
    _row("count-fix", StageCompleted, count_fix),
    _row("comment-findings", StageCompleted, comment_findings),
    _row("gate-passed", StageCompleted, gate_passed),
    _row("gate-failed", GateFailed, gate_failed),
    _row("reject-integration", StageReported, reject_integration),
    _row("relay-to-run", EscalationRaised, relay_to_run, _SUPERVISOR),
    _row("close-run-side", EscalationClosed, close_run_side),
    _row("resume-design", EscalationResolved, resume_design),
    _row("finish-git-job", (FlowFinished, FlowAbandoned), finish_git_job),
    _row("git-finished", FlowFinished, git_finished),
    _row("enqueue-gated", TaskGated, enqueue_gated),
    # 計画タスクのフローを組む前に、読む所を覚えさせる（最初のステージのプロンプトが読む）
    _row("record-cut-base", WorktreeReady, record_cut_base),
    _row("record-rebased-base", BranchRebased, record_rebased_base),
    _row("plan-on-worktree", WorktreeReady, plan_on_worktree, _SUPERVISOR),
    _row("status-gated", TaskGated, status(TaskStatus.GATED)),
    _row("status-stacking", GitJobTaken, status(TaskStatus.STACKING)),
    _row("status-escalated", EscalationRaised, status(TaskStatus.ESCALATED)),
    _row(
        "status-running",
        (EscalationResolved, EscalationClosed, ScopeChanged, FlowAccepted),
        status(TaskStatus.RUNNING),
    ),
    # ReviewLedger
    _row("conclude-round", FindingsEvaluated, conclude_round),
    _row("confirm-received", ResultReceived, confirm_received),
    _row("confirm-refused", ResultRefused, confirm_refused),
    _row("raise-carried", FindingCarried, raise_carried),
    # Design
    _row("track-proposal", DesignProposed, track_proposal),
    _row("record-settled", DesignSettled, record_settled),
    _row("conclude-settled", DesignSettled, conclude_settled),
    _row("reject-appendix", DesignSettled, reject_appendix),
    _row("conclude-revision", DesignRevisionStarted, conclude_revision),
    _row("escalate-reverted", DesignReverted, escalate_design_wait(_E.DESIGN_REVERTED)),
    _row("escalate-ambiguous", DesignAmbiguous, escalate_design_wait(_E.DESIGN_AMBIGUOUS)),
    _row(
        "escalate-rounds-exhausted",
        DesignRoundsExhausted,
        escalate_design_wait(_E.DESIGN_ROUNDS_EXHAUSTED),
    ),
    # Stack
    _row(
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
    ),
    _row("flow-for-job", GitJobTaken, flow_for_job, _SUPERVISOR),
    _row("abandon-withdrawn", GitJobWithdrawn, abandon_withdrawn),
    _row("mark-stacked", TaskStacked, mark_stacked),
    _row("return-to-queue", StackCutBack, return_to_queue),
    _row("record-integration-failure", IntegrationFailed, record_integration_failure),
    _row("clear-integration-failure", IntegrationRetried, clear_integration_failure),
    _row("regate-kept-stack-job", GitJobFinished, regate_kept_stack_job),
    _row("escalate-stuck-job", GitJobFinished, escalate_stuck_job),
    _row("retry-stuck-job", EscalationResolved, retry_stuck_job),
    _row("drop-stuck-job", EscalationClosed, drop_stuck_job),
    _row("retry-integration", EscalationResolved, retry_integration),
    # Questions
    _row("record-answer", QuestionAnswered, record_answer),
)

#: 反応が副作用を済ませた後に返すコマンドの組み方。反応はこれを呼び、返った一覧をそのまま返す
FOLLOW_UPS: Mapping[str, Policy] = MappingProxyType(
    {
        policy.name: policy
        for policy in (
            # answers/<tool_use_id>.json に EscalationResolved.answer を書いてから
            _row("resume-ask", EscalationResolved, resume_ask, By.REACTION),
        )
    }
)

#: メインループに登録する受け手（名前 → 受ける関数）。登録の順は、この並び
RECEIVERS: Mapping[str, Callable[[Event, EventId], list[Command]]] = MappingProxyType(
    {policy.name: policy.receive for policy in POLICIES}
)
