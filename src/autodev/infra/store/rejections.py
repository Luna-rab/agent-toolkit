"""拒んだコマンドの記録。

コマンドは正本に入れないので、拒んだことはここにしか残らない。`requests` から来た要求は、拒んだ
理由をその場で返せない（足した CLI はもう終わっている）ので、`autodev status` がここを読んで出す。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ...domain import codec
from ...domain.commands.base import Command


@dataclass(frozen=True)
class Rejection:
    at: str
    #: コマンドの名前（読めなかった要求なら、要求の `type` の列）
    type: str
    command_id: str
    #: 出した者（`Issuer` の JSON）
    issuer: dict[str, Any]
    reason: str

    @classmethod
    def of(cls, command: Command, reason: str, at: str) -> Rejection:
        return cls(
            at=at,
            type=type(command).__name__,
            command_id=command.command_id.value,
            issuer=codec.to_json(command.issuer),
            reason=reason,
        )


class RejectionLog:
    """`logs/rejected.jsonl` に 1 行ずつ足す。

    同じ command_id と理由の組は 1 行だけにする。落ちた後の配り直しで、ポリシーは同じ id の
    コマンドをもう一度出し、同じ理由でもう一度拒まれる。そのたびに足すと、`autodev status` に同じ
    拒否が並ぶ。command_id だけで重なりを見てはいけない。統括の判断の id は知らせ 1 つに 1 つなので、
    同じ知らせで差し戻されて出し直した判断はどれも同じ id になり、2 回目からの拒否が残らなくなる。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._recorded = {
            (entry.get("command_id"), entry.get("reason")) for entry in read_rejections(path)
        }

    def __call__(self, rejection: Rejection) -> None:
        key = (rejection.command_id, rejection.reason)
        if key in self._recorded:
            return
        self._recorded.add(key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rejection), ensure_ascii=False, sort_keys=True) + "\n")


def read_rejections(path: Path) -> list[dict[str, Any]]:
    """書いた順に。書きかけの最後の行（落ちた途中）は読み飛ばす。"""
    if not path.is_file():
        return []
    found: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            found.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return found
