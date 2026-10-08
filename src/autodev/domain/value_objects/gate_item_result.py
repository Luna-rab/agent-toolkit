from __future__ import annotations

from dataclasses import dataclass

from .gate_item import GateItem


@dataclass(frozen=True)
class GateItemResult:
    item: GateItem
    passed: bool
    reason: str = ""
