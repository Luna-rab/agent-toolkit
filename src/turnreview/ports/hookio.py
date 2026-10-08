"""hook の入出力。stdin の payload を読み、stdout に JSON を書く。"""

from __future__ import annotations

import json
import sys


def read_payload() -> dict | None:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def write(output: dict) -> None:
    print(json.dumps(output, ensure_ascii=False))
