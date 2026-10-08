"""編集されたファイルを読む。一時ディレクトリの場所を返す。"""

from __future__ import annotations

import tempfile
from pathlib import Path


def read_lines(path: Path) -> list[str] | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def first_line(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return handle.readline(200)
    except OSError:
        return None


def temp_roots() -> tuple[Path, ...]:
    """scratchpad が置かれうる一時ディレクトリ。`TMPDIR` が変えられていても `/tmp` は見る。"""
    return (Path(tempfile.gettempdir()), Path("/tmp"))
