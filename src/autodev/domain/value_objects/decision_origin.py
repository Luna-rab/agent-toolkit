from __future__ import annotations

from enum import Enum


class DecisionOrigin(Enum):
    USER = "user"
    RUN_SUPERVISOR = "run-supervisor"
