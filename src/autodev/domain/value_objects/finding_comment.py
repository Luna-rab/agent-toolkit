from __future__ import annotations

from dataclasses import dataclass

from .finding_id import FindingId


@dataclass(frozen=True)
class FindingComment:
    """指摘への直し方・判断のコメント（CommentFinding）。"""

    finding: FindingId
    body: str
