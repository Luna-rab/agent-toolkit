"""`agent-toolkit install --offline` を、HOME を一時ディレクトリにしたサブプロセスで通しで確かめる。

配置元はリポジトリの `config/` の実物。ネットワークには出ない（`--offline`）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.conftest import REPO_ROOT

CONFIG = REPO_ROOT / "config"
SHARED_SKILLS = ["stacked-pr", "create-pr", "grill-me"]
CLAUDE_SKILLS = ["editing-rules", "editing-skills"]
# 分ける前の config/claude/settings.json（main の内容）の写し
MAIN_SETTINGS = json.loads(
    (Path(__file__).parent / "data" / "claude-settings-main.json").read_text(encoding="utf-8")
)
PRESET_KEYS = ["model", "permissions", "env", "tui", "extraKnownMarketplaces", "enabledPlugins"]

ENTRY = "import sys; from toolkit.cli import main; sys.exit(main(sys.argv[1:]))"


@pytest.fixture
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def agent_toolkit(
    home: Path, *args: str, claude_config_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    env["HOME"] = str(home)
    if claude_config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    return subprocess.run(
        [sys.executable, "-c", ENTRY, *args],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def install(home: Path) -> subprocess.CompletedProcess[str]:
    return agent_toolkit(home, "install", "--offline")


def snapshot(home: Path) -> dict[str, tuple]:
    """ホームの下の全ファイル・全 symlink（行き先つき）・全ディレクトリ。"""
    state: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(home):
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            rel = str(path.relative_to(home))
            if path.is_symlink():
                state[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                state[rel] = ("dir",)
            else:
                state[rel] = ("file", path.read_bytes())
    return state


def link_target(path: Path) -> Path:
    assert path.is_symlink(), f"{path} は symlink ではない"
    return path.resolve()


def backups(home: Path) -> list[Path]:
    root = home / ".dotbackup"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_symlinks_shared_agents_md_for_claude_and_codex(home):
    result = install(home)

    assert result.returncode == 0, result.stderr
    source = (CONFIG / "shared" / "AGENTS.md").resolve()
    assert link_target(home / ".claude" / "CLAUDE.md") == source
    assert link_target(home / ".codex" / "AGENTS.md") == source


def test_links_each_skill_individually(home):
    result = install(home)

    assert result.returncode == 0, result.stderr
    for skills_dir in (home / ".claude" / "skills", home / ".agents" / "skills"):
        assert skills_dir.is_dir() and not skills_dir.is_symlink()
    for name in SHARED_SKILLS:
        source = (CONFIG / "shared" / "skills" / name).resolve()
        assert link_target(home / ".agents" / "skills" / name) == source
        assert link_target(home / ".claude" / "skills" / name) == source
    for name in CLAUDE_SKILLS:
        source = (CONFIG / "claude" / "skills" / name).resolve()
        assert link_target(home / ".claude" / "skills" / name) == source
        assert not (home / ".agents" / "skills" / name).exists()
        assert not (home / ".agents" / "skills" / name).is_symlink()


def test_default_install_writes_status_line_and_preset(home):
    result = install(home)

    assert result.returncode == 0, result.stderr
    assert json.loads((home / ".claude" / "settings.json").read_text()) == MAIN_SETTINGS


def test_merges_into_existing_settings_json(home):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"theme": "dark", "env": {"MY_VAR": "1"}, "model": "sonnet"}))

    result = install(home)

    assert result.returncode == 0, result.stderr
    merged = json.loads(path.read_text())
    assert merged["theme"] == "dark"
    assert merged["env"]["MY_VAR"] == "1"
    for key, value in MAIN_SETTINGS["env"].items():
        assert merged["env"][key] == value
    assert merged["model"] == MAIN_SETTINGS["model"]


def test_preset_none_writes_only_status_line(home):
    result = agent_toolkit(home, "install", "--offline", "--preset", "none")

    assert result.returncode == 0, result.stderr
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["statusLine"] == MAIN_SETTINGS["statusLine"]
    assert "hooks" not in settings
    for key in PRESET_KEYS:
        assert key not in settings


def test_preset_none_keeps_preset_keys_already_in_settings(home):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir()
    mine = {
        "model": "sonnet",
        "permissions": {"defaultMode": "plan"},
        "enabledPlugins": {"x@y": True},
    }
    path.write_text(json.dumps(mine))

    result = agent_toolkit(home, "install", "--offline", "--preset", "none")

    assert result.returncode == 0, result.stderr
    settings = json.loads(path.read_text())
    for key, value in mine.items():
        assert settings[key] == value


def test_only_skills_links_skills_and_writes_just_the_preset(home):
    result = agent_toolkit(home, "install", "--offline", "--only", "skills")

    assert result.returncode == 0, result.stderr
    source = (CONFIG / "shared" / "skills" / "create-pr").resolve()
    assert link_target(home / ".agents" / "skills" / "create-pr") == source
    assert link_target(home / ".claude" / "skills" / "create-pr") == source
    assert not (home / ".claude" / "CLAUDE.md").exists()
    assert not (home / ".claude" / "CLAUDE.md").is_symlink()
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    for key in PRESET_KEYS:
        assert settings[key] == MAIN_SETTINGS[key]
    assert "statusLine" not in settings


def test_skip_hud_with_preset_none_writes_no_settings(home):
    result = agent_toolkit(home, "install", "--offline", "--skip", "hud", "--preset", "none")

    assert result.returncode == 0, result.stderr
    assert not (home / ".claude" / "settings.json").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--only", "skills", "--skip", "hud"],
        ["--only", "nope"],
        ["--preset", "nope"],
    ],
)
def test_bad_selection_exits_two(home, args):
    result = agent_toolkit(home, "install", "--offline", *args)

    assert result.returncode == 2
    assert not (home / ".claude").exists()


def test_a_failing_step_does_not_stop_the_others_and_exits_twenty(home):
    (home / ".codex").write_text("not a directory\n")

    result = install(home)

    assert result.returncode == 20
    assert result.stderr.strip() != ""
    assert (
        link_target(home / ".claude" / "CLAUDE.md") == (CONFIG / "shared" / "AGENTS.md").resolve()
    )
    assert json.loads((home / ".claude" / "settings.json").read_text()) == MAIN_SETTINGS


def test_a_claude_file_fails_only_the_claude_steps(home):
    (home / ".claude").write_text("not a directory\n")

    result = install(home)

    assert result.returncode == 20
    assert result.stderr.strip() != ""
    assert link_target(home / ".codex" / "AGENTS.md") == (CONFIG / "shared" / "AGENTS.md").resolve()
    source = (CONFIG / "shared" / "skills" / "create-pr").resolve()
    assert link_target(home / ".agents" / "skills" / "create-pr") == source


def test_one_install_adds_one_settings_backup(home):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"theme": "dark"}))

    result = install(home)

    assert result.returncode == 0, result.stderr
    saved = [p for p in backups(home) if p.name.startswith("settings.json.")]
    assert len(saved) == 1
    assert json.loads(saved[0].read_text()) == {"theme": "dark"}


def test_leaves_unreadable_json_untouched_and_warns(home):
    path = home / ".codex" / "hooks.json"
    path.parent.mkdir()
    original = '{\n  // x\n  "hooks": {}\n}\n'
    path.write_text(original)

    result = install(home)

    assert result.returncode == 0
    assert path.read_text() == original
    assert result.stderr.strip() != ""


@pytest.mark.parametrize(
    "extra", [[], ["--preset", "none"], ["--only", "skills"]], ids=["default", "none", "skills"]
)
def test_second_run_changes_nothing(home, extra):
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"theme": "dark"}))
    (home / ".claude" / "CLAUDE.md").write_text("mine\n")
    first = agent_toolkit(home, "install", "--offline", *extra)
    assert first.returncode == 0, first.stderr
    before = snapshot(home)

    second = agent_toolkit(home, "install", "--offline", *extra)

    assert second.returncode == 0, second.stderr
    assert snapshot(home) == before


def test_keeps_third_party_skills_and_removes_only_dangling_managed_links(home):
    claude_skills = home / ".claude" / "skills"
    agents_skills = home / ".agents" / "skills"
    (claude_skills / "thirdparty").mkdir(parents=True)
    (claude_skills / "thirdparty" / "SKILL.md").write_text("third\n")
    (agents_skills / "other").mkdir(parents=True)
    (claude_skills / "gone").symlink_to(CONFIG / "shared" / "skills" / "gone")
    (claude_skills / "foreign").symlink_to("/elsewhere/missing")

    result = install(home)

    assert result.returncode == 0, result.stderr
    assert (claude_skills / "thirdparty" / "SKILL.md").read_text() == "third\n"
    assert (agents_skills / "other").is_dir()
    assert not (claude_skills / "gone").is_symlink()
    assert not (claude_skills / "gone").exists()
    assert (claude_skills / "foreign").is_symlink()
    assert os.readlink(claude_skills / "foreign") == "/elsewhere/missing"


def test_removes_dangling_links_into_old_dist_skills(home):
    claude_skills = home / ".claude" / "skills"
    claude_skills.mkdir(parents=True)
    old = "/x/dotfiles/dist/dot-claude/skills/old-skill"
    (claude_skills / "old-skill").symlink_to(old)

    result = install(home)

    assert result.returncode == 0, result.stderr
    assert not (claude_skills / "old-skill").is_symlink()


def test_backs_up_a_real_file_before_linking_and_replaces_a_skills_symlink(home):
    claude = home / ".claude"
    claude.mkdir()
    (claude / "CLAUDE.md").write_text("my notes\n")
    elsewhere = home / "elsewhere"
    elsewhere.mkdir()
    (claude / "skills").symlink_to(elsewhere)

    result = install(home)

    assert result.returncode == 0, result.stderr
    assert link_target(claude / "CLAUDE.md") == (CONFIG / "shared" / "AGENTS.md").resolve()
    assert any(
        p.name.startswith("CLAUDE.md.") and p.read_text() == "my notes\n" for p in backups(home)
    )
    assert (claude / "skills").is_dir() and not (claude / "skills").is_symlink()
    assert list(elsewhere.iterdir()) == []


def test_removes_old_dist_links_but_keeps_other_links_and_directories(home):
    claude = home / ".claude"
    claude.mkdir()
    (claude / "rules").symlink_to("/x/dotfiles/dist/dot-claude/rules")
    (claude / "hooks").symlink_to("/somewhere/else/hooks")
    (claude / "scripts").mkdir()
    (claude / "scripts" / "mine.sh").write_text("echo\n")

    result = install(home)

    assert result.returncode == 0, result.stderr
    assert not (claude / "rules").is_symlink()
    assert os.readlink(claude / "hooks") == "/somewhere/else/hooks"
    assert (claude / "scripts" / "mine.sh").read_text() == "echo\n"


def test_places_claude_files_under_claude_config_dir(home, tmp_path):
    claude = tmp_path / "claude-config"
    (claude / "skills").mkdir(parents=True)
    (claude / "skills" / "autodev").symlink_to("/x/dotfiles/dist/dot-claude/skills/autodev")
    (claude / "rules").symlink_to("/x/dotfiles/dist/dot-claude/rules")

    result = agent_toolkit(home, "install", "--offline", claude_config_dir=claude)

    assert result.returncode == 0, result.stderr
    assert link_target(claude / "CLAUDE.md") == (CONFIG / "shared" / "AGENTS.md").resolve()
    for name in SHARED_SKILLS:
        source = (CONFIG / "shared" / "skills" / name).resolve()
        assert link_target(claude / "skills" / name) == source
    for name in CLAUDE_SKILLS:
        source = (CONFIG / "claude" / "skills" / name).resolve()
        assert link_target(claude / "skills" / name) == source
    assert json.loads((claude / "settings.json").read_text()) == MAIN_SETTINGS
    assert not (claude / "rules").is_symlink()
    assert not (home / ".claude").exists()


def test_links_existing_archify_even_offline(home):
    archify = home / ".agents" / "skills" / "archify"
    archify.mkdir(parents=True)
    (archify / "SKILL.md").write_text("archify\n")

    result = install(home)

    assert result.returncode == 0, result.stderr
    assert link_target(home / ".claude" / "skills" / "archify") == archify.resolve()


def test_skip_extras_does_not_link_archify(home):
    archify = home / ".agents" / "skills" / "archify"
    archify.mkdir(parents=True)
    (archify / "SKILL.md").write_text("archify\n")

    result = agent_toolkit(home, "install", "--offline", "--skip", "extras")

    assert result.returncode == 0, result.stderr
    assert not (home / ".claude" / "skills" / "archify").is_symlink()
    assert not (home / ".claude" / "skills" / "archify").exists()


def test_backs_up_a_real_archify_directory_in_claude_skills(home):
    archify = home / ".agents" / "skills" / "archify"
    archify.mkdir(parents=True)
    (archify / "SKILL.md").write_text("archify\n")
    real = home / ".claude" / "skills" / "archify"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("old copy\n")

    result = install(home)

    assert result.returncode == 0, result.stderr
    assert link_target(real) == archify.resolve()
    assert any(p.read_text() == "old copy\n" for p in backups(home))


def test_install_help_exits_zero(home):
    result = agent_toolkit(home, "install", "--help")

    assert result.returncode == 0, result.stderr
    assert "--offline" in result.stdout


# --- 以前の agent-toolkit が入れたフックの片付け ---


def hook_commands(path: Path) -> dict[str, list[str]]:
    events = json.loads(path.read_text()).get("hooks", {})
    return {
        event: [hook["command"] for group in groups for hook in group["hooks"]]
        for event, groups in events.items()
    }


def hook_group(command: str) -> dict:
    return {"hooks": [{"type": "command", "command": command}]}


@pytest.mark.parametrize("extra", [[], ["--only", "instructions"]], ids=["default", "only"])
def test_removes_old_claude_hooks_and_keeps_user_ones(home, extra):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir()
    old = {
        "Stop": [
            hook_group("my-stop.sh"),
            hook_group("agent-toolkit hook stop --agent claude"),
            hook_group("/home/u/.claude/hooks/check-x.sh"),
        ],
        "SubagentStop": [hook_group("agent-toolkit hook subagent-stop --agent claude")],
    }
    path.write_text(json.dumps({"theme": "dark", "hooks": old}))

    result = agent_toolkit(home, "install", "--offline", *extra)

    assert result.returncode == 0, result.stderr
    assert hook_commands(path) == {"Stop": ["my-stop.sh"]}
    assert json.loads(path.read_text())["theme"] == "dark"


def test_removes_old_codex_hooks_and_keeps_user_ones(home):
    path = home / ".codex" / "hooks.json"
    path.parent.mkdir()
    old = {
        "PreToolUse": [
            hook_group("my-pre-tool.sh"),
            hook_group('[ -z "$AUTODEV_GUARD" ] || autodev hook deny-writes || exit 2'),
            hook_group('[ -z "$AUTODEV_GUARD" ] || autodev hook park-on-ask || exit 2'),
        ],
        "Stop": [hook_group("agent-toolkit hook stop --agent codex")],
    }
    path.write_text(json.dumps({"hooks": old}))

    result = install(home)

    assert result.returncode == 0, result.stderr
    assert hook_commands(path) == {"PreToolUse": ["my-pre-tool.sh"]}
    assert any(p.name.startswith("hooks.json.") for p in backups(home))


def test_does_not_create_codex_hooks_json(home):
    assert install(home).returncode == 0
    assert not (home / ".codex" / "hooks.json").exists()
