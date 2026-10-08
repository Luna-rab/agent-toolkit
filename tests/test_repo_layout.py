"""旧い配置（dist/・test/）が残っていないこと、新しい置き場に移っていること。"""

from __future__ import annotations

import pytest

from conftest import AUTODEV_PACKAGE, AUTODEV_SKILL, REPO_ROOT, SRC_ROOT

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
    "config/shared/skills/autodev/SKILL.md",
    "config/shared/skills/create-pr/SKILL.md",
    "config/shared/skills/grill-me/SKILL.md",
    "config/claude/skills/editing-rules/SKILL.md",
    "config/claude/skills/editing-skills/SKILL.md",
    "config/claude/hooks.json",
    "config/claude/hud.json",
    "config/presets/personal/claude/settings.json",
    "config/vscode/settings.json",
]


@pytest.mark.parametrize("path", MOVED)
def test_スキルと設定ファイルがconfigの下にある(path: str):
    assert (REPO_ROOT / path).is_file()


def test_autodevのスキルにコードを置かない():
    assert (AUTODEV_SKILL / "README.md").is_file()
    assert list(AUTODEV_SKILL.rglob("*.py")) == []
    assert not (AUTODEV_SKILL / "scripts").exists()


def test_パッケージはsrcの下にあり旧い入口のスクリプトが無い():
    assert (AUTODEV_PACKAGE / "__main__.py").is_file()
    for package in ("autodev", "hud", "turnreview"):
        assert (SRC_ROOT / package / "__init__.py").is_file(), package
    assert not (SRC_ROOT / "hud" / "statusline.py").exists()
    assert not (SRC_ROOT / "turnreview" / "review-new-comments.py").exists()
    assert not list(SRC_ROOT.rglob("autodev-watch.py"))
    assert not list(SRC_ROOT.rglob("review-new-*.py"))
