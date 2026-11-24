"""外とのやり取り（`hud/ports/git.py`・`hud/ports/usage.py`）。本物の git で確かめる。"""

from __future__ import annotations

import json
import subprocess

from hud_samples import usage as sample_usage
from hud_samples import write_usage

from hud.ports import git, usage


def test_git_statusの出力を返しgitの外ならNone(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    (repo / "b").write_text("new")
    out = git.status(str(repo))
    assert out is not None and "# branch.head main" in out and "? b" in out
    assert git.status(str(tmp_path)) is None


def usage_env(tmp_path, monkeypatch, fetched) -> list[str]:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "claude").mkdir()
    creds = {"claudeAiOauth": {"accessToken": "tok"}}
    (tmp_path / "claude" / ".credentials.json").write_text(json.dumps(creds), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(usage, "fetch", lambda token: calls.append(token) or fetched)
    return calls


def test_キャッシュが新しければ取りに行かない(tmp_path, monkeypatch):
    calls = usage_env(tmp_path, monkeypatch, {"other": 1})
    write_usage(tmp_path / "cache" / "claude-hud" / "usage.json", 1000.0)
    assert usage.usage(1000.0 + usage.TTL - 1) == sample_usage()
    assert calls == []


def test_キャッシュが古ければ取りに行って書き込む(tmp_path, monkeypatch):
    calls = usage_env(tmp_path, monkeypatch, {"fresh": 1})
    write_usage(tmp_path / "cache" / "claude-hud" / "usage.json", 1000.0)
    assert usage.usage(1000.0 + usage.TTL) == {"fresh": 1}
    assert calls == ["tok"]
    assert usage.usage(1000.0 + usage.TTL + 1) == {"fresh": 1}
    assert calls == ["tok"]


def test_取れなければ前のものを返し次のTTL秒は取りに行かない(tmp_path, monkeypatch):
    calls = usage_env(tmp_path, monkeypatch, None)
    write_usage(tmp_path / "cache" / "claude-hud" / "usage.json", 1000.0)
    assert usage.usage(2000.0) == sample_usage()
    assert usage.usage(2001.0) == sample_usage()
    assert calls == ["tok"]


def test_トークンが無ければ取りに行かない(tmp_path, monkeypatch):
    calls = usage_env(tmp_path, monkeypatch, {"fresh": 1})
    (tmp_path / "claude" / ".credentials.json").unlink()
    assert usage.usage(1000.0) is None
    assert calls == []
