"""ProcessRunner: 検証コマンドの終了コードと出力の末尾。"""

from __future__ import annotations

from pathlib import Path

from autodev.adapters.process.process import ProcessRunner
from autodev.domain.value_objects.verify_command import VerifyCommand


def test_終了コードと出力を返す(tmp_path: Path):
    got = ProcessRunner().run(VerifyCommand("echo out; echo err >&2; exit 3"), tmp_path)
    assert got.exit_code == 3
    assert not got.passed
    assert "out" in got.tail
    assert "err" in got.tail


def test_パイプとリダイレクトを含むコマンドを流せる(tmp_path: Path):
    """検証コマンドだけは bash -lc で流す。"""
    got = ProcessRunner().run(
        VerifyCommand("printf 'a\\nb\\n' | wc -l > n.txt && cat n.txt"), tmp_path
    )
    assert got.passed
    assert got.tail.strip() == "2"


def test_出力は末尾だけを残す(tmp_path: Path):
    got = ProcessRunner(tail_chars=10).run(VerifyCommand("seq 1 1000"), tmp_path)
    assert len(got.tail) == 10
    assert got.tail.endswith("1000\n")


def test_制限時間を超えたら子まで止めて124を返す(tmp_path: Path):
    got = ProcessRunner(timeout=0.5).run(VerifyCommand("echo start; sleep 30 | cat"), tmp_path)
    assert got.exit_code == 124
    assert "start" in got.tail
    assert "制限時間" in got.tail
