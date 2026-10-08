from __future__ import annotations

import re
from typing import ClassVar

from .base import Text
from .task_kind import TaskKind

#: TaskId の形。StreamId の形もこれから組む
TASK_ID_PATTERN = r"task[1-9][0-9]*|planning|git"


class TaskId(Text):
    """`task<番号>`。計画タスクと git 管理タスクは、ランに 1 つずつなので固定の名前を持つ。

    番号を使い回さないこと（捨てたタスクのブランチが残る）は、使った番号を知っている
    Run が確かめる。
    """

    PATTERN = re.compile(TASK_ID_PATTERN)
    PLANNING: ClassVar[str] = "planning"
    GIT: ClassVar[str] = "git"

    @classmethod
    def numbered(cls, number: int) -> TaskId:
        return cls(f"task{number}")

    @classmethod
    def planning(cls) -> TaskId:
        return cls(cls.PLANNING)

    @classmethod
    def git(cls) -> TaskId:
        return cls(cls.GIT)

    @property
    def number(self) -> int | None:
        """実装タスクの番号。計画タスクと git 管理タスクは None。"""
        if self.value.startswith("task"):
            return int(self.value[len("task") :])
        return None

    @property
    def kind(self) -> TaskKind:
        """id から決まるタスクの種類。計画タスクと git 管理タスクは固定の名前を持つ。"""
        if self.value == self.PLANNING:
            return TaskKind.PLANNING
        if self.value == self.GIT:
            return TaskKind.GIT
        return TaskKind.IMPLEMENTATION
