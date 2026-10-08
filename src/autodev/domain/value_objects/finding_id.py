from __future__ import annotations

import re

from .base import InvalidValue, Text
from .gate_item import GATE_ITEM_VALUES, GateItem


class FindingId(Text):
    """`R<番号>`（タスクの台帳）・`D<番号>`（設計の台帳）・`G-<項目>`（Gate の項目ごとの指摘）。

    台帳の中で一意かは、ReviewLedger が確かめる。
    """

    PATTERN = re.compile(r"[RD][1-9][0-9]*|G-[a-z][a-z-]*")

    def _check(self) -> None:
        if self.value.startswith("G-") and self.value[len("G-") :] not in GATE_ITEM_VALUES:
            raise InvalidValue(f"Gate の項目に無い: {self.value!r}")

    @classmethod
    def review(cls, number: int) -> FindingId:
        return cls(f"R{number}")

    @classmethod
    def design(cls, number: int) -> FindingId:
        return cls(f"D{number}")

    @classmethod
    def gate(cls, item: GateItem) -> FindingId:
        return cls(f"G-{item.value}")

    @property
    def gate_item(self) -> GateItem | None:
        """Gate の項目の指摘なら、その項目。レビューの指摘（R・D）は None。"""
        if self.value.startswith("G-"):
            return GateItem(self.value[len("G-") :])
        return None
