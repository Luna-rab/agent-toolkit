"""`agent-toolkit doctor` と `agent-toolkit list` を、偽物の CLI だけを置いた PATH で確かめる。

実機の gh・claude・uv には触れない。HOME は一時ディレクトリ。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from toolkit import doctor
from toolkit.catalog import Selection

ENTRY = "import sys; from toolkit.cli import main; sys.exit(main(sys.argv[1:]))"
COMPONENT_ORDER = ["instructions", "skills", "hooks", "hud", "vscode", "extras"]


@pytest.fixture
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def bin_dir(tmp_path) -> Path:
    path = tmp_path / "bin"
    path.mkdir()
    return path


def fake(bin_dir: Path, name: str, body: str) -> Path:
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def fake_gh(bin_dir: Path, *, auth_exit: int = 0, extensions: str = "gh stack\tgithub/gh-stack\n"):
    fake(
        bin_dir,
        "gh",
        f"""echo "$GH_PROMPT_DISABLED" > "{bin_dir}/gh-prompt"
case "$1 $2" in
  "auth status") exit {auth_exit} ;;
  "extension list") printf '%s' '{extensions}'; exit 0 ;;
esac
exit 0""",
    )


def full_path(bin_dir: Path) -> None:
    """必須の依存がすべて揃い、認証も通る PATH。"""
    fake(bin_dir, "uv", "exit 0")
    fake(bin_dir, "git", "exit 0")
    fake(bin_dir, "claude", "exit 0")
    fake_gh(bin_dir)


def doctor_cli(home: Path, bin_dir: Path, *args: str, extra_env: dict | None = None):
    env = {k: v for k, v in os.environ.items() if k not in ("UV", "PATH")}
    env.update({"HOME": str(home), "PATH": str(bin_dir), **(extra_env or {})})
    return subprocess.run(
        [sys.executable, "-c", ENTRY, *args],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def checks_of(done: subprocess.CompletedProcess[str]) -> dict[str, dict]:
    return {c["name"]: c for c in json.loads(done.stdout)["checks"]}


def test_all_present_returns_0(home, bin_dir):
    full_path(bin_dir)
    done = doctor_cli(home, bin_dir, "doctor", "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["status"] == 0


def test_missing_gh_returns_30(home, bin_dir):
    full_path(bin_dir)
    (bin_dir / "gh").unlink()
    done = doctor_cli(home, bin_dir, "doctor", "--json")
    assert done.returncode == 30, done.stderr
    gh = checks_of(done)["gh"]
    assert gh["installed"] is False
    assert gh["required"] is True


def test_missing_gh_stack_extension_returns_30(home, bin_dir):
    full_path(bin_dir)
    fake_gh(bin_dir, extensions="gh other\tsomeone/gh-other\n")
    done = doctor_cli(home, bin_dir, "doctor", "--json")
    assert done.returncode == 30, done.stderr


def test_unauthenticated_gh_returns_31(home, bin_dir):
    full_path(bin_dir)
    fake_gh(bin_dir, auth_exit=1)
    done = doctor_cli(home, bin_dir, "doctor", "--json")
    assert done.returncode == 31, done.stderr
    assert checks_of(done)["gh"]["authenticated"] is False


def test_skip_skills_does_not_require_gh(home, bin_dir):
    full_path(bin_dir)
    (bin_dir / "gh").unlink()
    done = doctor_cli(home, bin_dir, "doctor", "--skip", "skills", "--json")
    assert done.returncode == 0, done.stderr


def test_uv_found_through_UV_variable(home, bin_dir, tmp_path):
    full_path(bin_dir)
    (bin_dir / "uv").unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    uv = fake(elsewhere, "uv", "exit 0")
    done = doctor_cli(home, bin_dir, "doctor", "--json", extra_env={"UV": str(uv)})
    assert done.returncode == 0, done.stderr
    assert checks_of(done)["uv"]["installed"] is True


def test_uv_missing_everywhere_returns_30(home, bin_dir):
    full_path(bin_dir)
    (bin_dir / "uv").unlink()
    done = doctor_cli(home, bin_dir, "doctor", "--json")
    assert done.returncode == 30, done.stderr
    assert checks_of(done)["uv"]["installed"] is False


def test_optional_dependencies_missing_or_unauthenticated_still_returns_0(home, bin_dir):
    full_path(bin_dir)
    done = doctor_cli(home, bin_dir, "doctor", "--json")
    assert done.returncode == 0, done.stderr
    absent = checks_of(done)
    for name in ("codex", "node"):
        assert absent[name]["required"] is False
        assert absent[name]["installed"] is False

    fake(bin_dir, "codex", "exit 1")
    fake(bin_dir, "node", "exit 0")
    done = doctor_cli(home, bin_dir, "doctor", "--json")
    assert done.returncode == 0, done.stderr
    present = checks_of(done)
    assert present["codex"]["installed"] is True
    assert present["codex"]["authenticated"] is False
    assert present["codex"]["required"] is False
    assert present["node"]["installed"] is True


def test_skip_hooks_drops_hooks_from_needed_by(home, bin_dir):
    full_path(bin_dir)
    default = checks_of(doctor_cli(home, bin_dir, "doctor", "--json"))
    assert default["codex"]["needed_by"] == ["skills", "hooks"]

    skipped = checks_of(doctor_cli(home, bin_dir, "doctor", "--skip", "hooks", "--json"))
    assert skipped["codex"]["needed_by"] == ["skills"]


def test_only_hud_lists_neither_gh_nor_claude(home, bin_dir):
    full_path(bin_dir)
    done = doctor_cli(home, bin_dir, "doctor", "--only", "hud", "--json")
    assert done.returncode == 0, done.stderr
    names = checks_of(done)
    assert "gh" not in names
    assert "claude" not in names


def test_doctor_does_not_wait_on_stdin_and_disables_gh_prompt(home, bin_dir):
    full_path(bin_dir)
    fake(
        bin_dir,
        "gh",
        f"""read x
