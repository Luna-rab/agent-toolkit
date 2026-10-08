from __future__ import annotations

from dataclasses import dataclass

from .session_id import SessionId


@dataclass(frozen=True)
class Pointers:
    """調べる先。パスはランディレクトリから。"""

    task_dir: str | None = None
    result: str | None = None
    log: str | None = None
    tree: str | None = None
    session: SessionId | None = None
