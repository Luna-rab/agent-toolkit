"""部品・プリセット・配置・依存を宣言する唯一の場所。install・doctor・list はここから引く。"""

from __future__ import annotations

import argparse
import os
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
    Dependency("uv", True, False, "agent-hud・agent-toolkit の導入"),
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
            Dependency("git", True, False, "create-pr・stacked-pr がブランチを扱う"),
            Dependency("gh", True, True, "create-pr・stacked-pr が GitHub を触る"),
            Dependency("gh-stack", True, False, "stacked PR を作る"),
            Dependency("node", False, False, "archify スキルが使う"),
        ),
    ),
    Component(
        "hud",
        "Claude の statusLine（agent-hud）を配る",
        (Dependency("git", False, False, "ブランチ名を表示する"),),
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


def claude_dir(home: Path) -> Path:
    """Claude Code が読む設定フォルダ。"""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else home / ".claude"


def placements(repo: Path, home: Path, chosen: Selection) -> list[Placement]:
    config = repo / "config"
    claude = claude_dir(home)
    claude_settings = claude / "settings.json"
    table: list[Placement] = []
    if "instructions" in chosen.components:
        agents = config / "shared" / "AGENTS.md"
        table.append(Placement(agents, claude / "CLAUDE.md", "link", "instructions"))
        table.append(Placement(agents, home / ".codex" / "AGENTS.md", "link", "instructions"))
    if "skills" in chosen.components:
        for skill in skill_dirs(config / "shared" / "skills"):
            table.append(
                Placement(skill, home / ".agents" / "skills" / skill.name, "link", "skills")
            )
            table.append(Placement(skill, claude / "skills" / skill.name, "link", "skills"))
        for skill in skill_dirs(config / "claude" / "skills"):
            table.append(Placement(skill, claude / "skills" / skill.name, "link", "skills"))
    if "hud" in chosen.components:
        table.append(
            Placement(config / "claude" / "hud.json", claude_settings, "merge_json", "hud")
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
