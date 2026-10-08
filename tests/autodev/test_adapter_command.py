"""コマンドを 1 回流す共通の部品（adapters/process/command.py）。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from autodev.adapters.process import command
from autodev.adapters.process.command import (
    NOT_FOUND,
    TIMED_OUT,
    Stopped,
    StopScope,
    run,
    run_raw,
    stoppable,
)


def test_出力はutf8で読み読めない字は置き換える(tmp_path: Path):
    got = run(["printf", "caf\\351 ok"], cwd=tmp_path)
    assert got.ok
    assert got.out == "caf� ok"


def test_バイト列のまま受けられる(tmp_path: Path):
    assert run_raw(["printf", "caf\\351"], cwd=tmp_path).out == b"caf\xe9"


def test_入力が無くても標準入力を閉じて渡す(tmp_path: Path):
    """driver の標準入力を受け継ぐと、入力を待って止まりうる。"""
    got = run(["cat"], cwd=tmp_path, timeout=5)
    assert got.ok
    assert got.out == ""


def test_時間切れなら子が起こしたプロセスまで止める(tmp_path: Path):
    started = time.monotonic()
    got = run(["sh", "-c", "sleep 30 | cat"], cwd=tmp_path, timeout=0.5)
    assert got.code == TIMED_OUT
    assert time.monotonic() - started < 10


def test_止める口から別のスレッドで子が起こしたプロセスまで止め止めた後は流さない(tmp_path: Path):
    scope = StopScope()
    caught: list[BaseException] = []

    def work() -> None:
        with stoppable(scope):
            try:
                run(["sh", "-c", "touch started; sleep 30 | cat"], cwd=tmp_path)
            except Stopped as error:
                caught.append(error)
            try:
                run(["true"], cwd=tmp_path)
            except Stopped as error:
                caught.append(error)

    thread = threading.Thread(target=work)
    started = time.monotonic()
    thread.start()
    while not (tmp_path / "started").exists():
        time.sleep(0.01)
    scope.stop()
    thread.join(10)
    assert not thread.is_alive() and time.monotonic() - started < 10
    assert len(caught) == 2
    # 止める口の外のコマンドは止めない
    assert run(["true"], cwd=tmp_path).ok


def test_子が先に終わってもSIGTERMを無視する孫をSIGKILLで止める(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """子（sh）は SIGTERM で終わるが、孫は SIGTERM を無視して出力の管を握ったまま残る。"""
    monkeypatch.setattr(command, "KILL_AFTER_SECONDS", 0.5)
    scope = StopScope()
    caught: list[BaseException] = []

    def work() -> None:
        with stoppable(scope):
            try:
                run(["sh", "-c", "(trap '' TERM; touch started; sleep 30) & wait"], cwd=tmp_path)
            except Stopped as error:
                caught.append(error)

    thread = threading.Thread(target=work)
    started = time.monotonic()
    thread.start()
    while not (tmp_path / "started").exists():
        time.sleep(0.01)
    scope.stop()
    thread.join(10)
    assert not thread.is_alive() and time.monotonic() - started < 10
    assert len(caught) == 1


def test_見つからないコマンドは127を返す(tmp_path: Path):
    assert run(["no-such-command-autodev"], cwd=tmp_path).code == NOT_FOUND
