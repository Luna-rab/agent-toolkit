"""子プロセスの控え（`adapters/process/children.py`）: pid と開始時刻と boot_id で、同じプロセスかを見分ける。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from autodev.adapters.process import children, command


@pytest.fixture
def tracked(tmp_path: Path):
    children.track_in(tmp_path)
    yield tmp_path
    children.track_in(None)


def test_控えを置いた子は生きている間だけ残り終わったら消える(tracked: Path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        children.record(child.pid, ["python", "-c", "sleep"])
        assert children.survivors(tracked) == [children.Child(child.pid, "python -c sleep")]
    finally:
        child.kill()
        child.wait()
    assert children.survivors(tracked) == []
    # もう生きていない控えは消す
    assert list(tracked.glob("*.json")) == []


def test_pidが同じでも開始時刻が違えば別のプロセスとみなす(tracked: Path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        children.record(child.pid, ["python"])
        path = tracked / f"{child.pid}.json"
        body = json.loads(path.read_text(encoding="utf-8"))
        body["start"] = str(int(body["start"]) - 1)
        path.write_text(json.dumps(body), encoding="utf-8")
        assert children.survivors(tracked) == []
    finally:
        child.kill()
        child.wait()


def test_控えを書けなければ子を止めて待ってから投げ直す(tmp_path: Path):
    # 置き場がファイルなので、控えを書けない
    blocked = tmp_path / "children"
    blocked.write_text("", encoding="utf-8")
    children.track_in(blocked)
    started: list[int] = []
    original = subprocess.Popen

    def spy(*args, **kwargs):
        child = original(*args, **kwargs)
        started.append(child.pid)
        return child

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(subprocess, "Popen", spy)
            began = time.monotonic()
            with pytest.raises(OSError):
                command.run_raw([sys.executable, "-c", "import time; time.sleep(30)"])
    finally:
        children.track_in(None)
    assert time.monotonic() - began < 10
    (pid,) = started
    # 待ち終えているので、ゾンビとしても残っていない
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_子を待てずに抜けたら控えを消さない(tracked: Path, monkeypatch: pytest.MonkeyPatch):
    def broken(self, *args, **kwargs):
        raise RuntimeError("待てなかった")

    monkeypatch.setattr(subprocess.Popen, "communicate", broken)
    with pytest.raises(RuntimeError):
        command.run_raw([sys.executable, "-c", "import time; time.sleep(5)"])
    (record,) = tracked.glob("*.json")
    pid = json.loads(record.read_text(encoding="utf-8"))["pid"]
    os.kill(pid, 9)


def test_置き場を決めていなければ控えを置かない(tmp_path: Path):
    children.record(1, ["init"])
    children.forget(1)
    assert list(tmp_path.iterdir()) == []
