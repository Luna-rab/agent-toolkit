"""ファイルの書き方。"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    """一時ファイルに書いてから置き換える。

    途中で落ちても壊れた中身が残らず、読む側（Monitor・HUD・status）が書きかけを読まない。
    一時ファイルを同じディレクトリに作るのは、`os.replace` がファイルシステムをまたげないため。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def utc_now() -> str:
    """イベントの `at` などに入れる時刻。秒の小数まで持つ UTC の ISO 8601。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
