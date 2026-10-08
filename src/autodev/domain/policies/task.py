"""Task のイベントを受けるポリシー。"""

from __future__ import annotations

from ..aggregates.design import DESIGN_WAITS
from ..commands.base import Command
from ..commands.design import ProposeDesign, ResumeDesign
from ..commands.review_ledger import (
    CommentFinding,
    CountFix,
    RecordFindings,
    RecordGateResult,
    RecordJudgement,
)
from ..commands.run import CloseRelayedEscalation, EscalateToRun, UpdateTaskStatus
from ..commands.stack import (
    AppendEntry,
    EnqueueStack,
    FinishGitJob,
    RecordConflict,
    RecordOverview,
    RejectRequest,
    UnstackFrom,
)
from ..commands.task import AcceptFlow, RecordBase, ResumeStage
from ..events.base import Event
from ..events.run import EscalationRaised
from ..events.stack import GitJobTaken
from ..events.task import (
    BranchRebased,
    EscalationClosed,
    EscalationResolved,
    FlowAbandoned,
    FlowAccepted,
    FlowFinished,
    GateFailed,
    StageCompleted,
    StageReported,
    TaskGated,
    WorktreeReady,
)
from ..flow.standard import planning_flow
from ..stages.catalog import STAGE_SPECS
from ..stages.kinds import Handoff
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.event_id import EventId
from ..value_objects.flow_ending import FlowEnding
from ..value_objects.git_job_kind import GitJobKind
from ..value_objects.stack_entry import StackEntry
from ..value_objects.stage_kind import StageKind
from ..value_objects.task_id import TaskId
from ..value_objects.task_kind import TaskKind
from ..value_objects.task_status import TaskStatus
from .base import Rule, Stamp, is_from_task, review_stream_of, source_task

_S = StageKind
_J = GitJobKind
_E = EscalationKind
_PLANNING = TaskId.planning()
_GIT = TaskId.git()


def hand_findings(e: StageCompleted, src: EventId, stamp: Stamp) -> list[Command]:
    if e.handoff is not Handoff.FINDINGS:
        return []
    assert e.result is not None, "受け渡すステージの StageCompleted は結果を持つ"
    return [
        RecordFindings(
            **stamp(),
            ledger=review_stream_of(e.execution.task),
            source=e.execution,
            findings=e.result.findings,
            design=e.reviewed,
        )
    ]


def hand_judgement(e: StageCompleted, src: EventId, stamp: Stamp) -> list[Command]:
    if e.handoff is not Handoff.JUDGEMENT:
        return []
    assert e.result is not None, "受け渡すステージの StageCompleted は結果を持つ"
    return [
        RecordJudgement(
            **stamp(),
            ledger=review_stream_of(e.execution.task),
            execution=e.execution,
            verdicts=e.result.verdicts,
            stall_cause=e.result.stall_cause,
            design=e.reviewed,
            design_cause=e.result.design_cause,
        )
    ]


def hand_proposal(e: StageCompleted, src: EventId, stamp: Stamp) -> list[Command]:
    if e.handoff is not Handoff.PROPOSAL:
        return []
    assert e.result is not None and e.result.proposal is not None, "提案のステージは提案を返す"
    return [
        ProposeDesign(
            **stamp(), proposal=e.result.proposal, execution=e.execution, artifacts=e.shared
        )
    ]


def hand_overview(e: StageCompleted, src: EventId, stamp: Stamp) -> list[Command]:
    if e.handoff is not Handoff.OVERVIEW:
        return []
    job = e.job
    assert e.result is not None and e.result.pr is not None, "概要 PR を作るステージは PR を返す"
    assert job is not None and job.branch is not None and job.base is not None, "概要 PR の仕事"
    entry = StackEntry(_GIT, job.branch, e.result.pr, job.base)
    return [RecordOverview(**stamp(), entry=entry, source=e.execution)]


def hand_entry(e: StageCompleted, src: EventId, stamp: Stamp) -> list[Command]:
    if e.handoff is not Handoff.ENTRY:
        return []
    job = e.job
    assert e.result is not None and e.result.pr is not None, "積むステージは PR を返す"
    assert job is not None and job.task is not None, "積む仕事は相手のタスクを持つ"
    assert job.branch is not None and job.base is not None, "取り出した積む仕事は base を持つ"
    entry = StackEntry(job.task, job.branch, e.result.pr, job.base)
    return [AppendEntry(**stamp(), entry=entry, source=e.execution)]


def hand_cut_back(e: StageCompleted, src: EventId, stamp: Stamp) -> list[Command]:
    if e.handoff is not Handoff.CUT_BACK:
        return []
    assert e.job is not None, "閉じるステージは git 管理タスクの仕事の中で走る"
    return [
        UnstackFrom(**stamp(), entry=e.job.cut_from, discarded=e.job.discarded, source=e.execution)
    ]


def record_conflict(e: StageCompleted, src: EventId, stamp: Stamp) -> list[Command]:
    if not e.conflicts or e.job is None or e.job.task is None:
        return []
    return [RecordConflict(**stamp(), task=e.job.task, files=e.conflicts)]


def count_fix(e: StageCompleted, src: EventId, stamp: Stamp) -> list[Command]:
    if STAGE_SPECS[e.execution.stage].fixes is None:
        return []
    return [CountFix(**stamp(), ledger=review_stream_of(e.execution.task), execution=e.execution)]


def comment_findings(e: StageCompleted, src: EventId, stamp: Stamp) -> list[Command]:
    if e.result is None:
        return []
    return [
        CommentFinding(
            **stamp(),
            ledger=review_stream_of(e.execution.task),
            finding=c.finding,
            body=c.body,
            author=e.execution,
        )
        for c in e.result.comments
    ]


