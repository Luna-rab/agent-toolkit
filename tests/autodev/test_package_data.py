"""資材（指示書・スキーマ・雛形）はパッケージデータから引く。スキルのディレクトリには依らない。"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from autodev.app.driving.driver import default_status_command
from autodev.app.stages.prompts import assets
from autodev.domain.value_objects.run_name import RunName
from autodev.infra.paths import RunPaths
from conftest import AUTODEV_PACKAGE


def test_指示書の置き場はパッケージの中のcontracts():
    assert assets.contract_path("plan") == AUTODEV_PACKAGE / "contracts" / "plan.md"
    assert assets.contract_path("plan").is_file()


def test_スキーマの本文はパッケージの中のschemasから読む():
    expected = (AUTODEV_PACKAGE / "schemas" / "plan.json").read_text(encoding="utf-8")
    assert assets.schema_text("plan") == expected


def test_パッケージの根はautodevパッケージのディレクトリでSKILL_mdを持たない():
    assert assets.package_root() == AUTODEV_PACKAGE
    assert not (AUTODEV_PACKAGE / "SKILL.md").exists()
    for name in ("contracts", "schemas", "templates"):
        assert (AUTODEV_PACKAGE / name).is_dir(), name


def test_スキルのディレクトリを使わずに指示書とスキーマを引ける(tmp_path: Path):
    """`SKILL.md` を探す実装なら、スキルの無い環境（別の作業ディレクトリ・`-I`）で FileNotFoundError になる。"""
    code = (
        "from autodev.app.stages.prompts import assets;"
        "print(assets.contract_path('plan').is_file(), len(assets.schema_text('plan')) > 0)"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert (done.returncode, done.stdout.strip()) == (0, "True True"), done.stderr


def test_起動のコマンドはPATHのautodevを指す():
    found = assets.launcher()
    assert Path(found).name == "autodev"
    if Path(found).is_absolute():
        assert Path(found).is_file()


def test_統括に渡す状態を読むコマンドはautodevのstatusでpythonを前に付けない():
    command = default_status_command(RunPaths(RunName("demo"), Path("/tmp/run")))
    words = shlex.split(command)
    assert words == [assets.launcher(), "status", "--json", "--name", "demo"]
