"""driver の外からの要求（`infra/store/requests.py`）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from autodev_fakes import execution, task

from autodev.domain.commands.run import StartTask
from autodev.domain.commands.task import ResumeStage
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.issuer import Issuer
from autodev.infra.store.eventstore import EventStore
from autodev.infra.store.requests import Request, RequestBox, UnreadableRequest


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "events.db"


def test_driverが止まっていても足せて後から拾える(path: Path):
    EventStore.open(path).close()
    with RequestBox.open(path) as box:
        first = box.put(ResumeStage, {"task": task(1), "execution": execution(1)})
        second = box.put(ResumeStage, {"task": task(2), "execution": execution(2)})
    with EventStore.open(path) as store:
        box = RequestBox(store.connection)
        assert [r.id for r in box.pending()] == [first, second]
        request = box.next()
        assert request is not None
        assert request.id == first
        command = request.to_command()
        assert command == ResumeStage(
            command_id=CommandId(first), issuer=Issuer.cli(), task=task(1), execution=execution(1)
        )
        box.delete(first)
        assert [r.id for r in box.pending()] == [second]


def test_出した者とidはdriverが付け中身では名乗れない():
    claimed = Request(
        "request/1", "StartTask", '{"task": "task1", "issuer": {"kind": "policy"}}', "x"
    )
    with pytest.raises(UnreadableRequest, match="driver が付ける欄"):
        claimed.to_command()


@pytest.mark.parametrize(
    ("type_", "data", "match"),
    [
        ("Nope", "{}", "知らないコマンド"),
        ("StartTask", "{", "JSON でない"),
        ("StartTask", "[]", "object でない"),
        ("StartTask", "{}", "task が無い"),
        ("StartTask", '{"task": "x"}', "TaskId の形が違う"),
    ],
)
def test_読めない要求を見分ける(type_: str, data: str, match: str):
    with pytest.raises(UnreadableRequest, match=match):
        Request("request/1", type_, data, "x").to_command()


def test_読めない要求とCLIが出せないコマンドは足す前に拒む(path: Path):
    EventStore.open(path).close()
    with RequestBox.open(path) as box:
        with pytest.raises(UnreadableRequest, match="TaskId の形が違う"):
            box.put(ResumeStage, {"task": "x", "execution": execution(1)})
        # StartTask を出せるのはポリシーだけ
        with pytest.raises(UnreadableRequest, match="StartTask は CLI から出せない"):
            box.put(StartTask, {"task": task(1)})
        assert box.pending() == []


def test_無いランには足さずに落ちる(path: Path):
    with pytest.raises(FileNotFoundError):
        RequestBox.open(path)
    assert not path.exists()
