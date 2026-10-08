"""ポリシー: 集約どうしのつなぎ目（イベント → 次のコマンドの一覧）。

ポリシーは、受けたイベントとその id（どのストリームの何番目か）だけからコマンドを返す純粋な関数で、
集約の状態を読まない。状態で決まることは、受けた集約が `handle` で決める（取り出せる仕事が無ければ
何もしない、など）。正本は `test/autodev/test_seams.py` の `SEAMS` で、ここはその表を行ごとに書き
写したもの。行の名前（`Policy.name`）は表の名前と同じで、受け手の名前・チェックポイントの鍵・出す
コマンドの id（`CommandId.derived`）に入る。名前を変えると、変える前のイベントに反応しなくなる。

出す者（`By`）は 3 つある:

- `POLICY`: ポリシー
- `SUPERVISOR`: 計画タスクと git 管理タスクの統括（プログラム）。決まった並びを組むだけなので、
  ポリシーと同じ形で書く。出すコマンド（`AcceptFlow`・`EscalateToRun`）はタスクの統括にしか出せない
- `REACTION`: 反応の続き。反応が副作用を済ませてから、ここで組んだコマンドを返す（`FOLLOW_UPS`）

アプリケーション層は `RECEIVERS`（名前 → 受ける関数）を、この順に受け手として登録するだけである。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..aggregates.review_ledger import JudgeCapability
from ..commands.base import Command
from ..events.base import Event
from ..services.escalation_router import task_of_stream
from ..value_objects.command_id import CommandId
from ..value_objects.event_id import EventId
from ..value_objects.issuer import Issuer
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId

_GIT = TaskId.git()


class By(Enum):
    """コマンドを出す者。"""

    POLICY = "policy"
    #: 計画タスクと git 管理タスクの統括（プログラム）
    SUPERVISOR = "supervisor"
    #: 反応が副作用を済ませた後の続き
    REACTION = "reaction"


@dataclass
class Stamp:
    """1 つのイベントから 1 つの行が出すコマンドに、出す順に id と出した者を振る。

    id は受けたイベントの id と行の名前と順番で決まるので、配り直しで同じコマンドがもう一度出ても
    同じ id になり、2 回目は何もしない（メインループと集約の土台が弾く）::

        return [StartTask(**stamp(), task=planning), StartTask(**stamp(), task=git)]
    """

    name: str
    source: EventId
    by: By
    _count: int = field(default=0, init=False)

    def __call__(self, supervisor: TaskId | None = None) -> dict[str, Any]:
        """次のコマンドの `command_id` と `issuer`。統括（プログラム）は名乗るタスクを渡す。"""
        command_id = CommandId.derived(self.source, self.name, self._count)
        self._count += 1
        if self.by is By.SUPERVISOR:
            if supervisor is None:
                raise ValueError(f"{self.name}: 統括が出すコマンドには、名乗るタスクが要る")
            issuer = Issuer.task_supervisor(supervisor)
        elif self.by is By.REACTION:
            issuer = Issuer.reaction(self.name, self.source)
        else:
            issuer = Issuer.policy(self.name, self.source)
        return {"command_id": command_id, "issuer": issuer}


#: 1 行の中身。受けたイベント・その id・id を振る道具から、出すコマンドの一覧を返す
Rule = Callable[[Any, EventId, Stamp], list[Command]]


@dataclass(frozen=True)
class Policy:
    """表の 1 行。"""

    name: str
    on: tuple[type[Event], ...]
    rule: Rule
    by: By = By.POLICY

    def receive(self, event: Event, source: EventId) -> list[Command]:
        """受けたイベントから、処理してほしいコマンドの一覧（受けないイベントなら空）。"""
        if not isinstance(event, self.on):
            return []
        return self.rule(event, source, Stamp(self.name, source, self.by))


def source_task(source: EventId) -> TaskId:
    """イベントを出したタスク（task/<TaskId> のストリーム）。"""
    return task_of_stream(source.stream)


def is_from_task(source: EventId) -> bool:
    return source.stream.is_task


def is_from_git(source: EventId) -> bool:
    """git 管理タスクのストリームのイベントか。"""
    return source.stream == StreamId.task(_GIT)


def review_stream_of(task: TaskId) -> StreamId:
    return JudgeCapability.ledger_of(task)
