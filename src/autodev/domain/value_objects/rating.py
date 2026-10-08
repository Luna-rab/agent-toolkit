from __future__ import annotations

from enum import Enum


class Rating(Enum):
    MUST_FIX = "must-fix"
    SHOULD_FIX = "should-fix"
    NIT = "nit"
