from __future__ import annotations

from dataclasses import dataclass

from .task_id import TaskId
from .task_spec import TaskSpec


@dataclass(frozen=True)
class PlannedTask:
    """提案の中のタスク 1 つ。残すタスクは今の id、新しいタスクはまだ使っていない番号の id で書く。"""

    id: TaskId
    spec: TaskSpec
    blocked_by: frozenset[TaskId] = frozenset()
