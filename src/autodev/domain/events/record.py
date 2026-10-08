"""イベントの保存と読み出し。イベントストアの 1 行とイベントを行き来し、古い版を読み替える。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import codec
from .base import Event
from .registry import EVENT_TYPES


@dataclass(frozen=True)
class EventRecord:
    """イベントストアの 1 行のうち、イベントの中身にあたる列（`type`・`v`・`data`）。"""

    type: str
    v: int
    data: dict[str, Any]


#: 古い版の中身を、新しい形に読み替える処理。名前を変えることもあるので、(名前, 版, 中身) を返す
Upcaster = Callable[[dict[str, Any]], tuple[str, int, dict[str, Any]]]

#: (イベントの名前, 版) → 読み替える処理。今は読み替えるものが無い
UPCASTERS: Mapping[tuple[str, int], Upcaster] = {}


class UnknownEventType(ValueError):
    """イベントストアに、この driver が知らないイベントがある。"""


def to_record(event: Event) -> EventRecord:
    name = type(event).__name__
    if EVENT_TYPES.get(name) is not type(event):
        raise UnknownEventType(f"表に無いイベント: {name}")
    return EventRecord(type=name, v=type(event).VERSION, data=codec.to_json(event))


def from_record(
    record: EventRecord, upcasters: Mapping[tuple[str, int], Upcaster] = UPCASTERS
) -> Event:
    """古い版はアップキャスタで今の版に読み替えてから読む。今の版より新しい版は読めない。"""
    name, v, data = record.type, record.v, record.data
    seen: set[tuple[str, int]] = set()
    while True:
        cls = EVENT_TYPES.get(name)
        if cls is not None and v == cls.VERSION:
            return codec.from_json(cls, data)
        if cls is not None and v > cls.VERSION:
            raise UnknownEventType(
                f"{name} の版 {v} はこの driver より新しい（{cls.VERSION} まで）"
            )
        upcaster = upcasters.get((name, v))
        if upcaster is None:
            raise UnknownEventType(f"{name} の版 {v} を読み替える処理が無い")
        # 読み替えが輪になっていたら、いつまでも終わらない
        seen.add((name, v))
        name, v, data = upcaster(data)
        if (name, v) in seen:
            raise UnknownEventType(f"{name} の版 {v} への読み替えが輪になっている")
