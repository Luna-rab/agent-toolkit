"""ドメインイベント。

イベントは起きたことで、過去形で名付ける。各イベントは形の版（`VERSION`）を持つ。形を変えたら
`VERSION` を上げ、古い版を新しい版に読み替えるアップキャスタを `UPCASTERS` に足す。古いランの
`events.db` も再生できるようにするためである。

**作り直しを最初に出す（配る）までは、版を上げない。** それまでに作られた `events.db` は検査の中の
ものだけで、読み替える古いランが無い。形を変えるときは、欄を足して既定値を持たせるか、そのまま
直す。今は読み替えるものが無い。

イベントストアへの保存は `to_record`、読み出しは `from_record` を通す。名前 → クラスの表は
`EVENT_TYPES`、どの集約がどのイベントを出すか（apply に書くイベント）は `EVENTS_BY_AGGREGATE`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class Event:
    """ドメインイベントの土台。"""

    #: 形の版。形を変えたら上げて、アップキャスタを足す
    VERSION: ClassVar[int] = 1
