"""値オブジェクトの土台。

どれも不変で、等しさは値で決まる。作るときに形を検査し、不正な値はその場で `InvalidValue` にする。
集約の状態を見ないと決められない検査（「使ったことのある番号と重ねない」など）は、ここではなく
集約の `handle` で行う。

文字列 1 つ・整数 1 つを包む値は `Text`・`Number` を土台にする。イベントストアの JSON では、
包みを外した素の値になる（`codec.py`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar


class InvalidValue(ValueError):
    """値オブジェクトの形が不正。"""


@dataclass(frozen=True)
class Text:
    """文字列 1 つを包む値の土台。`PATTERN` があれば、全体がそれに合うことを求める。"""

    value: str
    PATTERN: ClassVar[re.Pattern[str] | None] = None

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise InvalidValue(f"{type(self).__name__} は文字列: {self.value!r}")
        if self.PATTERN is not None and not self.PATTERN.fullmatch(self.value):
            raise InvalidValue(f"{type(self).__name__} の形が違う: {self.value!r}")
        self._check()

    def _check(self) -> None:
        """`PATTERN` で書けない検査を足すときに上書きする。"""

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Number:
    """整数 1 つを包む値の土台。`MINIMUM` 以上を求める。"""

    value: int
    MINIMUM: ClassVar[int] = 1

    def __post_init__(self) -> None:
        # bool は int の部分型なので、True を 1 として通さない
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise InvalidValue(f"{type(self).__name__} は整数: {self.value!r}")
        if self.value < self.MINIMUM:
            raise InvalidValue(f"{type(self).__name__} は {self.MINIMUM} 以上: {self.value}")

    def __str__(self) -> str:
        return str(self.value)


def non_blank(owner: str, value: str) -> None:
    if not value.strip():
        raise InvalidValue(f"{owner} が空")
