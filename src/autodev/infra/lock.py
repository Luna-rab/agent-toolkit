"""ランディレクトリ 1 つに driver を 1 本だけ走らせる錠（`driver.lock` の flock）。

`events` に書くのはメインループ 1 本だけという前提は、同じラン名の `autodev run` を
2 つ走らせると崩れる。錠は driver のプロセスが握り、落ちたら OS が外すので、残った錠で止まることは無い。
`clean`・`purge` も、錠が握られている間は worktree とランディレクトリに触らない。
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from types import TracebackType

#: 錠を取り直すまでの間と回数。`held_elsewhere` が共有の錠を一瞬だけ握るので、その間に起動した
#: driver を「ほかに走っている」で落とさない。driver が握り続けている錠なら、待っても取れない
_RETRY_INTERVAL = 0.02
_RETRIES = 10


class DriverBusy(RuntimeError):
    """同じランの driver が、ほかに走っている。"""


class DriverLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> DriverLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        for attempt in range(_RETRIES + 1):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if attempt < _RETRIES:
                    time.sleep(_RETRY_INTERVAL)
                    continue
                os.close(fd)
                raise DriverBusy(f"このランの driver がほかに走っている（{self._path}）") from error
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            # 錠のファイルは消さない。消すと、待っていた側が消える前のファイルの錠を取り、
            # 次に来た側が新しいファイルの錠を取って、2 本が同時に走る
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def held_elsewhere(path: Path) -> bool:
    """ほかのプロセス（同じプロセスでも別に開いた錠）が `DriverLock` を握っているか。

    flock には握り手を問い合わせる方法が無い（fcntl の F_GETLK は別の種類の錠しか見ない）ので、
    共有の錠を待たずに取ってみて、取れたらすぐ放す。排他の錠と違い、共有の錠どうしはぶつからないので、
    status を同時に何本走らせても互いに落とさない。錠のファイルが無ければ、driver は一度も走っていない。
    作らずに読むだけにする（status がランディレクトリにファイルを増やさない）。
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
