"""ドメインサービス（`domain/services/`）: TaskScheduler・EscalationRouter。"""

from __future__ import annotations

from autodev.domain.services.escalation_router import (
    EscalationRouter,
    Route,
    SupervisorLevel,
    task_of_stream,
)
from autodev.domain.services.task_scheduler import SchedulingEntry, TaskScheduler
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.parallel_limit import ParallelLimit
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_kind import TaskKind
from autodev.domain.value_objects.task_status import TaskStatus

S = TaskStatus
E = EscalationKind
LIMIT = ParallelLimit(2)


def entries(*rows: tuple[str, TaskStatus, set[str]]) -> dict[TaskId, SchedulingEntry]:
    return {
        TaskId(task): SchedulingEntry(TaskId(task), status, frozenset(TaskId(d) for d in deps))
        for task, status, deps in rows
    }


# --- TaskScheduler ---


def test_依存先が積まれたまだ始めていないタスクを番号の順に空きの数だけ選ぶ():
    tasks = entries(
        ("task1", S.STACKED, set()),
        ("task10", S.PENDING, {"task1"}),
        ("task3", S.PENDING, {"task1"}),
        ("task2", S.PENDING, set()),
        ("task4", S.PENDING, {"task9"}),
        ("task9", S.GATED, set()),
    )
    assert TaskScheduler.startable(tasks, LIMIT) == (TaskId("task2"), TaskId("task3"))
    assert TaskScheduler.why_not(TaskId("task10"), tasks, LIMIT) is None


def test_走っているタスクと止まっているタスクを上限に数える():
    tasks = entries(
        ("task1", S.RUNNING, set()),
        ("task2", S.ESCALATED, set()),
        ("task3", S.PENDING, set()),
        # 計画タスクと git 管理タスクは上限に数えない
        ("planning", S.RUNNING, set()),
        ("git", S.RUNNING, set()),
    )
    assert TaskScheduler.occupied(tasks) == 2
    assert TaskScheduler.startable(tasks, LIMIT) == ()
    assert TaskScheduler.why_not(TaskId("task3"), tasks, LIMIT) == (
        "走っている実装タスクが上限（2）に達している"
    )
    assert TaskScheduler.startable(tasks, ParallelLimit(3)) == (TaskId("task3"),)


def test_始められない理由を返す():
    tasks = entries(
        ("task1", S.GATED, set()),
        ("task2", S.PENDING, {"task1", "task7"}),
        ("task3", S.RUNNING, set()),
        ("git", S.RUNNING, set()),
    )
    assert TaskScheduler.why_not(TaskId("task2"), tasks, LIMIT) == (
        "task2 が待つ task1, task7 がまだ積まれていない"
    )
    assert TaskScheduler.why_not(TaskId("task3"), tasks, LIMIT) == (
        "task3 は running で、まだ始めていないタスクではない"
    )
    assert TaskScheduler.why_not(TaskId("task5"), tasks, LIMIT) == "task5 は計画に無い"
    assert TaskScheduler.why_not(TaskId("git"), tasks, LIMIT) == "git は実装タスクではない"


# --- EscalationRouter ---


def test_出所から1段上の統括を決める():
    task = TaskId("task2")
    assert EscalationRouter.route(StreamId.task(task), E.STALL) == Route(SupervisorLevel.TASK, task)
    assert EscalationRouter.route(StreamId.task(TaskId.planning()), E.ASK) == Route(
        SupervisorLevel.TASK, TaskId.planning()
    )
    assert EscalationRouter.route(StreamId.run(), E.NEEDS_REPLAN) == Route(SupervisorLevel.RUN)
    assert EscalationRouter.route(StreamId.run(), E.QUESTION) == Route(SupervisorLevel.USER)


def test_タスクの中で上げてよい種類はタスクの種類で決まる():
    assert EscalationRouter.why_not_raise(TaskKind.IMPLEMENTATION, E.STALL) is None
    assert EscalationRouter.why_not_raise(TaskKind.PLANNING, E.DESIGN_REVERTED) is None
    assert EscalationRouter.why_not_raise(TaskKind.GIT, E.INTEGRATION_FAILED) is None
    assert EscalationRouter.why_not_raise(TaskKind.IMPLEMENTATION, E.ASK) == (
        "implementation のタスクの中では ask を上げない"
    )
    # 統括が上げる種類は、タスクの中では上げない
    assert EscalationRouter.why_not_raise(TaskKind.IMPLEMENTATION, E.NEEDS_REPLAN) is not None


def test_quick_check_failedは実装タスクの中で上げタスク統括が受ける():
    task = TaskId("task2")
    assert EscalationRouter.why_not_raise(TaskKind.IMPLEMENTATION, E.QUICK_CHECK_FAILED) is None
    assert EscalationRouter.why_not_raise(TaskKind.PLANNING, E.QUICK_CHECK_FAILED) is not None
    assert EscalationRouter.route(StreamId.task(task), E.QUICK_CHECK_FAILED) == Route(
        SupervisorLevel.TASK, task
    )
    # タスク統括はそのままの種類でラン統括へ上げない
    assert EscalationRouter.why_not_relay(TaskKind.IMPLEMENTATION, E.QUICK_CHECK_FAILED) is not None


def test_ラン統括へ上げてよい種類は統括で決まる():
    assert EscalationRouter.why_not_relay(TaskKind.IMPLEMENTATION, E.NEEDS_HUMAN) is None
    assert EscalationRouter.why_not_relay(TaskKind.IMPLEMENTATION, E.STALL) is not None
    # プログラムの統括は、受けたものをそのまま上げる
    assert EscalationRouter.why_not_relay(TaskKind.PLANNING, E.ASK) is None
    assert EscalationRouter.why_not_relay(TaskKind.GIT, E.INTEGRATION_FAILED) is None
    assert EscalationRouter.why_not_relay(TaskKind.GIT, E.NEEDS_REPLAN) is not None
    assert EscalationRouter.relays_as_is(TaskKind.PLANNING)
    assert EscalationRouter.relays_as_is(TaskKind.GIT)
    assert not EscalationRouter.relays_as_is(TaskKind.IMPLEMENTATION)


def test_タスクのストリームからタスクを引く():
    assert task_of_stream(StreamId.task(TaskId("task3"))) == TaskId("task3")
    assert task_of_stream(StreamId("task/git")) == TaskId.git()
