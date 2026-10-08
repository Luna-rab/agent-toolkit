from __future__ import annotations

import re

from .base import InvalidValue, Text


class Location(Text):
    """指摘の位置。`path`・`path:12`・`path:12-15` のどれか。"""

    # path は短い方から伸ばす。長い方からだと `a.py:3` の `:3` まで path に取られる
    PATTERN = re.compile(
        r"(?P<path>\S(?:.*?\S)?)(?::(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?)?"
    )

    def _check(self) -> None:
        match = self._match()
        if match["end"] is not None and int(match["end"]) < int(match["start"]):
            raise InvalidValue(f"範囲の終わりが始まりより前: {self.value!r}")

    def _match(self) -> re.Match[str]:
        match = Location.PATTERN.fullmatch(self.value)
        assert match is not None  # __post_init__ で確かめてある
        return match

    @property
    def path(self) -> str:
        return self._match()["path"]

    @property
    def lines(self) -> tuple[int, int] | None:
        """行の範囲（1 行なら始まりと終わりが同じ）。行番号の無い位置は None。"""
        match = self._match()
        if match["start"] is None:
            return None
        start = int(match["start"])
        return start, int(match["end"] or start)
