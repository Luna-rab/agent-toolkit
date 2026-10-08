from __future__ import annotations

from enum import Enum


class TaskStatus(Enum):
    """Run が持つ、各タスクの状態。"""

    PENDING = "pending"
    RUNNING = "running"
    ESCALATED = "escalated"
    GATED = "gated"
    STACKING = "stacking"
    STACKED = "stacked"
    DROPPED = "dropped"
    SUPERSEDED = "superseded"
    DISCARDED = "discarded"
    #: 計画タスクと git 管理タスクの終端。RunFinished の後に移る
    FINISHED = "finished"

    @property
    def is_terminal(self) -> bool:
        """終端の状態か。`stacked` は破棄されると動くが、終端に数える。"""
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.STACKED,
        TaskStatus.DROPPED,
        TaskStatus.SUPERSEDED,
        TaskStatus.DISCARDED,
        TaskStatus.FINISHED,
    }
)
