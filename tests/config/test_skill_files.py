"""SKILL.md の検査（frontmatter・500 行・相対リンク・scripts の参照）を両方のスキルの置き場に流す。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from conftest import REPO_ROOT

# `pythonpath` に tests/config は無いので、同じディレクトリの補助をファイルから読む
_spec = importlib.util.spec_from_file_location(
    "skill_checks", Path(__file__).with_name("skill_checks.py")
)
assert _spec is not None and _spec.loader is not None
_skill_checks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_skill_checks)
check_skills_root = _skill_checks.check_skills_root

SKILL_ROOTS = [
    REPO_ROOT / "config" / "shared" / "skills",
    REPO_ROOT / "config" / "claude" / "skills",
]


def _skill(root: Path, name: str, text: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


GOOD = "---\nname: good\ndescription: 正しいスキル\n---\n\n本文\n"


@pytest.mark.parametrize("root", SKILL_ROOTS, ids=lambda p: p.parent.name)
def test_実リポジトリのスキルが検査を通る(root: Path):
    failures = check_skills_root(root)
    assert [f.format() for f in failures] == []


def test_実リポジトリのスキルは合わせて6つある():
    names = [p.name for root in SKILL_ROOTS for p in root.iterdir() if (p / "SKILL.md").is_file()]
    assert sorted(names) == [
        "autodev",
        "create-pr",
        "editing-rules",
        "editing-skills",
        "grill-me",
        "stacked-pr",
    ]


def test_正しいスキルは失敗を返さない(tmp_path: Path):
    _skill(tmp_path, "good", GOOD)
    assert check_skills_root(tmp_path) == []


def test_壊れたfrontmatterは失敗になる(tmp_path: Path):
    _skill(tmp_path, "broken", "---\nname: [unclosed\ndescription: x: y: z\n---\n\n本文\n")
    assert len(check_skills_root(tmp_path)) >= 1


def test_frontmatterが無いSKILL_mdは失敗になる(tmp_path: Path):
    _skill(tmp_path, "nofm", "本文だけ\n")
    assert len(check_skills_root(tmp_path)) >= 1


def test_501行のSKILL_mdは失敗になり500行は通る(tmp_path: Path):
    header = "---\nname: long\ndescription: 長い\n---\n"
    _skill(tmp_path, "long", header + "行\n" * (501 - 4))
    _skill(tmp_path, "ok", header + "行\n" * (500 - 4))
    failures = check_skills_root(tmp_path)
    assert failures
    assert all("long" in f.path for f in failures)


def test_存在しないファイルへのリンクは失敗になる(tmp_path: Path):
    _skill(tmp_path, "link", GOOD + "\n[参照](missing.md)\n")
    assert len(check_skills_root(tmp_path)) >= 1


def test_実在するファイルへのリンクは通る(tmp_path: Path):
    skill_dir = _skill(tmp_path, "link", GOOD + "\n[参照](other.md)\n")
    (skill_dir / "other.md").write_text("中身\n", encoding="utf-8")
    assert check_skills_root(tmp_path) == []


def test_実行権限の無いスクリプトの参照は失敗になる(tmp_path: Path):
    skill_dir = _skill(tmp_path, "run", GOOD + "\n`scripts/go.sh` を実行する\n")
    (skill_dir / "scripts").mkdir()
    script = skill_dir / "scripts" / "go.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o644)
    assert len(check_skills_root(tmp_path)) >= 1
    script.chmod(0o755)
    assert check_skills_root(tmp_path) == []


def test_存在しないスクリプトの参照は失敗になる(tmp_path: Path):
    _skill(tmp_path, "run", GOOD + "\n`scripts/none.sh` を実行する\n")
    assert len(check_skills_root(tmp_path)) >= 1


def test_スキルが1つも無いディレクトリは失敗になる(tmp_path: Path):
    assert len(check_skills_root(tmp_path)) >= 1


def test_ドットで始まるものしか無いディレクトリも失敗になる(tmp_path: Path):
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    assert len(check_skills_root(tmp_path)) >= 1


def test_存在しないディレクトリは失敗になる(tmp_path: Path):
    assert len(check_skills_root(tmp_path / "nothing")) >= 1


def test_SKILL_mdの無いスキルのディレクトリは失敗になる(tmp_path: Path):
    (tmp_path / "empty").mkdir()
    assert len(check_skills_root(tmp_path)) >= 1
