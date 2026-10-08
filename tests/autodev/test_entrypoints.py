"""`autodev` の入口。コマンドとモジュールの実行のどちらでも使い方が出る。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

AUTODEV = str(Path(sys.executable).parent / "autodev")


@pytest.mark.parametrize(
    "command",
    [[AUTODEV, "--help"], [sys.executable, "-I", "-m", "autodev", "--help"]],
    ids=["コマンド", "python -m"],
)
def test_ヘルプは終了コード0で使い方を標準出力に出す(command: list[str]):
    done = subprocess.run(command, capture_output=True, text=True, check=False, cwd="/")
    assert done.returncode == 0, done.stderr
    assert "usage" in done.stdout.lower()
    assert "run" in done.stdout
