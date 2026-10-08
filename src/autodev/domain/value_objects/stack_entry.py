from __future__ import annotations

from dataclasses import dataclass

from .branch_name import BranchName
from .pr_number import PrNumber
from .task_id import TaskId


@dataclass(frozen=True)
class StackEntry:
    """スタックに積んだ 1 本。一番下は概要ブランチで、その base はランの base。"""

    task: TaskId
    branch: BranchName
    pr: PrNumber
    base: BranchName
