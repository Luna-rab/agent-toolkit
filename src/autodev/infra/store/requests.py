"""driver の外からの要求（Inbox パターン）。

`/autodev` の CLI（`autodev answer` など）は `requests` に 1 行足す。driver が止まっていても足せて、
次に起きたときに拾われる。driver は拾ったものをコマンドとして処理し、行を消す。

1 行は 1 つのコマンドで、`type` にコマンドの名前、`data` に `command_id` と `issuer` を除いた欄を
入れる。どちらも driver が付ける。

- `command_id` は行の id。処理した後・消す前に落ちても、拾い直したときに同じ id のイベントが
  あるので 2 回処理しない
- `issuer` は CLI に決める。行の中身で名乗らせない
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from ...domain import codec
from ...domain.commands.base import Command
from ...domain.commands.registry import COMMAND_TYPES
from ...domain.value_objects.base import InvalidValue
from ...domain.value_objects.issuer import Issuer
from ...domain.value_objects.issuer_kind import IssuerKind
from ..files import utc_now
from . import db

#: driver が付ける欄。行の中身に書いてあったら拒む
_DRIVER_FIELDS = frozenset({"command_id", "issuer"})


class UnreadableRequest(ValueError):
    """要求の行を、コマンドとして読めない。"""


@dataclass(frozen=True)
class Request:
    id: str
    type: str
    data: str
    at: str

    def to_command(self) -> Command:
        cls = COMMAND_TYPES.get(self.type)
        if cls is None:
            raise UnreadableRequest(f"知らないコマンド: {self.type}")
        try:
            fields = json.loads(self.data)
        except json.JSONDecodeError as e:
            raise UnreadableRequest(f"{self.type} の中身が JSON でない: {e}") from e
        if not isinstance(fields, dict):
            raise UnreadableRequest(f"{self.type} の中身が object でない")
        if claimed := sorted(_DRIVER_FIELDS & set(fields)):
            raise UnreadableRequest(f"{self.type} の中身に driver が付ける欄がある: {claimed}")
        fields |= {"command_id": self.id, "issuer": codec.to_json(Issuer.cli())}
        try:
            return codec.from_json(cls, fields)
        except (codec.DecodeError, InvalidValue) as e:
            raise UnreadableRequest(str(e)) from e


class RequestBox:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], str] = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    @classmethod
    def open(cls, path: Path) -> RequestBox:
        """driver の外（CLI）から足すときに開く。driver の書く接続とは別のプロセスの接続になる。

        無いランの events.db は作らずに FileNotFoundError にする（`build_status` と同じ扱い）。
        """
        return cls(db.connect(path, create=False))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> RequestBox:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def put(self, command_type: type[Command], fields: Mapping[str, Any]) -> str:
        """要求を 1 行足し、その id（= コマンドの id）を返す。`fields` は値オブジェクトのままでよい。

        足す前に、CLI が出してよいコマンドか（ドメインの `ISSUERS`）と、コマンドとして読めるかを
        確かめる。どちらかに外れた要求を足すと、driver が拾った所で初めて拒まれ、足した側は気づけない。
        """
        if command_type.__name__ not in COMMAND_TYPES:
            raise UnreadableRequest(f"知らないコマンド: {command_type.__name__}")
        if IssuerKind.CLI not in command_type.ISSUERS:
            raise UnreadableRequest(f"{command_type.__name__} は CLI から出せない")
        request = Request(
            id=f"request/{uuid.uuid4().hex}",
            type=command_type.__name__,
            data=json.dumps(
                {key: codec.to_json(value) for key, value in fields.items()},
                ensure_ascii=False,
                sort_keys=True,
            ),
            at=self._clock(),
        )
        request.to_command()
        with db.transaction(self._conn):
            self._conn.execute(
                "INSERT INTO requests (id, type, data, at) VALUES (?, ?, ?, ?)",
                (request.id, request.type, request.data, request.at),
            )
        return request.id

    def pending(self) -> list[Request]:
        """足された順に。"""
        rows = self._conn.execute("SELECT id, type, data, at FROM requests ORDER BY rowid")
        return [Request(*row) for row in rows]

    def next(self) -> Request | None:
        row = self._conn.execute(
            "SELECT id, type, data, at FROM requests ORDER BY rowid LIMIT 1"
        ).fetchone()
        return None if row is None else Request(*row)

    def delete(self, request_id: str) -> None:
        with db.transaction(self._conn):
            self._conn.execute("DELETE FROM requests WHERE id = ?", (request_id,))
