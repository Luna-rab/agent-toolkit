"""LLM の統括（ラン統括と実装タスクの統括）を、どのイベントで起こすか。

統括を起こすのは反応（アプリケーション層）だが、**どの出来事で誰を起こすかはここで決める。** 反応は
`wake_for` に聞き、返った統括を、返った知らせで起こすだけにする。うまく進んでいる間は統括を起こさない
（統括の文脈に何も積もらない）ので、ここに無いイベントでは誰も起きない。

計画タスクと git 管理タスクの統括はプログラムで、ポリシーの表（`policies/registry.py`）に入っている。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .events.base import Event
from .events.run import (
    AllTasksSettled,
    AnswerRecorded,
    EscalationAnswered,
    EscalationRaised,
    SettledPlanRecorded,
    TasksPlanned,
)
from .events.task import FlowAbandoned, FlowRejected, ScopeChanged, WorktreeReady
from .services.escalation_router import EscalationRouter, SupervisorLevel, task_of_stream
from .value_objects.event_id import EventId
from .value_objects.git_job_kind import GitJobKind
from .value_objects.guard import Guard
from .value_objects.model_class import ModelClass
from .value_objects.stream_id import StreamId
from .value_objects.task_id import TaskId
from .value_objects.task_kind import TaskKind
from .value_objects.write_scope import WriteScope

#: 統括の書き込みの範囲。統括は読むだけで、決めたことは判断の JSON で返す
SUPERVISOR_GUARD = Guard(WriteScope.NONE)


class Notice(Enum):
    """起こした理由（指示書の「起こされるとき」の行）。"""

    #: ラン統括・タスク統括: エスカレーションが上がった
    ESCALATION = "escalation"
    #: ラン統括: ユーザーの回答が届いた
    ANSWER = "answer"
    #: ラン統括: 再計画の設計が確定した（apply-plan を返す）
    SETTLED_REPLAN = "settled-replan"
    #: ラン統括: 再計画を反映した後も、開いているエスカレーションがある
    STILL_OPEN = "still-open"
    #: ラン統括: 実装タスクがすべて終端になった（finish を返す）
    ALL_SETTLED = "all-settled"
    #: タスク統括: タスクの worktree を切った（最初のフローを組む）
    STARTED = "started"
    #: タスク統括: 再計画で範囲が変わり、今のフローは捨てられた
    SCOPE_CHANGED = "scope-changed"
    #: タスク統括: 返したフローを FlowValidator が拒んだ
    FLOW_REJECTED = "flow-rejected"
    #: タスク統括: 上げたエスカレーションが回答以外で閉じられ、今のフローは捨てられた
    FLOW_ABANDONED = "flow-abandoned"
    #: どちらも: 返した判断が拒まれた（判断を置き換えたコマンドを集約が拒んだ・形が合わない）
    DECISION_REJECTED = "decision-rejected"
    #: どちらも: 前に応じられなかった知らせ（supervisor-failed）に 1 段上が答えたので、新しい
    #: セッションで同じ知らせから起こし直す。元の知らせと回答を載せる
    RETRY = "retry"


@dataclass(frozen=True)
class Supervisor:
    """LLM の統括 1 つ。`task` が None ならラン統括、あれば実装タスクの統括。"""

    task: TaskId | None = None

    def __post_init__(self) -> None:
        if self.task is not None and self.task.kind is not TaskKind.IMPLEMENTATION:
            raise ValueError(f"LLM の統括を持つのは実装タスクだけ: {self.task}")

    @classmethod
    def run(cls) -> Supervisor:
        return cls()

    @classmethod
    def of(cls, task: TaskId) -> Supervisor:
        return cls(task)

    @property
    def name(self) -> str:
        """セッションとログの置き場の名前。"""
        return "run" if self.task is None else f"task-{self.task}"

    @property
    def model_class(self) -> ModelClass:
        """claude に渡すモデルと effort のクラス。ラン統括もタスク統括も lead。"""
        return ModelClass.LEAD


@dataclass(frozen=True)
class Wake:
    supervisor: Supervisor
    notice: Notice
    #: RETRY のとき、起こし直す元の知らせ（統括が応じられなかった知らせのイベント）
    replay: EventId | None = None


def wake_for(event: Event, source: EventId) -> Wake | None:  # noqa: PLR0911  起こす場合ごとの分岐
    """このイベントで起こす統括と、その理由。起こさないなら None。"""
    stream = source.stream
    if stream == StreamId.run():
        if (
            isinstance(event, EscalationAnswered)
            and event.failed_notice is not None
            and event.task is not None
            and event.task.kind is TaskKind.IMPLEMENTATION
        ):
            # 応じなかったタスク統括の上げにラン統括が答えた。そのタスク統括を起こし直す
            return Wake(Supervisor.of(event.task), Notice.RETRY, event.failed_notice)
        return _wake_run(event)
    if isinstance(event, WorktreeReady):
        # 実装タスクを始めたとき（積む列から外して始め直したときも）、切った worktree でフローを組む。
        # 積み直すための切り直しは git 管理タスクのフローの中なので、起こさない
        if event.job is None or event.job.kind is not GitJobKind.CUT_TASK:
            return None
        if event.task.kind is not TaskKind.IMPLEMENTATION:
            return None
        return Wake(Supervisor.of(event.task), Notice.STARTED)
    if not stream.value.startswith("task/"):
        return None
    task = task_of_stream(stream)
    if task.kind is not TaskKind.IMPLEMENTATION:
        return None
    if isinstance(event, EscalationRaised):
        return Wake(Supervisor.of(task), Notice.ESCALATION)
    if isinstance(event, ScopeChanged):
        # 新しいブランチで始め直すなら、切り直した worktree を待つ（WorktreeReady で起こす）
        return None if event.branch is not None else Wake(Supervisor.of(task), Notice.SCOPE_CHANGED)
    if isinstance(event, FlowRejected):
        return Wake(Supervisor.of(task), Notice.FLOW_REJECTED)
    if isinstance(event, FlowAbandoned):
        return Wake(Supervisor.of(task), Notice.FLOW_ABANDONED)
    return None


def _wake_run(event: Event) -> Wake | None:
    """ラン統括を起こすのは、上がってきたイベント・回答の到着・再計画の確定・全タスクの終わり。

    ランの開始では起こさない（返す判断が無い）。初回の計画の確定は、ポリシーが ApplyPlan で反映する。
    """
    run = Supervisor.run()
    if isinstance(event, EscalationRaised):
        # ユーザーが受ける上げ（ラン統括が応じなかった）は、ポリシーが質問にする
        route = EscalationRouter.route(StreamId.run(), event.kind, event.task)
        return Wake(run, Notice.ESCALATION) if route.level is SupervisorLevel.RUN else None
    # 答える先のエスカレーションがもう閉じていた回答では起こさない（ラン統括が返せる判断が無い）
    if isinstance(event, AnswerRecorded) and not event.escalation_closed:
        # ラン統括が応じなかったので聞いた質問への回答なら、その知らせから起こし直す
        notice = Notice.ANSWER if event.failed_notice is None else Notice.RETRY
        return Wake(run, notice, event.failed_notice)
    if isinstance(event, SettledPlanRecorded) and event.replan:
        return Wake(run, Notice.SETTLED_REPLAN)
    if isinstance(event, TasksPlanned) and event.still_open:
        return Wake(run, Notice.STILL_OPEN)
    if isinstance(event, AllTasksSettled):
        return Wake(run, Notice.ALL_SETTLED)
    return None
