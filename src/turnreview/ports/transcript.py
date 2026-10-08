"""transcript（会話の JSONL）を、エントリの列として読む。壊れた行は飛ばす。"""

from __future__ import annotations

import json
from pathlib import Path


def entries(path: Path) -> list[dict]:
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return []
    found: list[dict] = []
    with handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                found.append(entry)
    return found
