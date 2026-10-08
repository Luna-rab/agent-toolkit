"""イベントストア（`infra/store/eventstore.py`・`infra/store/db.py`）。SQLite のファイルは tmp_path に作る。"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from autodev_fakes import Queue, factory, requested, task

from autodev.domain.events.base import Event
from autodev.domain.events.record import UnknownEventType, to_record
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.stream_id import StreamId
from autodev.infra.store import db
from autodev.infra.store.eventstore import (
    CorruptStream,
    EventReader,
    EventStore,
    StoredEvent,
    VersionConflict,
    decoded,
    replay,
)

STACK = StreamId.stack()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "run" / "events.db"


@pytest.fixture
def store(path: Path):
    store = EventStore.open(path, clock=lambda: "2026-10-02T00:00:00.000000Z")
    yield store
    store.close()


def test_追記したイベントを同じ形で読み出せる(store: EventStore):
    stored = store.append(CommandId("c1"), STACK, 0, [requested(1), requested(2)])
    assert [(s.seq, s.version, s.event_id) for s in stored] == [
        (1, 1, EventId("stack#1")),
        (2, 2, EventId("stack#2")),
    ]
    store.append(CommandId("c2"), StreamId.task(task(1)), 0, [requested(3)])
    read = store.read_stream(STACK)
    assert read == stored
    assert [s.decode() for s in read] == [requested(1), requested(2)]
    assert [s.at for s in read] == ["2026-10-02T00:00:00.000000Z"] * 2
    assert [s.seq for s in store.read_all()] == [1, 2, 3]
    assert [s.seq for s in store.read_all(after=2)] == [3]
    assert store.last_seq() == 3


def test_1つのコマンドのイベントは半分だけ確定しない(store: EventStore):
    @dataclass(frozen=True)
    class Stray(Event):
        """表に無いイベント。2 つ目で JSON にできずに落ちる。"""

    with pytest.raises(UnknownEventType):
        store.append(CommandId("c1"), STACK, 0, [requested(1), Stray()])
    assert store.read_all() == []
    assert not store.has_command(CommandId("c1"))
    # 戻した後も、同じ接続で続けて書ける
    store.append(CommandId("c2"), STACK, 0, [requested(1)])
    assert [s.version for s in store.read_all()] == [1]


def test_集約の思っている版と違えば何も書かない(store: EventStore):
    store.append(CommandId("c1"), STACK, 0, [requested(1)])
    with pytest.raises(VersionConflict):
        store.append(CommandId("c2"), STACK, 0, [requested(2)])
    with pytest.raises(VersionConflict):
        store.append(CommandId("c3"), STACK, 5, [requested(2)])
    assert [s.command_id for s in store.read_all()] == [CommandId("c1")]


def test_コマンドのidでイベントの有無を引ける(store: EventStore):
    assert store.append(CommandId("empty"), STACK, 0, []) == []
    store.append(CommandId("c1"), STACK, 0, [requested(1)])
    assert store.has_command(CommandId("c1"))
    assert not store.has_command(CommandId("empty"))


def test_チェックポイントを読み書きできる(store: EventStore):
    assert store.checkpoints() == {}
    store.set_checkpoint("a", 3)
    store.set_checkpoint("b", 1)
    store.set_checkpoint("a", 4)
    assert store.checkpoints() == {"a": 4, "b": 1}


def test_開き直しても残っている(path: Path):
    with EventStore.open(path) as store:
        store.append(CommandId("c1"), STACK, 0, [requested(1)])
        store.set_checkpoint("a", 1)
    with EventStore.open(path) as store:
        assert [s.decode() for s in store.read_all()] == [requested(1)]
        assert store.checkpoints() == {"a": 1}


def test_WALで読む側は書きかけのトランザクションを待たない(
    store: EventStore, path: Path, monkeypatch: pytest.MonkeyPatch
):
    store.append(CommandId("c1"), STACK, 0, [requested(1)])
    # 待つと SQLITE_BUSY で落ちるようにする
    monkeypatch.setattr(db, "BUSY_TIMEOUT", 0.0)
    conn = store.connection
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO events (stream, version, type, v, command_id, at, data)"
        " VALUES ('stack', 2, 'GitJobQueued', 1, 'c2', 'x', '{}')"
    )
    with EventReader.open(path) as reader:
        assert reader._conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert [s.command_id for s in reader.read_all()] == [CommandId("c1")]
        conn.execute("COMMIT")
        assert [s.command_id for s in reader.read_all()] == [CommandId("c1"), CommandId("c2")]


def test_読むだけの接続では書けない(store: EventStore, path: Path):
    with EventReader.open(path) as reader, pytest.raises(sqlite3.OperationalError):
        reader._conn.execute("DELETE FROM events")


def test_無いランを読むだけの接続で開くと作らずに落ちる(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        EventReader.open(tmp_path / "none" / "events.db")
    assert not (tmp_path / "none").exists()


def test_書く接続は別のスレッドから使えない(store: EventStore):
    errors: list[BaseException] = []

    def write() -> None:
        try:
            store.append(CommandId("c1"), STACK, 0, [requested(1)])
        except BaseException as e:
            errors.append(e)

    thread = threading.Thread(target=write)
    thread.start()
    thread.join()
    assert len(errors) == 1
    assert isinstance(errors[0], sqlite3.ProgrammingError)


MOUNTS = """\
/dev/sdf / ext4 rw,relatime 0 0
C:\\134 /mnt/c 9p rw,noatime 0 0
D: /srv/my\\040disk drvfs rw 0 0
/dev/sdg /srv/my\\040disk/linux ext4 rw 0 0
"""


@pytest.mark.parametrize(
    ("where", "refused"),
    [
        ("/mnt/c/Users/x/events.db", True),
        ("/mnt/c", True),
        ("/srv/my disk/events.db", True),
        # Windows 側の下に重ねた Linux のファイルシステム
        ("/srv/my disk/linux/events.db", False),
        ("/home/u/.local/state/autodev/r/events.db", False),
        # /mnt/<1 文字> でも、マウント表で Linux 側なら通す
        ("/mnt/d/events.db", False),
        ("/mnt/cx/events.db", False),
    ],
)
def test_マウント表でWindows側のディスクを見分ける(where: str, refused: bool):
    if refused:
        with pytest.raises(db.StoreError, match="Windows"):
            db.refuse_windows_disk(Path(where), MOUNTS)
    else:
        db.refuse_windows_disk(Path(where), MOUNTS)


def test_マウント表を読めなければmntの1文字で見分ける(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "_read_mounts", lambda: None)
    with pytest.raises(db.StoreError, match="Windows"):
        db.connect(Path("/mnt/c/Users/x/events.db"))
    db.refuse_windows_disk(Path("/mnt/cx/events.db"))


def test_COMMITに落ちてもトランザクションを開いたまま残さない(tmp_path: Path):
    conn = db.connect(tmp_path / "events.db")
    conn.executescript(
        "PRAGMA foreign_keys = ON;"
        "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
        "CREATE TABLE child (parent INTEGER REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED);"
    )
    # 遅らせた外部キーの検査は COMMIT の所で落ちる
    with pytest.raises(sqlite3.IntegrityError), db.transaction(conn):
        conn.execute("INSERT INTO child VALUES (1)")
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM child").fetchone() == (0,)
    with db.transaction(conn):
        conn.execute("INSERT INTO parent VALUES (1)")
    conn.close()


def test_再生するとストリームごとの集約になる(store: EventStore):
    store.append(CommandId("c1"), STACK, 0, [requested(1), requested(2)])
    aggregates = replay(decoded(store.read_all()), factory)
    queue = aggregates[STACK]
    assert isinstance(queue, Queue)
    assert queue.waiting == [task(1), task(2)]
    assert queue.version == 2
    assert queue.processed == {CommandId("c1")}


def test_版が飛んだストリームは再生しない():
    record = StoredEvent(1, STACK, 2, CommandId("c1"), "x", to_record(requested(1)))
    with pytest.raises(CorruptStream):
        replay([(record, requested(1))], factory)


def test_書き出しの形は列をそのまま持つ(store: EventStore):
    (stored,) = store.append(CommandId("c1"), STACK, 0, [requested(1)])
    assert stored.to_json() == {
        "seq": 1,
        "stream": "stack",
        "version": 1,
        "type": "GitJobQueued",
        "v": 1,
        "command_id": "c1",
        "at": "2026-10-02T00:00:00.000000Z",
        "data": {
            "job": {
                "id": 1,
                "kind": "stack",
                "task": "task1",
                "branch": "stack/r--task1",
                "base": None,
                "previous": None,
                "discarded": [],
                "cut_from": None,
                "ready_overview": False,
            }
        },
    }
