"""config/ の設定ファイルとエージェント定義が、コマンド名で書かれ、置き場が分かれていること。"""

from __future__ import annotations

import json
import sys

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from conftest import REPO_ROOT, SRC_ROOT

CONFIG = REPO_ROOT / "config"


def _commands(hooks: dict, event: str) -> list[str]:
    return [h["command"] for group in hooks[event] for h in group["hooks"]]


def _all_commands(hooks: dict) -> list[str]:
    return [c for event in hooks for c in _commands(hooks, event)]


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


PRESET = CONFIG / "presets" / "personal" / "claude" / "settings.json"


def test_claudeのhooksはフックをコマンド名で登録する():
    hooks = _load(CONFIG / "claude" / "hooks.json")["hooks"]
    assert set(hooks) == {"Stop", "SubagentStop"}
    assert _commands(hooks, "Stop") == ["agent-toolkit hook stop --agent claude"]
    assert _commands(hooks, "SubagentStop") == ["agent-toolkit hook subagent-stop --agent claude"]


def test_claudeのhudのstatuslineはコマンド名():
    settings = _load(CONFIG / "claude" / "hud.json")
    assert settings["statusLine"]["command"] == "agent-hud statusline"


def test_claudeのコマンドにパスが無い():
    hooks = _load(CONFIG / "claude" / "hooks.json")["hooks"]
    hud = _load(CONFIG / "claude" / "hud.json")
    commands = [*_all_commands(hooks), hud["statusLine"]["command"]]
    assert commands
    assert all("/" not in c for c in commands)


def test_claudeの設定は機能ごとのファイルと個人プリセットに分かれる():
    assert not (CONFIG / "claude" / "settings.json").exists()
    assert set(_load(CONFIG / "claude" / "hooks.json")) <= {"$schema", "hooks"}
    assert set(_load(CONFIG / "claude" / "hud.json")) <= {"$schema", "statusLine"}
    assert set(_load(PRESET)) == {
        "$schema",
        "model",
        "permissions",
        "env",
        "tui",
        "extraKnownMarketplaces",
        "enabledPlugins",
    }


def test_codexのhooksはStopとPreToolUse():
    hooks = _load(CONFIG / "codex" / "hooks.json")["hooks"]
    assert set(hooks) == {"Stop", "PreToolUse"}
    assert _commands(hooks, "Stop") == ["agent-toolkit hook stop --agent codex"]
    assert _commands(hooks, "PreToolUse") == [
        '[ -z "$AUTODEV_GUARD" ] || autodev hook deny-writes || exit 2',
        '[ -z "$AUTODEV_GUARD" ] || autodev hook park-on-ask || exit 2',
    ]


def test_vscodeのautodev_watchはagent_hudを実行する():
    settings = _load(CONFIG / "vscode" / "settings.json")
    profile = settings["terminal.integrated.profiles.linux"]["autodev watch"]
    joined = " ".join(profile["args"])
    assert "agent-hud watch" in joined
    assert "~/.claude/scripts" not in joined


@pytest.mark.parametrize("name", ["medium-worker", "medium-reviewer"])
def test_codexのエージェント定義が読める(name: str):
    path = REPO_ROOT / ".codex" / "agents" / f"{name}.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for key in ("name", "description", "developer_instructions"):
        assert isinstance(data[key], str) and data[key].strip(), key
    assert data["model_reasoning_effort"] == "medium"
    assert data["name"] == path.stem


def test_configにpyが無い():
    assert list(CONFIG.rglob("*.py")) == []


@pytest.mark.parametrize("name", ["SKILL.md", "settings.json", "hooks.json", "AGENTS.md"])
def test_srcに設定ファイルが無い(name: str):
    assert list(SRC_ROOT.rglob(name)) == []


def test_configにAGENTS_mdがある():
    assert (CONFIG / "shared" / "AGENTS.md").is_file()


def test_distと旧い検査スクリプトが無い():
    assert not (REPO_ROOT / "dist").exists()
    assert not (REPO_ROOT / ".claude" / "scripts" / "check-skills.py").exists()