def gate_passed(e: StageCompleted, src: EventId, stamp: Stamp) -> list[Command]:
    if e.execution.stage is not _S.GATE:
        return []
    return [
        RecordGateResult(
            **stamp(), ledger=review_stream_of(e.execution.task), execution=e.execution
        )
    ]


def gate_failed(e: GateFailed, src: EventId, stamp: Stamp) -> list[Command]:
    return [
        RecordGateResult(
            **stamp(),
            ledger=review_stream_of(e.execution.task),
            execution=e.execution,
            failed=e.failed,
        )
    ]


def reject_integration(e: StageReported, src: EventId, stamp: Stamp) -> list[Command]:
    if e.kind is not _E.INTEGRATION_FAILED or e.job is None or e.job.task is None:
        return []
    return [RejectRequest(**stamp(), task=e.job.task, reason=e.reason or e.kind.value)]


def relay_to_run(e: EscalationRaised, src: EventId, stamp: Stamp) -> list[Command]:
    """計画タスクと git 管理タスクの統括: 受けたエスカレーションを、そのままラン統括へ上げる。"""
    if not is_from_task(src) or source_task(src).kind is TaskKind.IMPLEMENTATION:
        return []
    task = source_task(src)
    return [
        EscalateToRun(
            **stamp(task),
            task=task,
            kind=e.kind,
            reason=e.reason or e.kind.value,
            pointers=e.pointers,
            hint=e.hint,
            source=src,
            question=e.question,
            answer_only=e.answer_only,
        )
    ]


def close_run_side(e: EscalationClosed, src: EventId, stamp: Stamp) -> list[Command]:
    """タスクの側で閉じたエスカレーションを中継していた、Run の側のエスカレーションも閉じる。"""
    if not is_from_task(src):
        return []
    return [CloseRelayedEscalation(**stamp(), source=e.escalation, reason=e.reason)]


def resume_design(e: EscalationResolved, src: EventId, stamp: Stamp) -> list[Command]:
    if e.kind not in DESIGN_WAITS:
        return []
    return [ResumeDesign(**stamp(), kind=e.kind, answer=e.answer)]


def resume_ask(e: EscalationResolved, src: EventId, stamp: Stamp) -> list[Command]:
    """反応の続き: answers/<tool_use_id>.json に回答を書き終えてから、止まった実行を続きから再開する。"""
    if e.resume is None or e.tool_use_id is None:
        return []
    return [ResumeStage(**stamp(), task=source_task(src), execution=e.resume)]


def finish_git_job(e: FlowFinished | FlowAbandoned, src: EventId, stamp: Stamp) -> list[Command]:
    if e.job is None:
        return []
    ending = FlowEnding.FINISHED if isinstance(e, FlowFinished) else FlowEnding.ABANDONED
    return [FinishGitJob(**stamp(), job=e.job.id, ending=ending)]


def git_finished(e: FlowFinished, src: EventId, stamp: Stamp) -> list[Command]:
    """仕上げの並びを終えた git 管理タスクを finished にする。"""
    if e.job is None or e.job.kind is not _J.FINISH:
        return []
    return [
        UpdateTaskStatus(**stamp(), task=_GIT, to_status=TaskStatus.FINISHED, cause="FlowFinished")
    ]


def enqueue_gated(e: TaskGated, src: EventId, stamp: Stamp) -> list[Command]:
    assert e.branch is not None, "実装タスクの TaskGated はブランチを持つ"
    return [EnqueueStack(**stamp(), task=source_task(src), branch=e.branch)]


def record_cut_base(e: WorktreeReady, src: EventId, stamp: Stamp) -> list[Command]:
    """切った worktree と元のコミットを、それを使うタスクに覚えさせる（差分の起点・読む所）。

    積み直すタスクを前に積んだブランチから切り直したなら、切った元は根元ではないので覚えさせない
    （`CutPoint.roots_branch`）。worktree も同じタスクのもので変わらない。
    """
    point = e.job.cut_point if e.job is not None else None
    if e.base is None or (point is not None and not point.roots_branch):
        return []
    return [RecordBase(**stamp(), task=e.task, base=e.base, tree=e.tree)]


def record_rebased_base(e: BranchRebased, src: EventId, stamp: Stamp) -> list[Command]:
    """積み直しで根元が動いたら、載せ直した先をそのタスクに覚えさせる。"""
    return [RecordBase(**stamp(), task=e.task, base=e.onto)]


def plan_on_worktree(e: WorktreeReady, src: EventId, stamp: Stamp) -> list[Command]:
    """計画タスクの統括: 切った仕事の種類で、初回か再計画かの並びを組む。"""
    if e.task != _PLANNING or e.job is None:
        return []
    settled_before = e.job.kind is _J.CUT_STACK_TOP
    return [
        AcceptFlow(
            **stamp(_PLANNING),
            task=_PLANNING,
            steps=planning_flow(settled_before=settled_before),
        )
    ]


def status(to: TaskStatus) -> Rule:
    """Task・Stack のイベントで、Run が持つタスクの状態を動かす。"""

    def rule(e: Event, src: EventId, stamp: Stamp) -> list[Command]:
        if isinstance(e, GitJobTaken):
            if e.job.kind is not _J.STACK or e.job.task is None:
                return []
            task = e.job.task
        elif not is_from_task(src):
            return []
        else:
            task = source_task(src)
        if isinstance(e, FlowAccepted) and e.closes is None:
            return []
        return [UpdateTaskStatus(**stamp(), task=task, to_status=to, cause=type(e).__name__)]

    return rule
