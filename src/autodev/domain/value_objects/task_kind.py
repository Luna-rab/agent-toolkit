from __future__ import annotations

from enum import Enum


class TaskKind(Enum):
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    GIT = "git"
