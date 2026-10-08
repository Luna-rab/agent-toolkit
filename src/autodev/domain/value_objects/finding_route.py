from __future__ import annotations

from dataclasses import dataclass

from .finding_id import FindingId
from .finding_target import FindingTarget


@dataclass(frozen=True)
class FindingRoute:
    """open の指摘 1 件の宛先。Task が、判定の後の行き先を決めるのに使う。"""

    finding: FindingId
    target: FindingTarget
