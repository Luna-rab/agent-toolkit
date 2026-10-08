from __future__ import annotations

import re

from .base import Text, non_blank


class OverviewPrTitle(Text):
    """概要 PR の題。頭に、autodev が作った概要 PR と分かる印 `[autodev]` を持つ。

    印はステージには書かせず、driver が `for_run` で付ける。
    """

    PATTERN = re.compile(r"\[autodev\] \S.*")
    MARK = "[autodev]"

    @classmethod
    def for_run(cls, subject: str) -> OverviewPrTitle:
        """WriteOverview が書いた 1 行に印を付ける。指示書に反して印まで書いてきても、二重に付けない。"""
        line = subject.strip().removeprefix(cls.MARK).strip()
        non_blank("概要 PR のタイトル", line)
        return cls(f"{cls.MARK} {line}")
