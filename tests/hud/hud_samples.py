"""hud の検査で使う入力。Claude Code が渡す JSON と、claude.ai の利用状況。"""

from __future__ import annotations

import json


def session(now: float) -> dict:
    return {
        "model": {"display_name": "Opus 5.5"},
        "effort": {"level": "high"},
        "workspace": {"current_dir": "/nonexistent/dotfiles"},
        "context_window": {"used_percentage": 50},
        "cost": {"total_cost_usd": 3.214},
        "rate_limits": {
            # 窓の 6 割が過ぎたところで 72% 使っている → 12 ポイント使いすぎ
            "five_hour": {"used_percentage": 72, "resets_at": now + 5 * 3600 * 0.4 + 30},
            "seven_day": {"used_percentage": 31},
        },
        "pr": {"number": 22, "url": "https://example.com/pull/22", "review_state": "pending"},
    }


def usage() -> dict:
    """claude.ai の利用状況。月 $800 の上限のうち $5.64 使っている。"""
    return {
        "five_hour": None,
        "seven_day": None,
        "extra_usage": {
            "is_enabled": True,
            "monthly_limit": 80000,
            "used_credits": 564,
            "decimal_places": 2,
        },
    }


def write_usage(path, checked_at: float) -> None:
    """`hud/ports/usage.py` のキャッシュ。`checked_at` が新しければ取りに行かずにこれを使う。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"checked_at": checked_at, "data": usage()}), encoding="utf-8")
