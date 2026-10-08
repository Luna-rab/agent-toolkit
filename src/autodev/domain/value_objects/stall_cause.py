from __future__ import annotations

from enum import Enum


class StallCause(Enum):
    """ジャッジが停滞に付ける分類。"""

    TESTS = "tests"
    APPROACH = "approach"
    SCOPE = "scope"
    AMBIGUOUS = "ambiguous"
