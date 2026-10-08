"""シグナルで driver を止める（`app/driving/stopping.py`）。1 回目は Panic、拒まれたか 2 回目なら子を止めて終える。"""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Callable

from autodev.app.driving.stopping import SignalStop


class Recorder:
    def __init__(self, *, reject: str | None = None) -> None:
        self.reject = reject
        self.events: list[str] = []
        self.done = threading.Event()

    def panic(self, cause: str, rejected: Callable[[str], None]) -> None:
        self.events.append(f"panic: {cause}")
        if self.reject is not None:
            rejected(self.reject)
        self.done.set()

    def kill(self) -> None:
        self.events.append("kill")

    def stop(self, reason: str) -> None:
        self.events.append(f"stop: {reason}")
        self.done.set()


def make(recorder: Recorder) -> SignalStop:
    return SignalStop(panic=recorder.panic, kill=recorder.kill, stop=recorder.stop)


def wait_for(recorder: Recorder, count: int) -> None:
    for _ in range(200):
        if len(recorder.events) >= count:
            return
        recorder.done.wait(0.01)


def test_1回目のシグナルはPanicを渡すだけで子は止めない():
    recorder = Recorder()
    with make(recorder) as stop:
        os.kill(os.getpid(), signal.SIGTERM)
        wait_for(recorder, 1)
    assert recorder.events == ["panic: SIGTERM を受けた"]
    assert stop.received == ["SIGTERM"]


def test_2回目のシグナルは子を止めてメインループを止める():
    recorder = Recorder()
    with make(recorder):
        os.kill(os.getpid(), signal.SIGINT)
        wait_for(recorder, 1)
        os.kill(os.getpid(), signal.SIGINT)
        wait_for(recorder, 3)
    assert recorder.events == [
        "panic: SIGINT を受けた",
        "kill",
        "stop: 2 回目の SIGINT を受けたので、子を止めて終える",
    ]


def test_Panicが拒まれたら子を止めてメインループを止める():
    recorder = Recorder(reject="ランはもう終わっている")
    with make(recorder):
        os.kill(os.getpid(), signal.SIGTERM)
        wait_for(recorder, 3)
    assert recorder.events == [
        "panic: SIGTERM を受けた",
        "kill",
        "stop: SIGTERM を受けたが、パニックにできなかった（ランはもう終わっている）。子を止めて終える",
    ]


def test_抜けたらもとのハンドラに戻す():
    before = signal.getsignal(signal.SIGTERM)
    with make(Recorder()):
        assert signal.getsignal(signal.SIGTERM) is not before
    assert signal.getsignal(signal.SIGTERM) is before
