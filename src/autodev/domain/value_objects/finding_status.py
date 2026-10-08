from __future__ import annotations

from enum import Enum


class FindingStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    REJECTED = "rejected"
    #: 再計画で別のタスクへ移した元。未解決に数えない。終端
    CARRIED = "carried"
