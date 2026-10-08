"""Stack のイベントを受けるポリシー。"""

from __future__ import annotations

from ..commands.base import Command
from ..commands.run import (
    ClearIntegrationFailure,
    MarkStacked,
    RecordIntegrationFailure,
    ReturnToQueue,
    UpdateTaskStatus,
)
from ..commands.stack import DropGitJob, RetryGitJob, RetryIntegration, TakeNextGitJob
from ..commands.task import AbandonFlow, AcceptFlow, Escalate
from ..events.base import Event
from ..events.stack import (
    GitJobFinished,
    GitJobTaken,
    GitJobWithdrawn,
    IntegrationFailed,
    IntegrationRetried,
    StackCutBack,
    TaskStacked,
)
from ..events.task import EscalationClosed, EscalationResolved
from ..flow.standard import git_job_flow
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.event_id import EventId
from ..value_objects.execution_id import ExecutionId
from ..value_objects.git_job_kind import GitJobKind
from ..value_objects.git_job_outcome import GitJobOutcome
from ..value_objects.limits import MAX_JOB_RETURNS
from ..value_objects.pointers import Pointers
from ..value_objects.task_id import TaskId
from ..value_objects.task_status import TaskStatus
from .base import Stamp, is_from_git

_J = GitJobKind
_E = EscalationKind


_GIT = TaskId.git()


def take_next(e: Event, src: EventId, stamp: Stamp) -> list[Command]:
    """取り出せるかは Stack が決める。取り出せなければ何もしない。"""
    return [TakeNextGitJob(**stamp())]


def flow_for_job(e: GitJobTaken, src: EventId, stamp: Stamp) -> list[Command]:
    """git 管理タスクの統括: 取り出した仕事から決まった並びを組む。"""
    return [AcceptFlow(**stamp(_GIT), task=_GIT, steps=git_job_flow(e.job), job=e.job)]


def abandon_withdrawn(e: GitJobWithdrawn, src: EventId, stamp: Stamp) -> list[Command]:
    if not e.in_progress:
        return []
    return [AbandonFlow(**stamp(), task=_GIT, reason="相手のタスクを止めた")]


def mark_stacked(e: TaskStacked, src: EventId, stamp: Stamp) -> list[Command]:
    return [MarkStacked(**stamp(), task=e.entry.task, pr=e.entry.pr)]


def return_to_queue(e: StackCutBack, src: EventId, stamp: Stamp) -> list[Command]:
    """積む列へ戻す一覧は Run が持つ（TasksDiscarded.requeue）ので、写さずに頼むだけ。"""
    return [ReturnToQueue(**stamp())]


def record_integration_failure(e: IntegrationFailed, src: EventId, stamp: Stamp) -> list[Command]:
    return [RecordIntegrationFailure(**stamp(), task=e.task, reason=e.reason, files=e.files)]


def clear_integration_failure(e: IntegrationRetried, src: EventId, stamp: Stamp) -> list[Command]:
    return [ClearIntegrationFailure(**stamp(), task=e.task)]


def regate_kept_stack_job(e: GitJobFinished, src: EventId, stamp: Stamp) -> list[Command]:
    """積む仕事のフローを捨てて仕事を残したら、相手のタスクを gated に戻す（積む列で待っている）。

    再計画で範囲が変わったら、gated のタスクとして積む列から外せる（TasksPlanned.withdraw）。
    """
    job = e.job
    if not e.outcome.keeps_job or job.kind is not _J.STACK or job.task is None:
        return []
    return [
        UpdateTaskStatus(
            **stamp(), task=job.task, to_status=TaskStatus.GATED, cause="GitJobFinished"
        )
    ]


def escalate_stuck_job(e: GitJobFinished, src: EventId, stamp: Stamp) -> list[Command]:
    """戻した回数が上限に達して止めた仕事を、git 管理タスクから上げる（ラン統括が続けるかやめるか）。

    やめるとランが終わらない仕事（`GitJobKind.can_drop` が偽）は、答え以外では閉じられない上げにする
    （`answer_only`）。ラン統括が答え以外の判断で閉じようとすると Run が拒み、理由を添えて差し戻す。
    """
    if e.outcome is not GitJobOutcome.STUCK:
        return []
    stuck = (
        f"git の仕事 {e.job.id}（{e.job.kind.value}）のフローを捨てて {MAX_JOB_RETURNS} 回戻したが"
        "、済んでいない。"
    )
    droppable = e.job.kind.can_drop
    if droppable:
        reason = stuck + "続けるなら答え、やめるなら答え以外の判断で閉じる"
    else:
        reason = (
            stuck
            + "この仕事はやめるとランが終わらないので、答えて続ける（答え以外では閉じられない）"
        )
    return [
        Escalate(
            **stamp(),
            task=_GIT,
            kind=_E.STAGE_ERRORS,
            pointers=Pointers(),
            reason=reason,
            answer_only=not droppable,
        )
    ]


def _stuck_job_escalation(kind: EscalationKind | None, origin: ExecutionId | None) -> bool:
    """止めた仕事への上げか（名前の付いた規則）。git 管理タスクの、上げ元の実行が無い stage-errors。"""
    return kind is _E.STAGE_ERRORS and origin is None


def retry_stuck_job(e: EscalationResolved, src: EventId, stamp: Stamp) -> list[Command]:
    """止めた仕事への上げに回答が届いた。続ける。"""
    if not is_from_git(src) or not _stuck_job_escalation(e.kind, e.origin):
        return []
    return [RetryGitJob(**stamp())]


def drop_stuck_job(e: EscalationClosed, src: EventId, stamp: Stamp) -> list[Command]:
    """止めた仕事への上げを答え以外で閉じた。やめる。"""
    if not is_from_git(src) or not _stuck_job_escalation(e.kind, e.origin):
        return []
    return [DropGitJob(**stamp())]


def retry_integration(e: EscalationResolved, src: EventId, stamp: Stamp) -> list[Command]:
    """統合の失敗に回答が届いた。同じ仕事で統合をやり直すので、失敗の印を下ろす。"""
    if not is_from_git(src) or e.kind is not _E.INTEGRATION_FAILED:
        return []
    return [RetryIntegration(**stamp())]
