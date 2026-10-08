"""Claude Code が statusline に渡す JSON を、表示に要る値だけの形にする。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hud.core.limits import Limit

#: 利用枠の窓の長さ（秒）。ペース（窓の経過に対して使いすぎているか）の計算に使う
WINDOWS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}


@dataclass(frozen=True)
class Session:
    model: str
    effort: str
    context_pct: float
    cost: float
    #: プロジェクトのルートの名前。サブディレクトリで動いていても変わらない
    repo: str
    cwd: str
    worktree: str
    #: Pro / Max のサブスクリプションでしか渡らない。渡らなければ空
    limits: tuple[Limit, ...]


def dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def parse(data: Any, now: float) -> Session:
    """`now` は UNIX 時刻。利用枠のリセットまでの残りを出すのに使う。"""
    cwd = str(dig(data, "workspace", "current_dir") or "")
    project = str(dig(data, "workspace", "project_dir") or cwd)
    return Session(
        model=str(dig(data, "model", "display_name") or "?"),
        effort=str(dig(data, "effort", "level") or ""),
        context_pct=float(dig(data, "context_window", "used_percentage") or 0),
        cost=float(dig(data, "cost", "total_cost_usd") or 0),
        repo=project.rstrip("/").rsplit("/", 1)[-1] or project,
        cwd=cwd,
        worktree=str(dig(data, "worktree", "name") or ""),
        limits=tuple(
            limit
            for key, label in (("five_hour", "5h"), ("seven_day", "7d"))
            if (limit := _limit(data, key, label, now)) is not None
        ),
    )


def _limit(data: Any, key: str, label: str, now: float) -> Limit | None:
    used = dig(data, "rate_limits", key, "used_percentage")
    if used is None:
        return None
    resets = dig(data, "rate_limits", key, "resets_at")
    remaining = None if resets is None else float(resets) - now
    return Limit(label=label, used=float(used), remaining=remaining, window=WINDOWS[key])
