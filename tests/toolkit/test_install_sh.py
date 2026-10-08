"""`install.sh` を、偽物の uv・agent-toolkit・mise と一時ディレクトリの HOME で動かして確かめる。

PATH は偽物のディレクトリと /usr/bin:/bin だけにする。実物の uv は呼ばない。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from tests.conftest import REPO_ROOT

INSTALL_SH = REPO_ROOT / "install.sh"

# 偽物の uv。引数を 1 行ずつ記録する。`tool dir --bin` は偽物の agent-toolkit のあるディレクトリを出す
FAKE_UV = """#!/bin/bash
echo "$*" >> "$FAKE_LOG_DIR/uv.log"
if [[ "$1 $2" == "tool dir" ]]; then
  echo "$FAKE_TOOL_BIN"
  exit "${FAKE_UV_DIR_RC:-0}"
fi
if [[ "$1 $2" == "tool install" ]]; then
  exit "${FAKE_UV_INSTALL_RC:-0}"
fi
exit 0
"""

FAKE_TOOLKIT = """#!/bin/bash
echo "$*" >> "$FAKE_LOG_DIR/toolkit.log"
exit "${FAKE_TOOLKIT_RC:-0}"
"""

# 偽物の mise。`which uv` に偽物の uv のパスを出す
FAKE_MISE = """#!/bin/bash
echo "$*" >> "$FAKE_LOG_DIR/mise.log"
if [[ "$1 $2" == "which uv" ]]; then
  echo "$FAKE_MISE_UV"
  exit 0
