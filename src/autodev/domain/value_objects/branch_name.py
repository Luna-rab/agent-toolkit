from __future__ import annotations

import re

from .base import InvalidValue, Text
from .run_name import RunName


class BranchName(Text):
    """git のブランチ名。ランの base（`main` など）もこれで持つ。

    autodev が切るブランチの規約は
    `overview` と `for_task` で作る。使ったことのある名前と重ねないことは、集約が確かめる。
    """

    # 使える文字を絞ってあるので、git check-ref-format が拒む空白・制御文字・`~^:?*[\`・`@{` は入らない
    PATTERN = re.compile(r"[A-Za-z0-9._/-]+")

    def _check(self) -> None:
        """git check-ref-format（と `git branch`）が拒む形を拒む。"""
        name = self.value
        if name == "HEAD" or name.startswith("-") or name.endswith(".") or ".." in name:
            raise InvalidValue(f"git が受けないブランチ名: {name!r}")
        for part in name.split("/"):
            # 先頭と末尾の `/`・`//` は空の成分になる
            if not part or part.startswith(".") or part.endswith(".lock"):
                raise InvalidValue(f"git が受けないブランチ名: {name!r}")

    @classmethod
    def overview(cls, run: RunName) -> BranchName:
        return cls(f"stack/{run}--task-0")

    @classmethod
    def for_task(cls, run: RunName, number: int, branch_round: int = 0) -> BranchName:
        """タスクのブランチ。`branch_round` は切り直した回数（Run の TaskEntry.branch_round）で、
        破棄の後に積み直す・積む列から外して始め直すたびに、新しい名前で切り直す。"""
        if number < 1 or branch_round < 0:
            raise InvalidValue(
                f"タスクの番号は 1 以上・切り直した回数は 0 以上: {number}, {branch_round}"
            )
        suffix = f"-r{branch_round}" if branch_round else ""
        return cls(f"stack/{run}--task-{number}{suffix}")

    @staticmethod
    def run_prefix(run: RunName) -> str:
        """そのランが切るブランチ（概要ブランチも）がどれも始まる文字列。`purge` が手元のブランチを集める。

        ラン名に `--` は入らないので、`stack/a--task-` は `stack/a-b--task-1` に当たらない。
        """
        return f"stack/{run}--task-"
