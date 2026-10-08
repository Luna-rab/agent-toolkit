from __future__ import annotations

from dataclasses import dataclass

from .gate_item_result import GateItemResult


@dataclass(frozen=True)
class GateReport:
    """完了チェックの項目ごとの合否。"""

    items: tuple[GateItemResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.items)

    @property
    def failed(self) -> tuple[GateItemResult, ...]:
        return tuple(result for result in self.items if not result.passed)
