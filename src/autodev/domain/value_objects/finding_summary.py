from __future__ import annotations

from dataclasses import dataclass

from .finding_id import FindingId
from .finding_target import FindingTarget
from .location import Location
from .rating import Rating


@dataclass(frozen=True)
class FindingSummary:
    """指摘 1 件の中身。台帳の外（ポリシー・Design）が、台帳を読まずに判断するのに使う。"""

    finding: FindingId
    rating: Rating
    body: str
    location: Location | None = None
    target: FindingTarget = FindingTarget.IMPL
