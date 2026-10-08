from __future__ import annotations

from enum import Enum


class StageExit(Enum):
    """ステージのプロセスの終わり方。"""

    OK = "ok"
    ERROR = "error"
