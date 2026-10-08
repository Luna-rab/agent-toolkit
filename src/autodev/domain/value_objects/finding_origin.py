from __future__ import annotations

from dataclasses import dataclass

from .finding_id import FindingId
from .stream_id import StreamId


@dataclass(frozen=True)
class FindingOrigin:
    """移した指摘の、移す元。"""

    ledger: StreamId
    finding: FindingId
