from __future__ import annotations

import re

from .base import Text, non_blank
from .pr_number import PrNumber


class TaskPrTitle(Text):
    """タスク PR の題。頭に、どのランの stacked PR か分かる印 `[autodev #<概要 PR の番号>]` を持つ。

    印はステージには書かせず、driver が `for_task` で付ける。
    """

    PATTERN = re.compile(r"\[autodev #[1-9]\d*\] \S.*")
    #: 件名にすでに付いている印。計画が件名に書いてきても、二重に付けない
    _MARK = re.compile(r"^\[autodev(?: #\d+)?\]\s*")

    @classmethod
    def for_task(cls, subject: str, overview_pr: PrNumber) -> TaskPrTitle:
        line = cls._MARK.sub("", subject.strip()).strip()
        non_blank("タスク PR の件名", line)
        return cls(f"[autodev #{overview_pr}] {line}")
