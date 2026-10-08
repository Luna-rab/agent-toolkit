from __future__ import annotations

import re

from .base import Text
from .event_id import EventId


class CommandId(Text):
    """コマンドの id。

    ポリシーと反応が出すものは `derived` で、受けたイベントの id と自分の名前から決める。受け直しで
    同じコマンドがもう一度出ても同じ id になり、2 回目を弾ける。
    """

    PATTERN = re.compile(r"\S+")

    @classmethod
    def derived(cls, event: EventId, source: str, index: int = 0) -> CommandId:
        """1 つのイベントから同じ受け手が複数のコマンドを出すときは、`index` で分ける。"""
        return cls(f"{event}/{source}/{index}")
