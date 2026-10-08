from __future__ import annotations

from dataclasses import dataclass

from .base import non_blank
from .branch_name import BranchName
from .commit_sha import CommitSha
from .task_id import TaskId


@dataclass(frozen=True)
class WorktreeCut:
    """CutBranch が切った worktree（WorktreeReady）。"""

    #: その worktree を使うタスク
    task: TaskId
    #: ランディレクトリからのパス
    tree: str
    branch: BranchName | None = None
    #: 切った元のコミット。タスクのブランチなら、そのタスクの差分の起点になる（Task.base_commit）
    base: CommitSha | None = None

    def __post_init__(self) -> None:
        non_blank("worktree の在りか", self.tree)
