"""Claude Code の JSON の読み方（`hud/core/session.py`）と利用枠のペース（`hud/core/limits.py`）。"""

from __future__ import annotations

import pytest
from hud_samples import session as sample

from hud.core import session
from hud.core.limits import Limit, until


def test_表示に要る値だけを取り出す():
    got = session.parse(sample(1000.0), 1000.0)
    assert (got.model, got.effort, got.context_pct, got.cost) == ("Opus 5.5", "high", 50.0, 3.214)
    assert (got.repo, got.cwd, got.worktree) == ("dotfiles", "/nonexistent/dotfiles", "")
    assert [(lim.label, lim.used) for lim in got.limits] == [("5h", 72.0), ("7d", 31.0)]
    assert got.limits[1].remaining is None


def test_リポジトリ名はサブディレクトリでもプロジェクトのルートから取る():
    data = {"workspace": {"current_dir": "/w/repo/sub", "project_dir": "/w/repo/"}}
    assert session.parse(data, 0).repo == "repo"


def test_入力が辞書でなくても落ちない():
    got = session.parse(None, 0)
    assert (got.model, got.limits) == ("?", ())


@pytest.mark.parametrize(
    ("used", "remaining", "want"), [(72, 7200, 12), (10, 9000, -40), (0, 18000, 0)]
)
def test_ペースは使った割合から窓の経過割合を引く(used, remaining, want):
    assert Limit("5h", used, remaining, 5 * 3600).pace == want


def test_リセットの時刻が無ければペースは出さない():
    assert Limit("7d", 31, None, 7 * 86400).pace is None


@pytest.mark.parametrize(
    ("seconds", "want"),
    [(59 * 60, "59m"), (3 * 3600 + 5 * 60, "3h05m"), (2 * 86400 + 5 * 3600, "2d05h")],
)
def test_リセットまでの残りは1日以上なら日と時(seconds, want):
    assert until(seconds) == want
