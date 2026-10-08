"""始められる実装タスクを選ぶ。

Run が `StartTask`・`StartReadyTasks` を受けたときに使う。実装タスクを始めてよいかを決めるのは
ここだけである。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..value_objects.parallel_limit import ParallelLimit
from ..value_objects.task_id import TaskId
from ..value_objects.task_kind import TaskKind
from ..value_objects.task_status import TaskStatus


@dataclass(frozen=True)
class SchedulingEntry:
    """選ぶのに要る、タスク 1 つの状態。"""

    id: TaskId
    status: TaskStatus
    blocked_by: frozenset[TaskId] = frozenset()


class TaskScheduler:
    #: 並列の上限に数える状態。escalated のタスクも worktree とフローを持ったまま止まっていて、
    #: 回答が届けば走り出す。数えないと、回答が届いたときに上限を超えて走る
    OCCUPYING = frozenset({TaskStatus.RUNNING, TaskStatus.ESCALATED})

    @classmethod
    def frees_slot(cls, before: TaskStatus, after: TaskStatus) -> bool:
        """その遷移で、並列の上限の枠が 1 つ空くか。"""
        return before in cls.OCCUPYING and after not in cls.OCCUPYING

    @classmethod
    def occupied(cls, tasks: Mapping[TaskId, SchedulingEntry]) -> int:
        return sum(
            1
            for entry in tasks.values()
            if entry.id.kind is TaskKind.IMPLEMENTATION and entry.status in cls.OCCUPYING
        )

    @classmethod
    def why_not(
        cls, task: TaskId, tasks: Mapping[TaskId, SchedulingEntry], limit: ParallelLimit
    ) -> str | None:
        """`task` を今始められないなら、その理由。始められるなら None。"""
        entry = tasks.get(task)
        if entry is None:
            return f"{task} は計画に無い"
        if task.kind is not TaskKind.IMPLEMENTATION:
            return f"{task} は実装タスクではない"
        if entry.status is not TaskStatus.PENDING:
            return f"{task} は {entry.status.value} で、まだ始めていないタスクではない"
        if waiting := cls._unstacked(entry, tasks):
            return f"{task} が待つ {', '.join(waiting)} がまだ積まれていない"
        if cls.occupied(tasks) >= limit.value:
            return f"走っている実装タスクが上限（{limit.value}）に達している"
        return None

    @classmethod
    def startable(
        cls, tasks: Mapping[TaskId, SchedulingEntry], limit: ParallelLimit
    ) -> tuple[TaskId, ...]:
        """今始められるタスクを、番号の小さい順に、上限の空きの数だけ。"""
        free = limit.value - cls.occupied(tasks)
        if free <= 0:
            return ()
        ready = [
            entry.id
            for entry in tasks.values()
            if entry.id.kind is TaskKind.IMPLEMENTATION
            and entry.status is TaskStatus.PENDING
            and not cls._unstacked(entry, tasks)
        ]
        ready.sort(key=lambda task: task.number or 0)
        return tuple(ready[:free])

    @staticmethod
    def _unstacked(entry: SchedulingEntry, tasks: Mapping[TaskId, SchedulingEntry]) -> list[str]:
        return sorted(
            str(dependency)
            for dependency in entry.blocked_by
            if (other := tasks.get(dependency)) is None or other.status is not TaskStatus.STACKED
        )