echo "$GH_PROMPT_DISABLED" > "{bin_dir}/gh-prompt"
case "$1 $2" in
  "extension list") echo 'gh stack	github/gh-stack' ;;
esac
exit 0""",
    )
    started = time.monotonic()
    done = doctor_cli(home, bin_dir, "doctor", "--json")
    assert time.monotonic() - started < 20
    assert done.returncode == 0, done.stderr
    assert (bin_dir / "gh-prompt").read_text(encoding="utf-8").strip() == "1"


def test_hanging_auth_check_is_cut_off_and_unauthenticated(home, bin_dir, monkeypatch):
    full_path(bin_dir)
    fake(
        bin_dir,
        "gh",
        """case "$1 $2" in
  "auth status") exec /bin/sleep 5 ;;
  "extension list") echo 'gh stack	github/gh-stack' ;;
esac
exit 0""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.setattr(doctor, "TIMEOUT", 0.5)
    started = time.monotonic()
    checks = doctor.check(Selection(frozenset({"skills"}), "none"), home)
    assert time.monotonic() - started < 4
    gh = next(c for c in checks if c.name == "gh")
    assert gh.authenticated is False


@pytest.mark.parametrize(
    "args",
    [["--only", "skills", "--skip", "hud"], ["--only", "nope"]],
)
def test_bad_selection_returns_2(home, bin_dir, args):
    full_path(bin_dir)
    done = doctor_cli(home, bin_dir, "doctor", *args)
    assert done.returncode == 2


def test_list_json(home, bin_dir):
    done = doctor_cli(home, bin_dir, "list", "--json")
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    assert [c["name"] for c in data["components"]] == COMPONENT_ORDER
    assert [p["name"] for p in data["presets"]] == ["personal", "none"]
    skills = next(c for c in data["components"] if c["name"] == "skills")
    gh = next(d for d in skills["dependencies"] if d["name"] == "gh")
    assert gh["required"] is True
    assert gh["auth"] is True


def test_list_plain_names_every_component(home, bin_dir):
    done = doctor_cli(home, bin_dir, "list")
    assert done.returncode == 0, done.stderr
    for name in COMPONENT_ORDER:
        assert name in done.stdout


def test_missing_copilot_is_optional_and_does_not_fail_doctor(home, bin_dir):
    full_path(bin_dir)
    done = doctor_cli(home, bin_dir, "doctor", "--json")
    assert done.returncode == 0, done.stderr
    copilot = checks_of(done)["copilot"]
    assert copilot["required"] is False
    assert copilot["installed"] is False
    assert json.loads(done.stdout)["status"] == 0
