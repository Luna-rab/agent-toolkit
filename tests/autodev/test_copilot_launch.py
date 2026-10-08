"""Copilot の起動の下ごしらえ（`adapters/copilot/launch.py`）。

COPILOT_HOME の置き場、ログインの判定、起動ごとのプラグイン（フック定義）、呼び出し元の
環境変数をそのまま渡す env_for を確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autodev.adapters.agent.types import AgentCall
from autodev.adapters.copilot.launch import (
    HOOK_TIMEOUT_SEC,
    env_for,
    has_login,
    read_config,
    source_home,
    write_plugin,
)
from autodev.domain.value_objects.session_id import SessionId

SESSION = SessionId("0b6f3c1e-9a8d-4c2b-8e7f-1a2b3c4d5e6f")


def make_call(tmp_path: Path, **fields: Any) -> AgentCall:
    values: dict[str, Any] = {
        "prompt": "やること",
        "cwd": str(tmp_path),
        "session": SESSION,
        "resume": False,
        "log_path": str(tmp_path / "run" / "logs" / "task1-Impl-r0-a1.jsonl"),
        "model": "gpt-5.5",
        "effort": "high",
        "run_dir": str(tmp_path / "run"),
    }
    values.update(fields)
    return AgentCall(**values)


def write_config(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(text, encoding="utf-8")
    return directory


# --- 置き場 ---


def test_source_homeはCOPILOT_HOMEを優先し無ければホームの下の_copilot(tmp_path: Path, monkeypatch):
    assert source_home({"COPILOT_HOME": str(tmp_path / "h")}) == tmp_path / "h"
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    assert source_home({}) == tmp_path / "user" / ".copilot"


# --- read_config ---

COMMENTED = """// 先頭のコメント
{
  /* 範囲のコメント */
  "url": "https://github.com", // 行末のコメント
  "authTokens": {"a": "/* not comment */"}
}
"""


def test_read_configはコメントを外し文字列の中の記号は残す(tmp_path: Path):
    source = write_config(tmp_path / "src", COMMENTED)
    config = read_config(source / "config.json")
    assert config["url"] == "https://github.com"
    assert config["authTokens"] == {"a": "/* not comment */"}


def test_read_configは無いファイルを空で返す(tmp_path: Path):
    assert read_config(tmp_path / "none.json") == {}


@pytest.mark.parametrize("text", ["not json", "[1, 2]"])
def test_read_configは読めないか_objectでなければValueError(tmp_path: Path, text: str):
    source = write_config(tmp_path / "src", text)
    with pytest.raises(ValueError):
        read_config(source / "config.json")


# --- has_login ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"authTokens": {"x": "t"}}', True),
        ('{"loggedInUsers": [{"login": "u"}]}', True),
        ('{"firstLaunchAt": "now"}', False),
        ("{}", False),
    ],
)
def test_has_loginは認証の項目の有無で決まる(tmp_path: Path, text: str, expected: bool):
    assert has_login(write_config(tmp_path / "src", text)) is expected


def test_has_loginはconfig_jsonが無ければ偽(tmp_path: Path):
    assert has_login(tmp_path / "empty") is False


# --- env_for ---

BASE = {
    "PATH": "/usr/bin",
    "COPILOT_PROVIDER_BASE_URL": "https://example.test",
    "COPILOT_OFFLINE": "1",
    "COPILOT_MODEL": "gpt-x",
    "COPILOT_HOME": "/caller/copilot",
    "GH_TOKEN": "gh-real",
    "GITHUB_TOKEN": "ghs-real",
    "COPILOT_GITHUB_TOKEN": "cp-real",
    "GH_ENTERPRISE_TOKEN": "ghe",
    "GH_CONFIG_DIR": "/caller/gh",
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "a.b",
    "GIT_CONFIG_VALUE_0": "1",
}


def test_env_forはbaseの全部の名前を同じ値で返し足すのはcall_envと記録の置き場だけ(tmp_path: Path):
    call = make_call(tmp_path, env={"AUTODEV_GUARD": "x"})
    env = env_for(call, base=BASE)
    for name, value in BASE.items():
        assert env[name] == value
    assert set(env) - set(BASE) == {"AUTODEV_GUARD", "AUTODEV_HOOK_RECORD"}
    assert env["AUTODEV_GUARD"] == "x"
    assert env["AUTODEV_HOOK_RECORD"] == f"{call.log_path}.hooks"
    assert "GIT_TERMINAL_PROMPT" not in env


def test_env_forはbaseにCOPILOT_HOMEが無ければ足さない(tmp_path: Path):
    base = {k: v for k, v in BASE.items() if k != "COPILOT_HOME"}
    assert "COPILOT_HOME" not in env_for(make_call(tmp_path), base=base)


def test_env_forはbaseを省くと呼び出し元の環境を使う(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GH_TOKEN", "gh-real")
    monkeypatch.setenv("COPILOT_HOME", "/caller/copilot")
    env = env_for(make_call(tmp_path))
    assert env["GH_TOKEN"] == "gh-real"
    assert env["COPILOT_HOME"] == "/caller/copilot"


def test_env_forはbaseを書き換えない(tmp_path: Path):
    before = dict(BASE)
    env_for(make_call(tmp_path, env={"AUTODEV_GUARD": "x"}), base=BASE)
    assert before == BASE


# --- write_plugin ---


def hook_commands(directory: Path) -> list[dict[str, Any]]:
    """プラグインの中の JSON から、フックのコマンドを持つ object（timeoutSec 付き）を集める。"""
    found: list[dict[str, Any]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "timeoutSec" in node and any(
                isinstance(v, str) and "autodev.adapters.copilot.hook" in v for v in node.values()
            ):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for path in directory.rglob("*.json"):
        walk(json.loads(path.read_text(encoding="utf-8")))
    return found


def command_of(entry: dict[str, Any]) -> str:
    return next(
        v for v in entry.values() if isinstance(v, str) and "autodev.adapters.copilot.hook" in v
    )


def test_write_pluginはpreToolUseのフック2つを書く(tmp_path: Path):
    directory = write_plugin(tmp_path / "plugin")
    assert directory.is_dir()
    entries = hook_commands(directory)
    commands = [command_of(e) for e in entries]
    assert len(commands) == 2
    for handler in ("deny-writes", "park-on-ask"):
        (matched,) = [c for c in commands if f"-m autodev.adapters.copilot.hook {handler}" in c]
        assert matched.endswith(" || exit 2")
    assert {e["timeoutSec"] for e in entries} == {HOOK_TIMEOUT_SEC} == {600}


def test_write_pluginのフックはpreToolUseの下にある(tmp_path: Path):
    directory = write_plugin(tmp_path / "plugin")
    texts = [p.read_text(encoding="utf-8") for p in directory.rglob("*.json")]
    (hooks,) = [json.loads(t) for t in texts if "autodev.adapters.copilot.hook" in t]
    assert "preToolUse" in json.dumps(hooks)
