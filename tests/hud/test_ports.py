"""外とのやり取り（`hud/ports/git.py`・`hud/ports/autodev.py`）。本物の git と、偽の autodev の入口で確かめる。"""

from __future__ import annotations

import json
import subprocess
import sys

from hud_samples import calls, status, write_fake_entry, write_usage
from hud_samples import usage as sample_usage

from hud.ports import autodev, git, usage


def test_git_statusの出力を返しgitの外ならNone(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    (repo / "b").write_text("new")
    out = git.status(str(repo))
    assert out is not None and "# branch.head main" in out and "? b" in out
    assert git.status(str(tmp_path)) is None


def test_入口の指定が無ければインストールされたautodevをモジュールの実行で呼ぶ(monkeypatch):
    monkeypatch.delenv(autodev.ENTRY_ENV, raising=False)
    assert autodev.command() == [sys.executable, "-I", "-m", "autodev"]


def test_入口の指定があればそのファイルをそのまま実行する(monkeypatch):
    monkeypatch.setenv(autodev.ENTRY_ENV, "/x/fake.py")
    assert autodev.command() == [sys.executable, "/x/fake.py"]


def test_インストールされたautodevのstatusを呼べる(tmp_path, monkeypatch):
    """本物の入口（`python -I -m autodev`）を、ラン記録の無い空の状態ディレクトリで呼ぶ。"""
    monkeypatch.delenv(autodev.ENTRY_ENV, raising=False)
    monkeypatch.setenv("AUTODEV_STATE_DIR", str(tmp_path / "state"))
    reply = autodev.statuses()
    assert (reply.code, reply.data) == (0, [])


def fake_env(tmp_path, monkeypatch, statuses: list[dict]) -> None:
    for key, value in write_fake_entry(tmp_path, statuses).items():
        monkeypatch.setenv(key, value)


def test_一覧はstatus_jsonを名前なしで呼ぶ(tmp_path, monkeypatch):
    fake_env(tmp_path, monkeypatch, [status(), {"name": "old", "error": "ValueError: x"}])
    reply = autodev.statuses()
    assert (reply.code, [st["name"] for st in reply.data]) == (0, ["add-cache", "old"])
    assert calls(tmp_path) == ["status --json"]


def test_1つのランはnameを付けて呼びランが無ければ終了コード5(tmp_path, monkeypatch):
    fake_env(tmp_path, monkeypatch, [status()])
    assert autodev.status("add-cache").data["name"] == "add-cache"
    missing = autodev.status("nope")
    assert (missing.code, missing.data, missing.message) == (5, None, "autodev: そのランが無い")
    assert calls(tmp_path) == ["status --json --name add-cache", "status --json --name nope"]


def test_入口が無い_JSONでない_返らないときも落ちない(tmp_path, monkeypatch):
    monkeypatch.setenv(autodev.ENTRY_ENV, str(tmp_path / "none.py"))
    missing = autodev.statuses()
    assert missing.code != 0
    assert missing.data is None
    entry = tmp_path / "noisy.py"
    entry.write_text("print('not json')", encoding="utf-8")
    monkeypatch.setenv(autodev.ENTRY_ENV, str(entry))
    assert (autodev.statuses().code, autodev.statuses().data) == (0, None)
    entry.write_text("import time; time.sleep(5)", encoding="utf-8")
    monkeypatch.setattr(autodev, "TIMEOUT", 0.2)
    slow = autodev.statuses()
    assert slow.code is None and "返らなかった" in slow.message


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
