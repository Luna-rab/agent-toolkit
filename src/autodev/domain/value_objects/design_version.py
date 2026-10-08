from __future__ import annotations

from .base import Number


class DesignVersion(Number):
    """設計ファイルの版。本文は `design/v<版>.md` にあり、イベントは版の番号だけを持つ。"""

    @property
    def path(self) -> str:
        return f"design/v{self.value}.md"
