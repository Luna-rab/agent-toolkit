"""集約の検査で使う、メインループの代わりと、出す者の見本。

I/O をしない。コマンドを handle に渡し、返ったイベントを当て、当てたイベントの列を残す。残した列を
再生すると同じ状態になることも、これで確かめる。
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from typing import Generic, TypeVar

from autodev.domain.aggregates.base import Aggregate
from autodev.domain.commands.base import Command
from autodev.domain.events.base import Event
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.session_id import SessionId

SESSION = SessionId("0f8fad5b-d9cb-469f-a165-70867728950e")
RUN_SUPERVISOR = Issuer.run_supervisor(SESSION)
CLI = Issuer.cli()
DRIVER = Issuer.driver()
POLICY = Issuer.policy("test-policy", EventId("run#1"))

_ids = itertools.count(1)


def new_id() -> CommandId:
    return CommandId(f"c{next(_ids)}")


A = TypeVar("A", bound=Aggregate)


class Loop(Generic[A]):
    """メインループの代わり。handle で決めたイベントを当てる。"""

    def __init__(self, aggregate: A) -> None:
        self.aggregate = aggregate
        self.history: list[tuple[Event, CommandId]] = []

    def __call__(self, command: Command) -> list[Event]:
        events = self.aggregate.handle(command)
        for event in events:
            self.aggregate.apply(event, command.command_id)
            self.history.append((event, command.command_id))
        return events

    @property
    def events(self) -> list[Event]:
        return [event for event, _ in self.history]

    def replayed(self) -> A:
        return type(self.aggregate).replay(self.aggregate.stream, self.history)


def of_type(events: Iterable[Event], cls: type[Event]) -> list:
    return [event for event in events if isinstance(event, cls)]


def names(events: Iterable[Event]) -> list[str]:
    return [type(event).__name__ for event in events]
