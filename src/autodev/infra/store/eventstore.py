"""イベントストア。

`events` は状態の正本で、追記だけ。`checkpoints` は受け手（ポリシーと反応）ごとに、どのイベントまで
受け終えたか。手で書き換えない。

    store = EventStore.open(paths.events_db)       # driver のメインループだけ
    stored = store.append(command.command_id, stream, aggregate.version, events)

    with EventReader.open(paths.events_db) as reader:   # status --json・events
        rows = reader.read_all()
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar

from ...domain.aggregates.base import Aggregate
from ...domain.events.base import Event
from ...domain.events.record import EventRecord, from_record, to_record
from ...domain.value_objects.command_id import CommandId
from ...domain.value_objects.event_id import EventId
from ...domain.value_objects.stream_id import StreamId
from ..files import utc_now
from . import db

R = TypeVar("R", bound="EventReader")

#: ストリームから、イベントが 1 つも無いときの集約を作る
AggregateFactory = Callable[[StreamId], Aggregate]


class VersionConflict(Exception):
    """追記しようとしたストリームの版が、集約の思っている版と違う。

    書くのはメインループ 1 本だけなので、起きたら集約とストアが食い違っている（再生し直す）。
    """


class CorruptStream(Exception):
    """ストリームの版が 1 から 1 ずつ並んでいない。"""


@dataclass(frozen=True)
class StoredEvent:
    """`events` の 1 行。中身は `EventRecord` のまま持ち、要るときに `decode` で読む。

    `autodev events` の書き出しは、この driver が知らないイベントがあっても止めずに出したいので、
    読み出しでは読み替えない。
    """

    seq: int
    stream: StreamId
    version: int
    command_id: CommandId
    at: str
    record: EventRecord

    @property
    def event_id(self) -> EventId:
        return EventId.of(self.stream, self.version)

    def decode(self) -> Event:
        return from_record(self.record)

    def to_json(self) -> dict[str, Any]:
        """`autodev events` が書き出す形。"""
        return {
            "seq": self.seq,
            "stream": self.stream.value,
            "version": self.version,
            "type": self.record.type,
            "v": self.record.v,
            "command_id": self.command_id.value,
            "at": self.at,
            "data": self.record.data,
        }


_COLUMNS = "seq, stream, version, type, v, command_id, at, data"


def _row(row: tuple[Any, ...]) -> StoredEvent:
    seq, stream, version, type_, v, command_id, at, data = row
    return StoredEvent(
        seq=seq,
        stream=StreamId(stream),
        version=version,
        command_id=CommandId(command_id),
        at=at,
        record=EventRecord(type=type_, v=v, data=json.loads(data)),
    )


class EventReader:
    """`events.db` を読むだけの口。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls: type[R], path: Path) -> R:
        """読むだけの接続で開く。driver が書いている間も待たずに、確定した所までを読める（WAL）。"""
        return cls(db.connect_readonly(path))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self: R) -> R:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def read_all(self, after: int = 0) -> list[StoredEvent]:
        """`after` より後ろの全イベントを seq の順に。1 回の SELECT なので、途中の追記は混ざらない。"""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM events WHERE seq > ? ORDER BY seq", (after,)
        )
        return [_row(row) for row in rows]

    def read_stream(self, stream: StreamId) -> list[StoredEvent]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM events WHERE stream = ? ORDER BY version", (stream.value,)
        )
        return [_row(row) for row in rows]

    def has_command(self, command_id: CommandId) -> bool:
        """そのコマンドが出したイベントがあるか。受け直しで 2 回目に来たコマンドを弾くのに使う。"""
        row = self._conn.execute(
            "SELECT 1 FROM events WHERE command_id = ? LIMIT 1", (command_id.value,)
        ).fetchone()
        return row is not None

    def last_seq(self) -> int:
        (seq,) = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        return seq

    def checkpoints(self) -> dict[str, int]:
        """受け手の名前 → 受け終えた seq。行の無い受け手は、まだ何も受けていない（0）。"""
        return dict(self._conn.execute("SELECT subscriber, seq FROM checkpoints").fetchall())


