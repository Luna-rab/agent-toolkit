"""claude.ai の利用状況の JSON から、月次のクレジットの枠を取り出す。

クレジットは毎月 1 日にリセットされる。**何時に切り替わるかは API が返さない**ので、UTC の月初とみなす。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from hud.core.limits import Limit


def month_span(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """`now` を含む月の初めと、次の月の初め（UTC）。"""
    utc = now.astimezone(dt.timezone.utc)
    start = utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def monthly(data: Any, now: dt.datetime) -> Limit | None:
    """`extra_usage` が有効で上限があれば、その枠。金額は最小単位の整数で、`decimal_places` 桁ずらす。"""
    extra = data.get("extra_usage") if isinstance(data, dict) else None
    if not isinstance(extra, dict) or not extra.get("is_enabled"):
        return None
    try:
        scale = 10 ** int(extra.get("decimal_places") or 0)
        cap = float(extra["monthly_limit"]) / scale
        spent = float(extra.get("used_credits") or 0) / scale
    except (KeyError, TypeError, ValueError):
        return None
    if cap <= 0:
        return None
    start, end = month_span(now)
    return Limit(
        label="mo",
        used=spent / cap * 100,
        remaining=(end - now).total_seconds(),
        window=int((end - start).total_seconds()),
        money=(spent, cap),
    )
