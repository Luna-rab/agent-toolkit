"""報告済みの鍵を、セッションごとに一時ディレクトリへ読み書きする。書けなくても止めない。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


class ReportStore:
    def __init__(self, directory: str, file_stem: str) -> None:
        self.path = Path(tempfile.gettempdir()) / directory / f"{file_stem}.json"

    def load(self) -> set[str]:
        try:
            return set(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return set()

    def save(self, keys: set[str]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(sorted(keys)), encoding="utf-8")
        except OSError:
            pass
