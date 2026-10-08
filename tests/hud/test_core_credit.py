"""月次のクレジットの読み方（`hud/core/credit.py`）。"""

from __future__ import annotations

import datetime as dt

import pytest
from hud_samples import usage

from hud.core import credit

UTC = dt.timezone.utc


def test_最小単位の金額をドルにして使った割合を出す():
    now = dt.datetime(2026, 10, 1, tzinfo=UTC)
    got = credit.monthly(usage(), now)
    assert got is not None
    assert (got.label, got.money) == ("mo", (5.64, 800.0))
    assert got.used == pytest.approx(0.705)


def test_窓はUTCの月初から次の月初まで():
    now = dt.datetime(2026, 12, 11, tzinfo=UTC)
    got = credit.monthly(usage(), now)
    assert got is not None and got.remaining is not None
    assert got.window == 31 * 86400
    assert got.remaining == 21 * 86400
    assert got.elapsed_pct == pytest.approx(10 / 31 * 100)


@pytest.mark.parametrize(
    "data",
    [
        None,
        {"extra_usage": None},
        {"extra_usage": {"is_enabled": False, "monthly_limit": 80000}},
        {"extra_usage": {"is_enabled": True, "monthly_limit": 0}},
        {"extra_usage": {"is_enabled": True}},
    ],
)
def test_クレジットが無効か上限が無ければ出さない(data):
    assert credit.monthly(data, dt.datetime(2026, 10, 1, tzinfo=UTC)) is None
