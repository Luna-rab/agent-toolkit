from __future__ import annotations

from typing import ClassVar

from .base import Number


class ParallelLimit(Number):
    """同時に走る実装タスクの上限。"""

    DEFAULT: ClassVar[int] = 3