fi
exit 1
"""


@dataclass
class Sandbox:
    root: Path
    home: Path
    path_bin: Path  # PATH の先頭に置くディレクトリ（初めは空）
    tool_bin: Path  # `uv tool dir --bin` が出すディレクトリ。PATH には入れない
    logs: Path

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        full_env = {
            "HOME": str(self.home),
            "PATH": f"{self.path_bin}:/usr/bin:/bin",
            "FAKE_LOG_DIR": str(self.logs),
            "FAKE_TOOL_BIN": str(self.tool_bin),
            "FAKE_MISE_UV": str(self.root / "mise-uv" / "uv"),
            **env,
        }
        return subprocess.run(
            [str(INSTALL_SH), *args],
            env=full_env,
            cwd=self.home,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def log(self, name: str) -> list[str]:
        path = self.logs / name
        return path.read_text().splitlines() if path.exists() else []


def write_exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    if shutil.which("uv", path="/usr/bin:/bin"):
        pytest.skip("/usr/bin か /bin に実物の uv があり、見えない状態を作れない")
    box = Sandbox(
        root=tmp_path,
        home=tmp_path / "home",
        path_bin=tmp_path / "pathbin",
        tool_bin=tmp_path / "toolbin",
        logs=tmp_path / "logs",
    )
    for d in (box.home, box.path_bin, box.logs):
        d.mkdir()
    write_exe(box.tool_bin / "agent-toolkit", FAKE_TOOLKIT)
    return box


def put_uv_on_path(box: Sandbox) -> None:
    write_exe(box.path_bin / "uv", FAKE_UV)


def test_cli_only_installs_cli_and_skips_agent_toolkit(sandbox):
    put_uv_on_path(sandbox)

    result = sandbox.run("--cli-only")

    assert result.returncode == 0, result.stderr
    assert sandbox.log("uv.log")[0] == f"tool install --editable --reinstall {REPO_ROOT}"
    assert sandbox.log("toolkit.log") == []


def test_arguments_are_passed_to_agent_toolkit_install(sandbox):
    put_uv_on_path(sandbox)

    result = sandbox.run("--offline", "--preset", "none")

    assert result.returncode == 0, result.stderr
    assert sandbox.log("toolkit.log") == ["install --offline --preset none"]
    assert f"tool install --editable --reinstall {REPO_ROOT}" in sandbox.log("uv.log")


def test_no_arguments_runs_full_install(sandbox):
    put_uv_on_path(sandbox)

    result = sandbox.run()

    assert result.returncode == 0, result.stderr
    assert sandbox.log("toolkit.log") == ["install"]


def test_agent_toolkit_exit_code_is_returned_as_is(sandbox):
    put_uv_on_path(sandbox)

    result = sandbox.run(FAKE_TOOLKIT_RC="20")

    assert result.returncode == 20
    assert sandbox.log("toolkit.log") == ["install"]


@pytest.mark.parametrize("extra", ["--offline", "--only", "--preset"])
def test_cli_only_with_other_arguments_exits_2_without_calling_uv(sandbox, extra):
    put_uv_on_path(sandbox)

    first = sandbox.run("--cli-only", extra)
    second = sandbox.run(extra, "--cli-only")

    assert first.returncode == 2
    assert second.returncode == 2
    assert sandbox.log("uv.log") == []
    assert sandbox.log("toolkit.log") == []


def test_no_uv_anywhere_exits_10(sandbox):
    result = sandbox.run()

    assert result.returncode == 10
    assert sandbox.log("toolkit.log") == []


def test_no_uv_anywhere_exits_10_with_cli_only(sandbox):
    assert sandbox.run("--cli-only").returncode == 10


def test_uv_env_pointing_to_missing_path_exits_10(sandbox):
    put_uv_on_path(sandbox)  # PATH に uv があっても、UV の指定が壊れていれば 10

    result = sandbox.run(UV=str(sandbox.root / "no-such" / "uv"))

    assert result.returncode == 10
    assert sandbox.log("uv.log") == []
    assert sandbox.log("toolkit.log") == []


def test_uv_env_is_used_when_path_has_no_uv(sandbox):
    fake = write_exe(sandbox.root / "elsewhere" / "uv", FAKE_UV)

    result = sandbox.run("--cli-only", UV=str(fake))

    assert result.returncode == 0, result.stderr
    assert sandbox.log("uv.log") == [f"tool install --editable --reinstall {REPO_ROOT}"]


def test_uv_found_through_mise_when_path_has_no_uv(sandbox):
    write_exe(sandbox.home / ".local" / "bin" / "mise", FAKE_MISE)
    write_exe(sandbox.root / "mise-uv" / "uv", FAKE_UV)

    result = sandbox.run()

    assert result.returncode == 0, result.stderr
    assert sandbox.log("mise.log") == ["which uv"]
    assert f"tool install --editable --reinstall {REPO_ROOT}" in sandbox.log("uv.log")
    assert sandbox.log("toolkit.log") == ["install"]


def test_uv_tool_install_failure_exits_11_without_calling_agent_toolkit(sandbox):
    put_uv_on_path(sandbox)

    result = sandbox.run(FAKE_UV_INSTALL_RC="1")

    assert result.returncode == 11
    assert sandbox.log("toolkit.log") == []


def test_uv_tool_install_failure_exits_11_with_cli_only(sandbox):
    put_uv_on_path(sandbox)

    assert sandbox.run("--cli-only", FAKE_UV_INSTALL_RC="1").returncode == 11


def test_uv_tool_dir_failure_exits_11_without_calling_agent_toolkit(sandbox):
    put_uv_on_path(sandbox)

    result = sandbox.run(FAKE_UV_DIR_RC="1")

    assert result.returncode == 11
    assert sandbox.log("toolkit.log") == []


def test_old_autodev_symlink_into_dotfiles_is_removed(sandbox):
    put_uv_on_path(sandbox)
    bindir = sandbox.home / ".local" / "bin"
    bindir.mkdir(parents=True)
    old = bindir / "autodev"
    old.symlink_to("/x/dist/dot-claude/skills/autodev/scripts/autodev.py")

    result = sandbox.run("--cli-only")

    assert result.returncode == 0, result.stderr
    assert not old.is_symlink()
    assert not old.exists()


def test_other_autodev_symlink_is_kept(sandbox):
    put_uv_on_path(sandbox)
    bindir = sandbox.home / ".local" / "bin"
    bindir.mkdir(parents=True)
    other = bindir / "autodev"
    other.symlink_to("/x/somewhere/else/autodev.py")

    result = sandbox.run("--cli-only")

    assert result.returncode == 0, result.stderr
    assert other.is_symlink()
    assert os.readlink(other) == "/x/somewhere/else/autodev.py"


def test_install_sh_has_valid_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
