from __future__ import annotations

import re

from .base import Text
from .task_id import TASK_ID_PATTERN, TaskId


class StreamId(Text):
    """集約 1 つぶんのイベントの列。"""

    PATTERN = re.compile(
        rf"run|design|stack|questions|review/design|(?:task|review)/(?:{TASK_ID_PATTERN})"
    )

    @classmethod
    def run(cls) -> StreamId:
        return cls("run")

    @classmethod
    def task(cls, task: TaskId) -> StreamId:
        return cls(f"task/{task}")

    @classmethod
    def review(cls, task: TaskId) -> StreamId:
        return cls(f"review/{task}")

    @classmethod
    def design_review(cls) -> StreamId:
        return cls("review/design")

    @classmethod
    def design(cls) -> StreamId:
        return cls("design")

    @classmethod
    def stack(cls) -> StreamId:
        return cls("stack")

    @classmethod
    def questions(cls) -> StreamId:
        return cls("questions")

    @property
    def is_review(self) -> bool:
        return self.value.startswith("review/")

    @property
    def is_task(self) -> bool:
        """タスクのストリーム（`task/<TaskId>`）か。"""
        return self.value.startswith("task/")
