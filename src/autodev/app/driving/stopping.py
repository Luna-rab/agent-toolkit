"""SIGTERM・SIGINT で driver を止める（終了コード 3）。

- 1 回目: Panic を渡し、パニックと同じ道で止める。走っている実行を interrupted にし、子が終わるのを
  待ってから終える。呼び直せば、止めたステージを `--resume` で続ける
- Panic が拒まれた（ランを終えた後など）・2 回目: 子をグループごと SIGKILL で止め、メインループを止める。
  driver はそれでも子を待ち終えてから終える（待たずに抜けると子が残る）

ハンドラはメインスレッドの、どの行の途中にでも割り込んで走る。そこで錠を取る・スレッドを起こすと、
メインスレッドが握っている錠（Inbox・threading の内部）と待ち合って止まりうる。そのためハンドラは
`SimpleQueue.put_nowait` だけをし（再入してよいと文書にある）、先に起こした見張りのスレッドが受ける。
"""

from __future__ import annotations

import queue
import signal
import threading
from collections.abc import Callable
from types import FrameType, TracebackType

#: (原因, 拒まれたときに理由を渡す関数) を受けて Panic を渡す
PanicSubmitter = Callable[[str, Callable[[str], None]], None]

SIGNALS = (signal.SIGTERM, signal.SIGINT)


class SignalStop:
    def __init__(
        self,
        *,
        panic: PanicSubmitter,
        kill: Callable[[], None],
        stop: Callable[[str], None],
    ) -> None:
        self._panic = panic
        self._kill = kill
        self._stop = stop
        self._queue: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        #: 受けたシグナルの名前（受けた順）
        self.received: list[str] = []
        self._watcher: threading.Thread | None = None
        self._previous: dict[signal.Signals, object] = {}

    def __enter__(self) -> SignalStop:
        # シグナルを受けられるのはメインスレッドだけ。ほかのスレッドで driver を回すときは置かない
        if threading.current_thread() is not threading.main_thread():
            return self
        self._watcher = threading.Thread(target=self._watch, name="signal-watch", daemon=True)
        self._watcher.start()
        self._previous = {sig: signal.signal(sig, self._handle) for sig in SIGNALS}
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)  # ty: ignore[invalid-argument-type]
        if self._watcher is not None:
            self._queue.put(None)
            self._watcher.join()

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        self._queue.put_nowait(signal.Signals(signum).name)

    def _watch(self) -> None:
        while (name := self._queue.get()) is not None:
            self.received.append(name)
            if len(self.received) == 1:
                self._panic(
                    f"{name} を受けた", lambda reason, name=name: self._rejected(name, reason)
                )
            else:
                self._hard(f"2 回目の {name} を受けたので、子を止めて終える")

    def _rejected(self, name: str, reason: str) -> None:
        self._hard(f"{name} を受けたが、パニックにできなかった（{reason}）。子を止めて終える")

    def _hard(self, reason: str) -> None:
        self._kill()
        self._stop(reason)
