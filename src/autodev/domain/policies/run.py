"""Run のイベントを受けるポリシー。"""

from __future__ import annotations

from ..commands.base import Command
from ..commands.design import DiscardProposal
from ..commands.questions import PostQuestion, WithdrawQuestions
from ..commands.review_ledger import CarryFinding
from ..commands.run import ApplyPlan, StartReadyTasks, StartTask
from ..commands.stack import (
    EnqueueGitJob,
    EnqueueStack,
    PauseStacking,
    ResumeStacking,
    WithdrawRequest,
)
from ..commands.task import (
    AcceptFlow,
    AddNote,
    ChangeScope,
    CloseEscalation,
    OpenTask,
    ResolveEscalation,
    ResumeInterrupted,
    StopTask,
)
from ..events.base import Event
from ..events.run import (
    EscalationAnswered,
    EscalationRaised,
    ReplanRequested,
    RunFinished,
    RunResumed,
    RunStarted,
    SettledPlanRecorded,
    TasksDiscarded,
    TasksPlanned,
    TasksReturnedToQueue,
    TasksStopped,
    TaskStarted,
    TaskStatusChanged,
)
from ..events.task import EscalationClosed
from ..flow.standard import planning_flow
from ..services.escalation_router import EscalationRouter, SupervisorLevel
from ..services.task_scheduler import TaskScheduler
from ..value_objects.branch_name import BranchName
from ..value_objects.decision import Decision
from ..value_objects.decision_origin import DecisionOrigin
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.event_id import EventId
from ..value_objects.git_job_kind import GitJobKind
from ..value_objects.pointers import Pointers
from ..value_objects.question_id import QuestionId
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId
from ..value_objects.task_kind import TaskKind
from .base import Stamp

_J = GitJobKind
_E = EscalationKind
_PLANNING = TaskId.planning()
_GIT = TaskId.git()


def start_planning_and_git(e: RunStarted, src: EventId, stamp: Stamp) -> list[Command]:
    """計画タスクと git 管理タスクを始め、最初の仕事（概要ブランチを切る）を列に入れる。"""
    return [
        StartTask(**stamp(), task=_PLANNING),
        StartTask(**stamp(), task=_GIT),
        EnqueueGitJob(
            **stamp(),
            kind=_J.CUT_OVERVIEW,
            task=_PLANNING,
            branch=BranchName.overview(e.name),
            base=e.base,
        ),
    ]


def open_task(e: TaskStarted, src: EventId, stamp: Stamp) -> list[Command]:
    if e.reopened:
        return []
    return [
        OpenTask(
            **stamp(),
            task=e.task,
            kind=e.kind,
            spec=e.spec,
            artifacts=e.artifacts,
            blocked_by=e.blocked_by,
            branch=e.branch,
            conflicts=e.conflicts,
        )
    ]


def rescope_reopened(e: TaskStarted, src: EventId, stamp: Stamp) -> list[Command]:
    """積む列から外して始め直したタスクには、新しいブランチで範囲を変えたと知らせる。"""
    if not e.reopened:
        return []
    assert e.spec is not None, "実装タスクの TaskStarted は中身を持つ"
    return [
        ChangeScope(
            **stamp(),
            task=e.task,
            spec=e.spec,
            artifacts=e.artifacts,
            pointers=Pointers(),
            branch=e.branch,
        )
    ]


def cut_task_branch(e: TaskStarted, src: EventId, stamp: Stamp) -> list[Command]:
    if e.kind is not TaskKind.IMPLEMENTATION:
        return []
    return [EnqueueGitJob(**stamp(), kind=_J.CUT_TASK, task=e.task, branch=e.branch)]


def apply_first_plan(e: SettledPlanRecorded, src: EventId, stamp: Stamp) -> list[Command]:
    # 再計画（replan）なら、反応がラン統括を起こして apply-plan を返させる
    return [] if e.replan else [ApplyPlan(**stamp(), design=e.proposal.design)]


