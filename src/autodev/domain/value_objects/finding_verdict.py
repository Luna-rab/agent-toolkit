from __future__ import annotations

from dataclasses import dataclass

from .finding_id import FindingId
from .finding_status import FindingStatus
from .finding_target import FindingTarget


@dataclass(frozen=True)
class FindingVerdict:
    """ジャッジの判定 1 件（JudgeFinding）。"""

    finding: FindingId
    to: FindingStatus
    comment: str
    #: `None` は今の宛先のまま
    target: FindingTarget | None = None
