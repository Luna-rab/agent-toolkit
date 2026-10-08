"""部品・プリセット・配置・依存を宣言する唯一の場所。install・doctor・list はここから引く。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Dependency:
    name: str
    required: bool
    auth: bool
    purpose: str


@dataclass(frozen=True)
class Component:
    name: str
    description: str
    dependencies: tuple[Dependency, ...]


@dataclass(frozen=True)
class Preset:
    name: str
    description: str
    dependencies: tuple[Dependency, ...]


@dataclass(frozen=True)
class Placement:
    source: Path
    target: Path
    method: Literal["link", "merge_json"]
    component: str  # 部品名。プリセットの配置は "preset"


@dataclass(frozen=True)
class Selection:
    components: frozenset[str]
    preset: str


CLI_DEPENDENCIES: tuple[Dependency, ...] = (
    Dependency("uv", True, False, "autodev・agent-hud・agent-toolkit の導入"),
)

COMPONENTS: tuple[Component, ...] = (
    Component(
        "instructions",
        "共通の指示（AGENTS.md）を Claude と Codex へ配る",
        (),
    ),
    Component(
        "skills",
        "スキルの symlink を張り、行き先の無いリンクを片付ける",
        (
            Dependency("git", True, False, "autodev が worktree とブランチを扱う"),
            Dependency("claude", True, True, "autodev が Claude Code を呼ぶ"),
            Dependency(
                "codex",
                False,
                True,
                "autodev が Codex CLI を呼ぶ（models.json で codex を選んだときだけ）",
            ),
            Dependency(
                "copilot",
                False,
                False,
                "autodev が Copilot CLI を呼ぶ（models.json で copilot を選んだときだけ）",
            ),
            Dependency("gh", True, True, "autodev が GitHub を触る"),
            Dependency("gh-stack", True, False, "stacked PR を作る"),
            Dependency("node", False, False, "archify スキルが使う"),
        ),
    ),
    Component(
        "hooks",
        "Claude と Codex のフックを配る",
        (Dependency("codex", False, True, "Codex のフックが動く"),),
    ),
    Component(
        "hud",
        "Claude の statusLine（agent-hud）を配る",
        (Dependency("git", False, False, "ブランチ名を表示する"),),
    ),
    Component(
        "vscode",
        "VS Code の設定を配る（~/.vscode-server があるときだけ）",
        (),
    ),
    Component(
        "extras",
        "外部から取得するもの（gh-stack 拡張、archify スキル）",
        (),
    ),
)

PRESETS: tuple[Preset, ...] = (
    Preset(
        "personal",
        "モデル・権限・環境変数・プラグインの個人設定を ~/.claude/settings.json へ足す",
        (),
    ),
    Preset("none", "個人設定を足さない", ()),
)


def add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    names = [component.name for component in COMPONENTS]
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--only", nargs="+", choices=names, metavar="COMPONENT", help="選んだ部品だけ行う"
    )
    group.add_argument(
        "--skip", nargs="+", choices=names, metavar="COMPONENT", help="選んだ部品を除く"
    )
    parser.add_argument(
        "--preset",
        choices=[preset.name for preset in PRESETS],
        default="personal",
        help="個人プリセット（既定 personal）",
    )


def selection(args: argparse.Namespace) -> Selection:
    names = [component.name for component in COMPONENTS]
    if args.only:
        chosen = frozenset(args.only)
    elif args.skip:
        chosen = frozenset(names) - frozenset(args.skip)
    else:
        chosen = frozenset(names)
    return Selection(chosen, args.preset)


def skill_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def placements(repo: Path, home: Path, chosen: Selection) -> list[Placement]:
    config = repo / "config"
    claude_settings = home / ".claude" / "settings.json"
    table: list[Placement] = []
    if "instructions" in chosen.components:
        agents = config / "shared" / "AGENTS.md"
        table.append(Placement(agents, home / ".claude" / "CLAUDE.md", "link", "instructions"))
        table.append(Placement(agents, home / ".codex" / "AGENTS.md", "link", "instructions"))
    if "skills" in chosen.components:
        for skill in skill_dirs(config / "shared" / "skills"):
            table.append(
                Placement(skill, home / ".agents" / "skills" / skill.name, "link", "skills")
            )
            table.append(
                Placement(skill, home / ".claude" / "skills" / skill.name, "link", "skills")
            )
        for skill in skill_dirs(config / "claude" / "skills"):
            table.append(
                Placement(skill, home / ".claude" / "skills" / skill.name, "link", "skills")
            )
    if "hooks" in chosen.components:
        table.append(
            Placement(config / "claude" / "hooks.json", claude_settings, "merge_json", "hooks")
        )
        table.append(
            Placement(
                config / "codex" / "hooks.json",
                home / ".codex" / "hooks.json",
                "merge_json",
                "hooks",
            )
        )
    if "hud" in chosen.components:
        table.append(
            Placement(config / "claude" / "hud.json", claude_settings, "merge_json", "hud")
        )
    if "vscode" in chosen.components and (home / ".vscode-server").is_dir():
        table.append(
            Placement(
                config / "vscode" / "settings.json",
                home / ".vscode-server" / "data" / "Machine" / "settings.json",
                "merge_json",
                "vscode",
            )
        )
    if chosen.preset == "personal":
        table.append(
            Placement(
                config / "presets" / "personal" / "claude" / "settings.json",
                claude_settings,
                "merge_json",
                "preset",
            )
        )
    return table