def start_ready(e: Event, src: EventId, stamp: Stamp) -> list[Command]:
    """始められるタスクが増えうるイベントで頼む。どれを始めるかは、Run が TaskScheduler に聞いて
    決める。増えうるのは、計画を反映した・依存先を積んだ・差し込んだ・止めた（待つ
    相手が減り、枠が空く）・呼び直された・並列の枠が空いた、のとき。"""
    return [StartReadyTasks(**stamp())]


def start_ready_after_slot_freed(e: TaskStatusChanged, src: EventId, stamp: Stamp) -> list[Command]:
    """並列の枠が空いた（running → gated など）。

    TaskGated ではなく、Run が状態を動かした TaskStatusChanged で頼む。TaskGated を受けた行どうしの
    処理の順によらず、Run が gated を当てた後に頼むためである。
    """
    if not TaskScheduler.frees_slot(e.from_status, e.to_status):
        return []
    return [StartReadyTasks(**stamp())]


def write_overview(e: TasksPlanned, src: EventId, stamp: Stamp) -> list[Command]:
    return [EnqueueGitJob(**stamp(), kind=_J.REWRITE_OVERVIEW if e.replan else _J.OPEN_OVERVIEW)]


def change_scope(e: TasksPlanned, src: EventId, stamp: Stamp) -> list[Command]:
    specs = {task.id: task.spec for task in e.tasks}
    return [
        ChangeScope(
            **stamp(), task=task, spec=specs[task], artifacts=e.artifacts, pointers=Pointers()
        )
        for task in sorted(e.scope_changed, key=str)
    ]


def withdraw_rescoped(e: TasksPlanned, src: EventId, stamp: Stamp) -> list[Command]:
    return [WithdrawRequest(**stamp(), task=task) for task in sorted(e.withdraw, key=str)]


def carry_findings(e: TasksPlanned, src: EventId, stamp: Stamp) -> list[Command]:
    return [
        CarryFinding(
            **stamp(), ledger=StreamId.review(c.from_task), finding=c.finding, to_task=c.to_task
        )
        for c in e.carry
    ]


def resume_stacking(e: TasksPlanned, src: EventId, stamp: Stamp) -> list[Command]:
    """再計画を反映したら、積む仕事をまた取り出す。取り出しは StackingResumed で頼む（take-next）。"""
    return [ResumeStacking(**stamp())]


def discard_proposal(e: ReplanRequested, src: EventId, stamp: Stamp) -> list[Command]:
    return [DiscardProposal(**stamp(), reason=e.reason)]


def cut_stack_top(e: ReplanRequested, src: EventId, stamp: Stamp) -> list[Command]:
    return [EnqueueGitJob(**stamp(), kind=_J.CUT_STACK_TOP)] if e.settled_before else []


def plan_again(e: ReplanRequested, src: EventId, stamp: Stamp) -> list[Command]:
    """計画タスクの統括: 設計が一度も確定していなければ、trees/overview のまま Prepare → Plan から組み直す。"""
    if e.settled_before:
        return []  # stack-top を切り直した WorktreeReady で組む（plan-on-worktree）
    return [
        AcceptFlow(**stamp(_PLANNING), task=_PLANNING, steps=planning_flow(settled_before=False))
    ]


def pause_stacking(e: ReplanRequested, src: EventId, stamp: Stamp) -> list[Command]:
    """再計画が進んでいる間は、積む仕事を取り出さない（Stack の規則）。"""
    return [PauseStacking(**stamp())]


def stop_tasks(e: TasksStopped, src: EventId, stamp: Stamp) -> list[Command]:
    commands: list[Command] = []
    for task in sorted(e.tasks, key=str):
        commands += [
            StopTask(**stamp(), task=task, reason="タスクを止めた"),
            WithdrawRequest(**stamp(), task=task),
        ]
    return commands


