from __future__ import annotations

from enum import Enum


class DesignCause(Enum):
    """設計のジャッジの分類。"""

    REVERTED = "reverted"
    AMBIGUOUS = "ambiguous"
