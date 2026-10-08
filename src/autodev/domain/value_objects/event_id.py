from __future__ import annotations

import re

from .base import Text
from .stream_id import StreamId


class EventId(Text):
    """イベントの id。`<StreamId>#<ストリームの中の版>`。

    ストリームと版の組は一意（`events` の UNIQUE）なので、乱数を使わずに決まる。集約は `handle` の中で、
    これから出すイベントの id を知ることができる（エスカレーションの id にする）。
    """

    PATTERN = re.compile(r"[a-z]+(?:/[a-z0-9]+)?#[1-9][0-9]*")

    def _check(self) -> None:
        StreamId(self.value.split("#", 1)[0])

    @classmethod
    def of(cls, stream: StreamId, version: int) -> EventId:
        return cls(f"{stream}#{version}")

    @property
    def stream(self) -> StreamId:
        return StreamId(self.value.split("#", 1)[0])

    @property
    def version(self) -> int:
        return int(self.value.split("#", 1)[1])
