"""旧い配置（dist/・test/）が残っていないこと、新しい置き場に移っていること。"""

from __future__ import annotations

import pytest

from conftest import REPO_ROOT, SRC_ROOT

REMOVED = [
    "dist/dot-claude/skills",
    "dist/dot-claude/scripts",
    "dist/dot-claude/hooks/turnreview",
    "dist/dot-claude/settings.json",
    "dist/dot-vscode-server",
    "test",
]


@pytest.mark.parametrize("path", REMOVED)
def test_旧い置き場が無い(path: str):
    assert not (REPO_ROOT / path).exists()


def test_旧いフックの入口のpyが無い():
    hooks = REPO_ROOT / "dist" / "dot-claude" / "hooks"
    assert not hooks.exists() or list(hooks.glob("*.py")) == []


MOVED = [
    "config/shared/skills/stacked-pr/SKILL.md",
    "config/shared/skills/create-pr/SKILL.md",
    "config/shared/skills/grill-me/SKILL.md",
    "config/claude/skills/editing-rules/SKILL.md",
    "config/claude/skills/editing-skills/SKILL.md",
    "config/claude/hud.json",
    "config/presets/personal/claude/settings.json",
]


@pytest.mark.parametrize("path", MOVED)
def test_スキルと設定ファイルがconfigの下にある(path: str):
    assert (REPO_ROOT / path).is_file()


def test_パッケージはsrcの下にあり旧い入口のスクリプトが無い():
    assert sorted(p.name for p in SRC_ROOT.iterdir() if p.is_dir()) == ["hud", "toolkit"]
    for package in ("hud", "toolkit"):
        assert (SRC_ROOT / package / "__init__.py").is_file(), package
    assert not (SRC_ROOT / "hud" / "statusline.py").exists()
