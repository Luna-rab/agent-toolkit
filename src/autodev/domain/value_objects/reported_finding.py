from __future__ import annotations

from dataclasses import dataclass

from .finding_target import FindingTarget
from .location import Location
from .rating import Rating


@dataclass(frozen=True)
class ReportedFinding:
    """レビューが挙げた指摘 1 件（Expect の defects も、ここに読み替える）。id は台帳が振る。"""

    rating: Rating
    body: str
    location: Location | None = None
    target: FindingTarget = FindingTarget.IMPL
