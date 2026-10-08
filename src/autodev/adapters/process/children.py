"""driver が起こした子プロセス（claude・git・検証コマンド）の控え。

子はどれも `start_new_session=True` で起こす（止めるときに孫までまとめて止めるため）。そのため driver が
SIGKILL で落ちると、子は止まらずに走り続ける。そこへ呼び直すと同じ実行が 2 本走り、`clean`・`purge` は
走っている子の下から worktree を消す。それを見分けるために、子を起こしたら控えを置き、待ち終えたら消す。

控えは `<置き場>/<pid>.json` に pid・開始時刻・起動の id（boot_id）を書く。pid は使い回されるので、
pid が生きているかだけでは別のプロセスを取り違える。開始時刻（`/proc/<pid>/stat` の starttime。起動
からの clock tick）と boot_id が控えと同じときだけ、同じプロセスとみなす。

置き場は driver が `track_in` で 1 つ決める（driver は 1 プロセスに 1 つ）。決めていなければ控えを
置かない（実行器や Git を driver の外で使うとき）。

Linux の `/proc` が前提である。置き場を決めたのに控えを置けない（`/proc` が無い・書けない）ときは、
黙って見張らずに走らせず、子を止めてから OSError を投げ直す。

"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_directory: Path | None = None


@dataclass(frozen=True)
class Child:
    pid: int
    command: str


def track_in(directory: Path | None) -> None:
    """これから起こす子の控えを `directory` に置く。None で止める。"""
    global _directory  # noqa: PLW0603
    with _lock:
        _directory = directory


def _boot_id() -> str:
    """起動の id。`/proc` が無い所（Linux でない）では OSError で、控えを置けない。"""
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()


def _identity(pid: int) -> tuple[str, str] | None:
    """（boot_id, 開始時刻）。プロセスが無ければ None。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        boot = _boot_id()
    except OSError:
        return None
    # 2 つ目の欄（コマンド名）は括弧の中に空白を含みうるので、最後の `)` から後ろを数える
    fields = stat.rsplit(")", 1)[1].split()
    # fields[0] が 3 つ目の欄（状態）。終わって待たれていないもの（Z）は、もう走っていない
    if fields[0] in ("Z", "X"):
        return None
    return boot, fields[19]


def record(pid: int, argv: Sequence[str]) -> None:
    """控えを置く。置けなければ OSError（`/proc` が無い・書けない）。呼んだ側は、見張れない子を
    走らせ続けず、止めてから投げ直す（`stop_unwatched`）。"""
    with _lock:
        directory = _directory
    if directory is None:
        return
    _boot_id()
    identity = _identity(pid)
    if identity is None:
        # もう終わった子。見張るものが無い
        return
    boot, start = identity
    body = {"pid": pid, "boot": boot, "start": start, "command": " ".join(argv[:3])}
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def stop_unwatched(child: subprocess.Popen[Any]) -> None:
    """控えを置けなかった子を、グループごと止めて待つ。見張れない子を走らせると、driver が落ちた後に
    残っても誰も見分けられない。"""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(child.pid, signal.SIGKILL)
    child.wait()


def kill_survivors(directory: Path) -> list[Child]:
    """控えのうち生きている子を、グループごと SIGKILL で止める（2 回目のシグナル）。止めたものを返す。"""
    found = survivors(directory)
    for child in found:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signal.SIGKILL)
    return found


def forget(pid: int) -> None:
    """控えを消す。子を待ち終えた後だけ呼ぶ（待つ前に消すと、残った子を見分けられない）。"""
    with _lock:
        directory = _directory
    if directory is not None:
        (directory / f"{pid}.json").unlink(missing_ok=True)


def survivors(directory: Path) -> list[Child]:
    """控えのうち、まだ同じプロセスが生きているもの。もう生きていない控えは消す。

    消すのは、錠を取って前の driver がいないと分かった側（`run`・`clean`・`purge`）だけが呼ぶ。
    """
    return _scan(directory, prune=True)


def alive(directory: Path) -> list[Child]:
    """`survivors` と同じものを、控えを消さずに数える。錠を取らずに読む側（`status`）が使う。

    置き場を読めない（PermissionError など）ときは、分かる分だけ返して落ちない。
    """
    try:
        return _scan(directory, prune=False)
    except OSError:
        return []


def _scan(directory: Path, *, prune: bool) -> list[Child]:
    found: list[Child] = []
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            pid, boot, start = int(body["pid"]), str(body["boot"]), str(body["start"])
        except (OSError, ValueError, KeyError, TypeError):
            # 一時ファイルから置き換えるので書きかけは無い。読めない控えは確かめようが無いので消す
            if prune:
                with contextlib.suppress(OSError):
                    path.unlink()
            continue
        if _identity(pid) == (boot, start):
            found.append(Child(pid, str(body.get("command") or "")))
        elif prune:
            path.unlink(missing_ok=True)
    return found
