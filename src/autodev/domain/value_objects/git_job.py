from __future__ import annotations

from dataclasses import dataclass

from .base import InvalidValue
from .branch_name import BranchName
from .cut_point import CutPoint
from .git_job_kind import GitJobKind
from .stack_entry import StackEntry
from .task_id import TaskId


@dataclass(frozen=True)
class GitJob:
    """git 管理タスクの仕事 1 つと、その相手。Stack の列に並び、1 つずつ取り出される。

    git 管理タスクの統括（プログラム）は、取り出した仕事から決まった並びを組むだけで、相手（タスク・
    ブランチ・base・閉じる所）は仕事が持つ。`base` と `cut_from` は、取り出したときに Stack が埋める
    （そのときのスタックの一番上・閉じる中で一番下）。
    """

    #: Stack が列に入れた順に振る番号（1 から）
    id: int
    kind: GitJobKind
    #: 相手のタスク。切る・積むタスク、worktree を使うタスク（概要・stack-top は計画タスク）
    task: TaskId | None = None
    #: 切る・積むブランチ。概要 PR の仕事は概要ブランチ
    branch: BranchName | None = None
    #: 載せる先。切る・積む仕事はスタックの一番上、概要ブランチを切る仕事はランの base
    base: BranchName | None = None
    #: 積み直すとき、前に積んだブランチ。CutBranch がそこから新しい名前で切り直す
    previous: BranchName | None = None
    #: 破棄する仕事: 破棄したタスク
    discarded: frozenset[TaskId] = frozenset()
    #: 破棄する仕事: 閉じる中で一番下。閉じるものが無ければ None
    cut_from: StackEntry | None = None
    #: 仕上げ: 概要 PR を draft から外すか
    ready_overview: bool = False

    def __post_init__(self) -> None:
        if self.id < 1:
            raise InvalidValue(f"仕事の番号は 1 以上: {self.id}")

    @property
    def cut_point(self) -> CutPoint | None:
        """CutBranch が切る元（4 つの場合がある）。切る元を持たない仕事は None。

        stack-top は一番上のコミットに HEAD を固定する。概要ブランチはランの base から切る。積み直す
        タスクは前に積んだブランチから、ほかはスタックの一番上から、新しい名前で切る。
        """
        if self.kind is GitJobKind.CUT_STACK_TOP:
            return CutPoint(self.base, detached=True) if self.base is not None else None
        if self.kind is GitJobKind.CUT_OVERVIEW:
            return CutPoint.for_overview(self.base) if self.base is not None else None
        if self.previous is not None:
            return CutPoint(self.previous, roots_branch=False)
        return CutPoint(self.base) if self.base is not None else None
