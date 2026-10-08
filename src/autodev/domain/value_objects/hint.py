from __future__ import annotations

from dataclasses import dataclass

from .design_cause import DesignCause
from .finding_id import FindingId
from .gate_item import GateItem
from .stall_cause import StallCause


@dataclass(frozen=True)
class Hint:
    """どこから読めばよいかの手がかり。本文は載せない。"""

    finding_ids: tuple[FindingId, ...] = ()
    stall_cause: StallCause | None = None
    design_cause: DesignCause | None = None
    gate_items: tuple[GateItem, ...] = ()