def discard_job(e: TasksDiscarded, src: EventId, stamp: Stamp) -> list[Command]:
    return [EnqueueGitJob(**stamp(), kind=_J.DISCARD, discarded=e.tasks)]


def requeue(e: TasksReturnedToQueue, src: EventId, stamp: Stamp) -> list[Command]:
    return [
        EnqueueStack(**stamp(), task=task, branch=branch)
        for task, branch in zip(e.tasks, e.branches, strict=True)
    ]


def close_task_side(e: EscalationClosed, src: EventId, stamp: Stamp) -> list[Command]:
    """Run の側で閉じたエスカレーションの、タスクの側の元を閉じる。"""
    if src.stream != StreamId.run() or e.task is None or e.source is None:
        return []
    return [CloseEscalation(**stamp(), task=e.task, escalation=e.source, reason=e.reason)]


def resolve_task_side(e: EscalationAnswered, src: EventId, stamp: Stamp) -> list[Command]:
    if e.task is None or e.source is None:
        return []
    return [
        ResolveEscalation(
            **stamp(), task=e.task, escalation=e.source, answer=e.answer, question=e.question
        )
    ]


def note_answer(e: EscalationAnswered, src: EventId, stamp: Stamp) -> list[Command]:
    """上げ元を書かずに上げたエスカレーションへの回答は、タスクの notes に残す。

    統括が応じなかった上げ（failed_notice がある）への回答は、起こし直すタスク統括への言葉で、
    受入条件の判断ではないので残さない（起こし直す知らせに載る）。
    """
    if e.task is None or e.source is not None or e.failed_notice is not None:
        return []
    origin = DecisionOrigin.USER if e.question is not None else DecisionOrigin.RUN_SUPERVISOR
    return [AddNote(**stamp(), task=e.task, decision=Decision(e.answer, origin, e.question))]


def ask_user_for_supervisor(e: EscalationRaised, src: EventId, stamp: Stamp) -> list[Command]:
    """ユーザーが直に受ける Run の上げ（ラン統括が応じなかった）を、`/autodev` への質問にする。

    回答が届くと Run がこの上げを閉じ、ラン統括を新しいセッションで、同じ知らせで起こし直す。
    タスク統括が応じなかった上げは、何度続いてもラン統括が受ける（段を飛ばさない）。
    """
    if src.stream != StreamId.run() or e.kind is not _E.SUPERVISOR_FAILED:
        return []
    route = EscalationRouter.route(src.stream, e.kind, e.task)
    if route.level is not SupervisorLevel.USER:
        return []
    body = (
        f"ラン統括が知らせ {e.failed_notice} に応じなかった（{e.reason}）。原因を取り除いてから"
        "答えると、回答を添えて、ラン統括を新しいセッションで同じ知らせから起こし直す。"
    )
    question = QuestionId(f"supervisor-failed-{src.version}")
    return [PostQuestion(**stamp(), question=question, body=body, escalation=src)]


def withdraw_questions(e: EscalationClosed, src: EventId, stamp: Stamp) -> list[Command]:
    """Run の側のエスカレーションが閉じたら、それを経路に持つ回答待ちの質問を取り下げる。

    ラン統括が ask-user の後に stop-tasks・replan で閉じると、質問が回答を待ったまま残り、driver が
    回答待ち（終了コード 4）で止まる。回答で閉じたなら質問はもう answered で、Questions は何もしない。
    """
    if src.stream != StreamId.run():
        return []
    return [WithdrawQuestions(**stamp(), escalation=e.escalation, reason=e.reason)]


def finish_job(e: RunFinished, src: EventId, stamp: Stamp) -> list[Command]:
    return [EnqueueGitJob(**stamp(), kind=_J.FINISH, ready_overview=e.ready_overview)]


def resume_tasks(e: RunResumed, src: EventId, stamp: Stamp) -> list[Command]:
    return [ResumeInterrupted(**stamp(), task=task) for task in e.tasks]
