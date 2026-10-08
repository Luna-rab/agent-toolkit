from __future__ import annotations

from dataclasses import dataclass

from .base import InvalidValue
from .stage_kind import StageKind
from .task_id import TaskId


@dataclass(frozen=True)
class ExecutionId:
    """ステージの実行 1 回の id。ラウンドは ReviewLoop・DesignLoop の中で 1 から、ほかは 0。"""

    task: TaskId
    stage: StageKind
    round: int
    attempt: int

    def __post_init__(self) -> None:
        if self.round < 0 or self.attempt < 1:
            raise InvalidValue(f"ラウンドは 0 以上・試行は 1 以上: {self.round}, {self.attempt}")

    def __str__(self) -> str:
        return f"{self.task}-{self.stage.value}-r{self.round}-a{self.attempt}"