class EventStore(EventReader):
    """書く口。driver のメインループの 1 つの接続だけが使う。"""

    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], str] = utc_now) -> None:
        super().__init__(conn)
        #: イベントの `at` に入れる時刻。ドメインは時刻を取らないので、ここで付ける
        self.clock = clock

    @classmethod
    def open(cls, path: Path, clock: Callable[[], str] = utc_now) -> EventStore:
        """書く接続で開く。無ければファイルとテーブルを作る。"""
        return cls(db.connect(path), clock)

    @property
    def connection(self) -> sqlite3.Connection:
        """同じ接続で `requests` を読み書きするため（書く接続を 2 つにしない）。"""
        return self._conn

    def append(
        self,
        command_id: CommandId,
        stream: StreamId,
        expected_version: int,
        events: Sequence[Event],
    ) -> list[StoredEvent]:
        """1 つのコマンドが出したイベントを、1 つのトランザクションで追記する。

        途中で落ちたら（版の食い違い・JSON にできない値など）、1 つも残さない。`expected_version` は
        handle した時点の集約の版で、追記するイベントは `expected_version + 1` から番号を振る。
        """
        if not events:
            return []
        at = self.clock()
        stored: list[StoredEvent] = []
        with db.transaction(self._conn):
            (current,) = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM events WHERE stream = ?", (stream.value,)
            ).fetchone()
            if current != expected_version:
                raise VersionConflict(
                    f"{stream} の版は {current} で、集約は {expected_version} だと思っている"
                )
            for offset, event in enumerate(events, start=1):
                record = to_record(event)
                version = expected_version + offset
                cursor = self._conn.execute(
                    "INSERT INTO events (stream, version, type, v, command_id, at, data)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        stream.value,
                        version,
                        record.type,
                        record.v,
                        command_id.value,
                        at,
                        json.dumps(record.data, ensure_ascii=False, sort_keys=True),
                    ),
                )
                seq = cursor.lastrowid
                assert seq is not None
                stored.append(StoredEvent(seq, stream, version, command_id, at, record))
        return stored

    def set_checkpoint(self, subscriber: str, seq: int) -> None:
        self.set_checkpoints({subscriber: seq})

    def set_checkpoints(self, seqs: Mapping[str, int]) -> None:
        """受け手ごとのチェックポイントを、1 つのトランザクションでまとめて書く。

        synchronous=NORMAL（`db.connect`）なので確定のたびの書き切り（fsync）は待たないが、確定
        1 回ごとに WAL へ書き足す。受け手ごとに確定すると、イベント 1 つにつき受け手の数だけ書き足す。
        """
        if not seqs:
            return
        with db.transaction(self._conn):
            self._conn.executemany(
                "INSERT INTO checkpoints (subscriber, seq) VALUES (?, ?)"
                " ON CONFLICT (subscriber) DO UPDATE SET seq = excluded.seq",
                list(seqs.items()),
            )


def replay(
    history: Iterable[tuple[StoredEvent, Event]], factory: AggregateFactory
) -> dict[StreamId, Aggregate]:
    """読んだイベントを、ストリームごとの集約に当てる。`apply` だけを通り、判断は走らせない。"""
    aggregates: dict[StreamId, Aggregate] = {}
    for stored, event in history:
        aggregate = aggregates.get(stored.stream)
        if aggregate is None:
            aggregate = aggregates[stored.stream] = factory(stored.stream)
        if stored.version != aggregate.version + 1:
            raise CorruptStream(
                f"{stored.stream} の版が飛んでいる: {aggregate.version} の次が {stored.version}"
            )
        aggregate.apply(event, stored.command_id)
    return aggregates


def decoded(events: Iterable[StoredEvent]) -> list[tuple[StoredEvent, Event]]:
    return [(stored, stored.decode()) for stored in events]
