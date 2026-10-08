from __future__ import annotations

from .base import Text, non_blank


class Instruction(Text):
    """`/autodev` に渡された指示の本文。"""

    def _check(self) -> None:
        non_blank("指示", self.value)
