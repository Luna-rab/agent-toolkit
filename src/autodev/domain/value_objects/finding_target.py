from __future__ import annotations

from enum import Enum


class FindingTarget(Enum):
    """指摘の宛先。直す者を決める。"""

    SUPERVISOR = "supervisor"
    IMPL = "impl"
    TESTS = "tests"
