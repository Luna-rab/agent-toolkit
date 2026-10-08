"""ポリシー（`domain/policies/`）が、つなぎ目の表（`test_seams.py` の `SEAMS`）どおりに動くか。

`test_seams.py` の流れを、表の代わりに本物のメインループ（`app/driving/mainloop.py`）とイベントストアで通す。
ポリシーはドメインの一覧（`RECEIVERS`）をそのまま受け手として登録し、外からの入力（実行器・LLM の
統括・CLI・driver）は流れの台本のまま、偽の受け手として登録する。受け手に配ったイベントごとに、
ポリシーが返したコマンドが、表の行が組むコマンドと（id と出した者まで）同じかを確かめる。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
import test_seams as seams

from autodev.app.driving.mainloop import Delivery, LoopExit, MainLoop, Subscriber
from autodev.domain.aggregates.base import Aggregate
from autodev.domain.commands.base import Command
from autodev.domain.commands.review_ledger import RaiseFinding
from autodev.domain.commands.task import Escalate
from autodev.domain.events.base import Event
from autodev.domain.events.design import DesignAmbiguous, DesignRoundsExhausted
from autodev.domain.events.review_ledger import FindingCarried
from autodev.domain.policies.base import By, Policy
from autodev.domain.policies.registry import FOLLOW_UPS, POLICIES, RECEIVERS
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.finding_id import FindingId
from autodev.domain.value_objects.finding_target import FindingTarget
from autodev.domain.value_objects.issuer_kind import IssuerKind
from autodev.domain.value_objects.rating import Rating
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.infra.store.eventstore import EventStore
from autodev.infra.store.rejections import Rejection

SEAMS = {seam.name: seam for seam in seams.SEAMS}
ROWS: dict[str, Policy] = {policy.name: policy for policy in (*POLICIES, *FOLLOW_UPS.values())}


def expected(seam: seams.Seam, event: Event, source: EventId) -> list[Command]:
    """表の行が組むコマンド（test_seams の World と同じ id と出した者）。"""
    if not isinstance(event, seam.on):
        return []
    return [
        cls(
            command_id=CommandId.derived(source, seam.name, index),
            issuer=seams._issuer(seam, source, fields),
            **fields,
        )
        for index, (cls, fields) in enumerate(seam.build(event, source))
    ]


class LoopWorld(seams.World):
    """test_seams の World を、本物のメインループとイベントストアで動かす。"""

    #: イベントストアを置く場所（検査ごとに tmp_path を入れる）
    directory: ClassVar[Path]
    #: 受け手に同じイベントを 2 回配ったことにする（返したコマンドを処理した後に、もう一度返す）
    twice: ClassVar[bool] = False
    #: 表どおりと確かめた行（空でないコマンドを返したもの）。流れをまたいで集める
    compared: ClassVar[Counter[str]] = Counter()

    def __init__(self, *actors: seams.Actor, script: seams.Script | None = None) -> None:
        super().__init__(*actors, script=script)
        self.store = EventStore.open(self.directory / f"events-{len(seams.World.created)}.db")
        subscribers = [Subscriber("observer", self._observe)]
        subscribers += [Subscriber(name, self._policy(ROWS[name])) for name in RECEIVERS]
        # 反応の偽物: answers/<tool_use_id>.json を書いたことにして、ドメインが組む続きを返す
        subscribers += [Subscriber(name, self._policy(row)) for name, row in FOLLOW_UPS.items()]
        subscribers += [
            Subscriber(f"actor-{index}", self._actor(actor)) for index, actor in enumerate(actors)
        ]
        self.actors = []
        self.loop = MainLoop(self.store, seams.factory, subscribers, on_rejected=self._rejected)

    # --- 集約とコマンド ---

    def get(self, stream: StreamId) -> Aggregate:
        found = self.loop.aggregates.get(stream)
        return found if found is not None else seams.factory(stream)

    def process(self, command: Command) -> list[Event]:
        before = self.store.last_seq()
        self.loop.process(command)
        return [stored.decode() for stored in self.store.read_all(before)]

    def pump(self) -> None:
        assert self.loop.run().exit is LoopExit.IDLE

    @staticmethod
    def _rejected(rejection: Rejection) -> None:
        name = rejection.issuer.get("name") or rejection.issuer.get("kind")
        raise AssertionError(f"{rejection.type}（{name}）を拒んだ: {rejection.reason}")

    # --- 受け手 ---

    def _observe(self, delivery: Delivery) -> list[Command]:
        """実行器の代わり: 走らせると決めた・続きから再開した実行を覚える。"""
        event = delivery.event
        self.log.append((delivery.event_id, event))
        if isinstance(event, seams.StageRequested):
            self.requested.append(event.execution)
        if isinstance(event, seams.StageStarted) and self._was_started_before(event):
            self.resumed.append(event.execution)
        return []

    def _policy(self, row: Policy) -> Callable[[Delivery], Iterator[Command]]:
        seam = SEAMS[row.name]

        def receive(delivery: Delivery) -> Iterator[Command]:
            event, source = delivery.event, delivery.event_id
            commands = row.receive(event, source)
            assert commands == expected(seam, event, source), f"{row.name} が表と違う"
            if commands:
                self.fired[row.name] += 1
                LoopWorld.compared[row.name] += 1
            yield from commands
            if not self.twice:
                return
            # 返したコマンドを処理し終えた後に、同じイベントをもう一度配られた（チェックポイントを
            # 進める前に落ちた）。同じ id のコマンドなので、何も起きない
            before = self.store.last_seq()
            yield from row.receive(event, source)
            assert self.store.last_seq() == before, f"{row.name} のコマンドを 2 回処理した"

        return receive

    def _actor(self, actor: seams.Actor) -> Callable[[Delivery], list[Command]]:
        def receive(delivery: Delivery) -> list[Command]:
            return actor(self, delivery.event_id, delivery.event)

        return receive


@pytest.fixture
def loop_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> type[LoopWorld]:
    monkeypatch.setattr(LoopWorld, "directory", tmp_path, raising=False)
    monkeypatch.setattr(seams, "World", LoopWorld)
    return LoopWorld


FLOW_IDS = [getattr(flow, "__name__", str(flow)).removeprefix("test_") for flow in seams.FLOWS]


# --- 表と一覧の対応 ---


def test_表の行とポリシーは名前と受けるイベントと出す者が1対1で対応する():
    assert set(ROWS) == set(SEAMS)
    # 受け手として登録する並びも、表の並び（同じイベントは登録した順に受け手へ渡す）
    assert [p.name for p in POLICIES] == [s.name for s in seams.SEAMS if s.by != "reaction"]
    for name, row in ROWS.items():
        seam = SEAMS[name]
        on = seam.on if isinstance(seam.on, tuple) else (seam.on,)
        assert row.on == on, name
        assert row.by.value == seam.by, name


def test_受け手の一覧は反応の続きを除いた行で名前が重ならない():
    assert set(RECEIVERS) == {policy.name for policy in POLICIES}
    assert len(RECEIVERS) == len(POLICIES)
    assert not set(RECEIVERS) & set(FOLLOW_UPS)
    assert all(row.by is By.REACTION for row in FOLLOW_UPS.values())
    assert all(policy.by is not By.REACTION for policy in POLICIES)


# --- 流れ ---


@pytest.mark.parametrize("flow", seams.FLOWS, ids=FLOW_IDS)
def test_ポリシーを登録したメインループで流れが最後まで進む(
    loop_world: type[LoopWorld], flow: Callable[[], None]
):
    flow()


@pytest.mark.parametrize("flow", seams.FLOWS, ids=FLOW_IDS)
def test_同じイベントを2回配ってもコマンドは1回だけ処理する(
    loop_world: type[LoopWorld], monkeypatch: pytest.MonkeyPatch, flow: Callable[[], None]
):
    monkeypatch.setattr(LoopWorld, "twice", True)
    flow()


def test_表の行はどれも流れの中で表どおりのコマンドを返した(loop_world: type[LoopWorld]):
    LoopWorld.compared.clear()
    for flow in seams.FLOWS:
        flow()
    assert set(ROWS) - set(LoopWorld.compared) == set(seams.NOT_IN_FLOWS)


# --- 流れで通らない行 ---

PLANNING_JUDGE = ExecutionId(TaskId.planning(), StageKind.DESIGN_JUDGE, 2, 1)
DESIGN_EVENT = EventId.of(StreamId.design(), 7)


@pytest.mark.parametrize(
    ("name", "event", "kind"),
    [
        ("escalate-ambiguous", DesignAmbiguous(PLANNING_JUDGE), EscalationKind.DESIGN_AMBIGUOUS),
        (
            "escalate-rounds-exhausted",
            DesignRoundsExhausted(3, PLANNING_JUDGE),
            EscalationKind.DESIGN_ROUNDS_EXHAUSTED,
        ),
    ],
)
def test_設計の回答待ちは計画タスクから判定した実行を添えて上げる(
    name: str, event: Event, kind: EscalationKind
):
    (command,) = RECEIVERS[name](event, DESIGN_EVENT)
    assert command == expected(SEAMS[name], event, DESIGN_EVENT)[0]
    assert isinstance(command, Escalate)
    assert (command.task, command.kind, command.origin) == (TaskId.planning(), kind, PLANNING_JUDGE)
    assert command.command_id == CommandId.derived(DESIGN_EVENT, name, 0)
    assert command.issuer.kind is IssuerKind.POLICY
    assert (command.issuer.name, command.issuer.event) == (name, DESIGN_EVENT)


def test_受けないイベントには何も返さない():
    other: Any = DesignAmbiguous(PLANNING_JUDGE)
    answered = {name: RECEIVERS[name](other, DESIGN_EVENT) for name in RECEIVERS}
    assert [name for name, commands in answered.items() if commands] == ["escalate-ambiguous"]


def test_移した指摘は移す元の宛先のまま移した先に立てる():
    source = EventId("review/task2#4")
    event = FindingCarried(
        FindingId("R1"),
        TaskId("task3"),
        Rating.MUST_FIX,
        "テストが無い",
        target=FindingTarget.TESTS,
    )
    (command,) = RECEIVERS["raise-carried"](event, source)
    assert isinstance(command, RaiseFinding)
    assert command.finding_target is FindingTarget.TESTS
    assert command == expected(SEAMS["raise-carried"], event, source)[0]
