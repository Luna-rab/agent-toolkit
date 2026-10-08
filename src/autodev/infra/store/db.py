"""`events.db` への接続とテーブル。

- 書く接続は driver のメインループの 1 つだけ。Python の `sqlite3` の接続は既定でスレッドを
  またげない（`check_same_thread`）ので、別のスレッドから使えば例外で気づける
- driver の外の CLI（`autodev answer` など）は、`requests` に 1 行足すためだけに自分の書く接続を開く。
  別のプロセスなので SQLite のロックで直列になり、`BUSY_TIMEOUT` まで待つ
- WAL モードにする。読むだけの接続（`status --json`・`events`）が、driver の書き込みを待たない
- トランザクションは `transaction` で明示する。`isolation_level=None` にして、`sqlite3` が
  暗黙に BEGIN を挟まないようにする（どこで確定するかを、コードを読めば分かるようにする）
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: 別のプロセスの書き込みを待つ秒数
BUSY_TIMEOUT = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  stream     TEXT NOT NULL,
  version    INTEGER NOT NULL,
  type       TEXT NOT NULL,
  v          INTEGER NOT NULL,
  command_id TEXT NOT NULL,
  at         TEXT NOT NULL,
  data       TEXT NOT NULL,
  UNIQUE (stream, version)
);
CREATE INDEX IF NOT EXISTS events_by_stream ON events (stream, version);
CREATE INDEX IF NOT EXISTS events_by_command ON events (command_id);
CREATE TABLE IF NOT EXISTS checkpoints (subscriber TEXT PRIMARY KEY, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
  id   TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  data TEXT NOT NULL,
  at   TEXT NOT NULL
);
"""

#: WSL から見た Windows 側のディスク（`/mnt/c` など）
_WINDOWS_DISK = re.compile(r"/mnt/[a-zA-Z](?:/|$)")


class StoreError(Exception):
    """`events.db` を開けない・使えない。"""


class EventsDbMissing(FileNotFoundError):
    """開こうとした時点で `events.db` が無い。ランが無いことを、ほかの FileNotFoundError と分ける。"""


def connect(path: Path, *, create: bool = True) -> sqlite3.Connection:
    """書く接続を開く。`create` なら、無ければファイルとテーブルを作る。

    `create=False` は、無いランの events.db を作らずに FileNotFoundError にする（driver の外の CLI。
    ラン名を打ち間違えたときに、空のランを作って黙って要求を溜めないため）。
    """
    path = path.absolute()
    if not create and not path.is_file():
        raise EventsDbMissing(f"events.db が無い: {path}")
    # シンボリックリンクの先が Windows 側のこともあるので、たどった先で見る
    refuse_windows_disk(path.resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT, isolation_level=None)
    try:
        (mode,) = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if mode != "wal":
            raise StoreError(f"{path} を WAL モードにできない（{mode} のまま）")
        # WAL の NORMAL は、確定のたびにディスクへの書き切り（fsync）を待たない。プロセスが落ちても
        # 確定済みのものは消えない。消えうるのは OS ごと落ちたときの直近の確定だけで、そのときも
        # データベースは壊れず、消えた分は再生と受け手の配り直しで受け直せる。FULL（既定）だと
        # 確定 1 回に数 ms 待ち、イベント 1 つごとにメインループが止まる
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
    except BaseException:
        conn.close()
        raise
    return conn


def connect_readonly(path: Path) -> sqlite3.Connection:
    """読むだけの接続を開く。ファイルが無ければ作らずに FileNotFoundError にする。

    WAL の読み手も `-shm` を使うので、ディレクトリに書ければ SQLite がそれを作る（中身は変えない）。
    """
    path = path.absolute()
    if not path.is_file():
        raise EventsDbMissing(f"events.db が無い: {path}")
    return sqlite3.connect(
        f"{path.as_uri()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT, isolation_level=None
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """抜けたら確定し、例外なら全部を戻す。

    IMMEDIATE で始めて、書き込みのロックを最初に取る。DEFERRED だと、読んだ後に書こうとした所で
    別のプロセスの書き込みとぶつかり、待たずに SQLITE_BUSY で落ちることがある。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        # COMMIT が落ちたときも含め、開いたままのトランザクションを残さない。SQLite が自分で
        # 戻した後（ディスクが一杯など）に ROLLBACK を送ると、それ自体が落ちて元の例外を隠す
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def refuse_windows_disk(path: Path, mounts: str | None = None) -> None:
    """置き場が Windows 側のディスクなら StoreError にする。

    WSL からは Windows 側のディスクのファイルのロックが当てにならず、SQLite が壊れうる。マウント表
    （`/proc/self/mounts`）で置き場のファイルシステムを見て、drvfs か 9p なら拒む。マウント表を
    読めなければ、`/mnt/<1 文字>` の下かどうかで見る。
    """
    if mounts is None:
        mounts = _read_mounts()
    if mounts is None:
        windows = _WINDOWS_DISK.match(str(path)) is not None
    else:
        windows = _filesystem_of(path, mounts) in _WINDOWS_FILESYSTEMS
    if windows:
        raise StoreError(
            f"events.db を Windows 側のディスクに置かない（WSL ではロックが効かない）: {path}"
        )


#: WSL が Windows 側のディスクをつなぐファイルシステム
_WINDOWS_FILESYSTEMS = frozenset({"drvfs", "9p"})


def _read_mounts() -> str | None:
    try:
        return Path("/proc/self/mounts").read_text(encoding="utf-8")
    except OSError:
        return None


def _filesystem_of(path: Path, mounts: str) -> str | None:
    """`path` を含むマウントのうち、一番深いもののファイルシステム。同じ所に重ねたら後のもの。"""
    target = str(path)
    found: tuple[int, str] | None = None
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        # マウント表は空白などを `\040` のように 8 進で書く
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[1])
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        if inside and (found is None or len(point) >= found[0]):
            found = (len(point), fields[2])
    return None if found is None else found[1]
