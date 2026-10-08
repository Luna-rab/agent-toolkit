"""driver の錠（`infra/lock.py`）。"""

from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path

import pytest

from autodev.infra.lock import DriverBusy, DriverLock, held_elsewhere


def test_statusが共有の錠を一瞬握っている間に起動したdriverは落とさない(tmp_path: Path):
    path = tmp_path / "driver.lock"
    path.touch()
    fd = os.open(path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH)
    timer = threading.Timer(0.05, lambda: os.close(fd))
    timer.start()
    try:
        with DriverLock(path):
            assert held_elsewhere(path)
    finally:
        timer.join()


def test_握り続けている錠は待っても取れない(tmp_path: Path):
    path = tmp_path / "driver.lock"
    with DriverLock(path), pytest.raises(DriverBusy):
        DriverLock(path).__enter__()
    assert not held_elsewhere(path)
