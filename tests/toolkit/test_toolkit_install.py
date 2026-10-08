"""`agent-toolkit install --offline` を、HOME を一時ディレクトリにしたサブプロセスで通しで確かめる。

配置元はリポジトリの `config/` の実物。ネットワークには出ない（`--offline`）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from tests.conftest import REPO_ROOT

from toolkit.catalog import COMPONENTS

CONFIG = REPO_ROOT / "config"
SHARED_SKILLS = ["stacked-pr", "autodev", "create-pr", "grill-me"]
CLAUDE_SKILLS = ["editing-rules", "editing-skills"]
# 分ける前の config/claude/settings.json（main の内容）の写し
MAIN_SETTINGS = json.loads(
    (Path(__file__).parent / "data" / "claude-settings-main.json").read_text(encoding="utf-8")
)
PRESET_KEYS = ["model", "permissions", "env", "tui", "extraKnownMarketplaces", "enabledPlugins"]


def machine_settings(home: Path) -> Path:
    return home / ".vscode-server" / "data" / "Machine" / "settings.json"


ENTRY = "import sys; from toolkit.cli import main; sys.exit(main(sys.argv[1:]))"


@pytest.fixture
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def agent_toolkit(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home)}
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


def test_default_install_equals_settings_before_the_split(home):
    result = install(home)

    assert result.returncode == 0, result.stderr
    assert json.loads((home / ".claude" / "settings.json").read_text()) == MAIN_SETTINGS
    codex = json.loads((CONFIG / "codex" / "hooks.json").read_text())
    assert json.loads((home / ".codex" / "hooks.json").read_text()) == codex


def test_vscode_settings_only_when_vscode_server_exists(home):
    result = install(home)

    assert result.returncode == 0, result.stderr
    assert not (home / ".vscode-server").exists()

    (home / ".vscode-server").mkdir()
    result = install(home)

    assert result.returncode == 0, result.stderr
    vscode = json.loads((CONFIG / "vscode" / "settings.json").read_text())
    assert json.loads(machine_settings(home).read_text()) == vscode


def test_skip_vscode_leaves_vscode_server_alone(home):
    (home / ".vscode-server").mkdir()

    result = agent_toolkit(home, "install", "--offline", "--skip", "vscode")

    assert result.returncode == 0, result.stderr
    assert not machine_settings(home).exists()
    assert (home / ".claude" / "settings.json").is_file()


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


def test_preset_none_writes_only_hooks_and_status_line(home):
    result = agent_toolkit(home, "install", "--offline", "--preset", "none")

    assert result.returncode == 0, result.stderr
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["hooks"] == MAIN_SETTINGS["hooks"]
    assert settings["statusLine"] == MAIN_SETTINGS["statusLine"]
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
    assert settings["hooks"] == MAIN_SETTINGS["hooks"]


def test_keeps_user_hook_groups_and_drops_old_managed_ones(home):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir()
    user = {"hooks": [{"type": "command", "command": "my-stop.sh"}]}
    old = {"hooks": [{"type": "command", "command": "/home/u/.claude/hooks/check-x.sh"}]}
    path.write_text(json.dumps({"hooks": {"Stop": [user, old]}}))

    result = install(home)

    assert result.returncode == 0, result.stderr
    commands = [
        h["command"] for g in json.loads(path.read_text())["hooks"]["Stop"] for h in g["hooks"]
    ]
    assert "my-stop.sh" in commands
    assert "/home/u/.claude/hooks/check-x.sh" not in commands
    assert commands.count("agent-toolkit hook stop --agent claude") == 1


def test_only_skills_links_skills_and_writes_just_the_preset(home):
    result = agent_toolkit(home, "install", "--offline", "--only", "skills")

    assert result.returncode == 0, result.stderr
    source = (CONFIG / "shared" / "skills" / "autodev").resolve()
    assert link_target(home / ".agents" / "skills" / "autodev") == source
    assert link_target(home / ".claude" / "skills" / "autodev") == source
    assert not (home / ".claude" / "CLAUDE.md").exists()
    assert not (home / ".claude" / "CLAUDE.md").is_symlink()
    assert not (home / ".codex" / "hooks.json").exists()
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    for key in PRESET_KEYS:
        assert settings[key] == MAIN_SETTINGS[key]
    assert "hooks" not in settings
    assert "statusLine" not in settings


def test_skip_hooks_and_hud_with_preset_none_writes_no_settings(home):
    result = agent_toolkit(
        home, "install", "--offline", "--skip", "hooks", "hud", "--preset", "none"
    )

    assert result.returncode == 0, result.stderr
    assert not (home / ".claude" / "settings.json").exists()
    assert not (home / ".codex" / "hooks.json").exists()


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
    codex = json.loads((CONFIG / "codex" / "hooks.json").read_text())
    assert json.loads((home / ".codex" / "hooks.json").read_text()) == codex
    source = (CONFIG / "shared" / "skills" / "autodev").resolve()
    assert link_target(home / ".agents" / "skills" / "autodev") == source


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
    (home / ".vscode-server").mkdir()
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


# --- Codex のガードのフック ---

DENY_WRITES_COMMAND = '[ -z "$AUTODEV_GUARD" ] || autodev hook deny-writes || exit 2'
PARK_ON_ASK_COMMAND = '[ -z "$AUTODEV_GUARD" ] || autodev hook park-on-ask || exit 2'
NO_AUTODEV_PATH = "/usr/bin:/bin"


def codex_pre_tool_commands(home: Path) -> list[str]:
    config = json.loads((home / ".codex" / "hooks.json").read_text())
    return [
        hook["command"]
        for group in config["hooks"]["PreToolUse"]
        for hook in group["hooks"]
        if isinstance(hook.get("command"), str)
    ]


def run_registered(command: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", command],
        input="{}",
        capture_output=True,
        text=True,
        env={"PATH": NO_AUTODEV_PATH, **env},
        check=False,
    )


def test_installs_the_codex_guard_hooks_into_pre_tool_use(home):
    assert install(home).returncode == 0
    commands = codex_pre_tool_commands(home)
    assert commands.count(DENY_WRITES_COMMAND) == 1
    assert commands.count(PARK_ON_ASK_COMMAND) == 1


def test_codex_guard_hooks_do_not_pile_up_when_installed_twice(home):
    assert install(home).returncode == 0
    first = codex_pre_tool_commands(home)
    assert install(home).returncode == 0
    assert codex_pre_tool_commands(home) == first


def test_codex_guard_hooks_keep_user_hooks_and_replace_only_ours(home):
    codex = home / ".codex"
    codex.mkdir()
    mine = {"hooks": [{"type": "command", "command": "my-pre-tool.sh"}]}
    (codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [mine]}}))
    assert install(home).returncode == 0
    assert install(home).returncode == 0
    commands = codex_pre_tool_commands(home)
    assert commands.count("my-pre-tool.sh") == 1
    assert commands.count(DENY_WRITES_COMMAND) == 1
    assert commands.count(PARK_ON_ASK_COMMAND) == 1


@pytest.mark.parametrize("command", [DENY_WRITES_COMMAND, PARK_ON_ASK_COMMAND])
def test_registered_codex_hook_stops_when_guarded_and_autodev_is_missing(home, command):
    assert install(home).returncode == 0
    assert command in codex_pre_tool_commands(home)
    assert shutil.which("autodev", path=NO_AUTODEV_PATH) is None
    guarded = run_registered(command, {"AUTODEV_GUARD": "{}"})
    assert guarded.returncode == 2


@pytest.mark.parametrize("command", [DENY_WRITES_COMMAND, PARK_ON_ASK_COMMAND])
def test_registered_codex_hook_lets_everything_through_without_the_guard(home, command):
    assert install(home).returncode == 0
    assert command in codex_pre_tool_commands(home)
    assert shutil.which("autodev", path=NO_AUTODEV_PATH) is None
    assert run_registered(command, {}).returncode == 0


def test_catalog_lists_codex_as_an_optional_dependency_of_skills():
    skills = next(c for c in COMPONENTS if c.name == "skills")
    codex = [d for d in skills.dependencies if d.name == "codex"]
    assert len(codex) == 1
    assert codex[0].required is False
    assert "autodev" in codex[0].purpose
    assert "models.json" in codex[0].purpose


def test_catalog_lists_copilot_as_an_optional_dependency_of_skills():
    skills = next(c for c in COMPONENTS if c.name == "skills")
    copilot = [d for d in skills.dependencies if d.name == "copilot"]
    assert len(copilot) == 1
    assert copilot[0].required is False
    assert "autodev" in copilot[0].purpose
    assert "models.json" in copilot[0].purpose
