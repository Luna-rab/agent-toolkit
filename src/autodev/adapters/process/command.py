"""コマンドを 1 回流す。Git・Forge・ProcessRunner が使う。

シェルを通さない。ラン名やブランチ名がそのまま引数に入るので、シェルを通すと
空白や引用符の混ざった値でコマンドが組み変わる。
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import children

#: 見つからないコマンド・時間切れに付ける終了コード（シェルの慣習に合わせる）
NOT_FOUND = 127
TIMED_OUT = 124
#: 止めるとき、SIGTERM から SIGKILL までに待つ秒数。git は SIGTERM でロックのファイルを片付けて終わる
KILL_AFTER_SECONDS = 10.0


class Stopped(RuntimeError):
    """`StopScope.stop` で止めた。止めた後に流そうとしたコマンドも、流さずにこれを投げる。"""


class StopScope:
    """そのスレッドが流す子プロセスを、別のスレッドからまとめて止める口（`stoppable` の中で流したもの）。

    子はどれも新しいプロセスグループで起こすので、グループごとに止める（孫まで止まる）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._children: set[subprocess.Popen[Any]] = set()
        self._stopped = False

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            children = list(self._children)
        for child in children:
            _signal_group(child, signal.SIGTERM)
            timer = threading.Timer(KILL_AFTER_SECONDS, _signal_group, (child, signal.SIGKILL))
            timer.daemon = True
            timer.start()

    @contextlib.contextmanager
    def _track(self, child: subprocess.Popen[Any]) -> Iterator[None]:
        with self._lock:
            self._children.add(child)
            stopped = self._stopped
        if stopped:
            _signal_group(child, signal.SIGKILL)
        try:
            yield
        finally:
            with self._lock:
                self._children.discard(child)


_local = threading.local()


@contextlib.contextmanager
def stoppable(scope: StopScope) -> Iterator[StopScope]:
    """この中で（このスレッドが）流すコマンドを、`scope.stop()` で止められるようにする。"""
    previous = getattr(_local, "scope", None)
    _local.scope = scope
    try:
        yield scope
    finally:
        _local.scope = previous


def _scope() -> StopScope | None:
    return getattr(_local, "scope", None)


def check_stopped() -> None:
    """止めた後なら、コマンドを流さずに Stopped を投げる。"""
    scope = _scope()
    if scope is not None and scope.stopped:
        raise Stopped("止めたので流さない")


@contextlib.contextmanager
def tracked(child: subprocess.Popen[Any]) -> Iterator[None]:
    """`child` が走っている間、今の StopScope から止められるようにする。"""
    scope = _scope()
    if scope is None:
        yield
        return
    with scope._track(child):
        yield


def _signal_group(child: subprocess.Popen[Any], sig: signal.Signals) -> None:
    """子のプロセスグループに送る。子がもう終わっていても送る。

    子が SIGTERM で終わっても、SIGTERM を無視する孫はグループに残って出力の管を握り、`communicate`
    が返らない。グループの id（子の pid）は、グループに誰かいる間は使い回されないので、子を待ち終えた
    後でも送ってよい。誰もいなければ ProcessLookupError になるので、捨てる。
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(child.pid, sig)


@dataclass(frozen=True)
class Completed:
    """コマンド 1 回の結果。終了コードが 0 以外でも例外にしない（読む側が決める）。"""

    argv: tuple[str, ...]
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


@dataclass(frozen=True)
class RawCompleted:
    """標準出力をバイト列のまま受けた結果（ファイルの中身を読むとき）。"""

    argv: tuple[str, ...]
    code: int
    out: bytes
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def decoded(self) -> Completed:
        return Completed(self.argv, self.code, _decode(self.out), self.err)


class CommandFailed(RuntimeError):
    """呼んだ側が成功を前提にしていたコマンドが落ちた。"""

    def __init__(self, completed: Completed | RawCompleted) -> None:
        self.completed = completed
        out = completed.out if isinstance(completed.out, str) else _decode(completed.out)
        detail = (completed.err or out).strip()
        super().__init__(f"{shlex.join(completed.argv)}（終了コード {completed.code}）: {detail}")


def merged_env(env: Mapping[str, str | None] | None) -> dict[str, str]:
    """今の環境に `env` を重ねる。値が None の変数は外す。

    空文字を入れる形では足りない。`ANTHROPIC_API_KEY=""` でも「設定されている」と読む相手がいる。
    """
    merged = dict(os.environ)
    for key, value in (env or {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def run_raw(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str | None] | None = None,
    stdin: str | None = None,
    timeout: float | None = None,
) -> RawCompleted:
    """`stoppable` の中なら、止めたとき（止めた後に呼んだときも）Stopped を投げる。止めた子の終了
    コードを読ませると、呼んだ側が「在った」「無かった」と読み違える。"""
    args = tuple(str(a) for a in argv)
    check_stopped()
    try:
        child = subprocess.Popen(
            args,
            cwd=cwd,
            env=merged_env(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # 時間切れのとき、子が起こしたプロセスまでまとめて止める。子だけを止めると、孫が
            # 出力の管を握ったまま残り、読み終わらない
            start_new_session=True,
        )
    except FileNotFoundError:
        return RawCompleted(args, NOT_FOUND, b"", f"コマンドが見つからない: {args[0]}")
    # 渡すものが無くても空の入力を渡して閉じる。driver の標準入力を受け継ぐと、入力を待って止まりうる
    feed = (stdin or "").encode("utf-8")
    try:
        children.record(child.pid, args)
    except BaseException:
        children.stop_unwatched(child)
        raise
    with tracked(child):
        try:
            out, err = child.communicate(feed, timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(child.pid, signal.SIGKILL)
            out, err = child.communicate()
            children.forget(child.pid)
            check_stopped()
            return RawCompleted(
                args, TIMED_OUT, out, _decode(err) + f"\n制限時間を超えた（{timeout} 秒）"
            )
    # 待ち終えた後だけ消す。待てずに抜けた（例外）なら、控えを残して生きているかを後で見分ける
    children.forget(child.pid)
    check_stopped()
    return RawCompleted(args, child.returncode, out, _decode(err))


def run(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str | None] | None = None,
    stdin: str | None = None,
    timeout: float | None = None,
) -> Completed:
    """出力は utf-8 で読み、読めない字は置き換える（落とさない）。"""
    return run_raw(argv, cwd=cwd, env=env, stdin=stdin, timeout=timeout).decoded()


def checked(completed: Completed) -> Completed:
    if not completed.ok:
        raise CommandFailed(completed)
    return completed
