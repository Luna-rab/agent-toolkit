"""イベントとコマンドの名前の表と、JSON との往復（`domain/events/`・`domain/commands/`・`domain/codec.py`）。"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
from dataclasses import dataclass, field
from typing import Any

import pytest
from autodev_samples import sample

from autodev.domain import codec, commands, events
from autodev.domain.commands.base import Command
from autodev.domain.commands.design import DesignCommand
from autodev.domain.commands.questions import QuestionsCommand
from autodev.domain.commands.registry import COMMAND_TYPES, COMMANDS_BY_AGGREGATE
from autodev.domain.commands.review_ledger import ReviewCommand
from autodev.domain.commands.run import RunCommand
from autodev.domain.commands.stack import StackCommand
from autodev.domain.commands.task import ConcludeReviewRound, TaskCommand
from autodev.domain.events.base import Event
from autodev.domain.events.record import EventRecord, UnknownEventType, from_record, to_record
from autodev.domain.events.registry import EVENT_TYPES, EVENTS_BY_AGGREGATE
from autodev.domain.events.review_ledger import (
    FindingCarried,
    FindingRaised,
    FindingRetargeted,
    FixCounted,
)
from autodev.domain.events.run import (
    AllTasksSettled,
    RunPanicked,
    SettledPlanRecorded,
    TasksPlanned,
    TaskStarted,
)
from autodev.domain.events.task import RoundConcluded
from autodev.domain.flow.flow import Cursor
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.artifact_ref import ArtifactRef
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.finding_id import FindingId
from autodev.domain.value_objects.finding_route import FindingRoute
from autodev.domain.value_objects.finding_target import FindingTarget
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.rating import Rating
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_kind import TaskKind
from autodev.domain.value_objects.verify_command import VerifyCommand

#: 宛先の集約ごとの土台。表には入れない
COMMAND_BASES = {
    Command,
    RunCommand,
    TaskCommand,
    ReviewCommand,
    DesignCommand,
    StackCommand,
    QuestionsCommand,
}


def defined_in(package: Any, base: type) -> set[type]:
    """パッケージの中のモジュールで定義した、`base` の部分クラス。"""
    found: set[type] = set()
    for info in pkgutil.iter_modules(package.__path__, prefix=f"{package.__name__}."):
        module = importlib.import_module(info.name)
        found |= {
            obj
            for obj in vars(module).values()
            if inspect.isclass(obj) and issubclass(obj, base) and obj.__module__ == module.__name__
        }
    return found


def test_eventsで定義したイベントはすべて名前の表にある():
    assert defined_in(events, Event) - {Event} == set(EVENT_TYPES.values())
    assert all(name == cls.__name__ for name, cls in EVENT_TYPES.items())


def test_commandsで定義したコマンドはすべて名前の表にある():
    assert defined_in(commands, Command) - COMMAND_BASES == set(COMMAND_TYPES.values())
    assert all(name == cls.__name__ for name, cls in COMMAND_TYPES.items())


def test_補足で足したイベントがあり名前を変えた古いイベントは無い():
    for name in (
        "TaskStatusChanged",
        "AllTasksSettled",
        "SettledPlanRecorded",
        "DesignProposalAbandoned",
        "DesignRevisionStarted",
        "RunResumed",
        "TaskOpened",
        "StageRequested",
        "ExecutionRestarted",
    ):
        assert name in EVENT_TYPES
    assert "AllTasksStacked" not in EVENT_TYPES


def test_補足で足したコマンドがある():
    for name in (
        "UpdateTaskStatus",
        "RecordSettledPlan",
        "DiscardProposal",
        "MarkInterrupted",
        "OpenTask",
        "RecordJudgement",
        "RecordFindings",
        "ConfirmHandoff",
        "ResumeInterrupted",
        "EnqueueGitJob",
        "ConcludeGateRound",
    ):
        assert name in COMMAND_TYPES
    # --resume で続けられなかったかは、証拠の事実（resumed・initialized など）から Task が決める
    assert "RestartExecution" not in COMMAND_TYPES
    # 次のステージは Task が決める。止めを外すのは ConcludeGateRound だけ
    assert "RequestStage" not in COMMAND_TYPES
    # 破棄は再計画の反映（ApplyReplan）の中だけで行う。判定の締めは判定と一緒に届く
    assert "DiscardTasks" not in COMMAND_TYPES
    assert "EvaluateStall" not in COMMAND_TYPES


def test_複数の集約に属するのはエスカレーションと結果の受け渡しの知らせだけ():
    # Run の側のエスカレーションも EscalationClosed で閉じる。ステージの結果を受け取る
    # 側は、どれも同じ形で受けた／受けられないを返す
    owners: dict[type[Event], list[str]] = {}
    for aggregate, classes in EVENTS_BY_AGGREGATE.items():
        assert len(set(classes)) == len(classes), aggregate
        for cls in classes:
            owners.setdefault(cls, []).append(aggregate)
    shared = {cls.__name__: names for cls, names in owners.items() if len(names) > 1}
    receivers = ["ReviewLedger", "Design", "Stack"]
    assert shared == {
        "EscalationRaised": ["Run", "Task"],
        "EscalationClosed": ["Run", "Task"],
        "ResultReceived": receivers,
        "ResultRefused": receivers,
    }


def test_コマンドは宛先の集約と出してよい者を持つ():
    for aggregate, classes in COMMANDS_BY_AGGREGATE.items():
        assert aggregate in EVENTS_BY_AGGREGATE
        for cls in classes:
            assert aggregate == cls.AGGREGATE, cls.__name__
            assert cls.ISSUERS, f"{cls.__name__} を出してよい者が無い"


@pytest.mark.parametrize("cls", list(EVENT_TYPES.values()), ids=list(EVENT_TYPES))
def test_イベントはJSONを通して往復する(cls: type[Event]):
    event = sample(cls)
    record = to_record(event)
    stored = json.loads(json.dumps(record.data, ensure_ascii=False))
    assert from_record(EventRecord(record.type, record.v, stored)) == event


@pytest.mark.parametrize("cls", list(COMMAND_TYPES.values()), ids=list(COMMAND_TYPES))
def test_コマンドはJSONを通して往復し宛先のストリームを持つ(cls: type[Command]):
    command = sample(cls)
    stored = json.loads(json.dumps(codec.to_json(command), ensure_ascii=False))
    assert codec.from_json(cls, stored) == command
    assert isinstance(command.target, StreamId)


def test_包んだ値は素の値になり集合は並べて書く():
    event = TaskStarted(
        TaskId("task2"),
        TaskKind.IMPLEMENTATION,
        blocked_by=frozenset({TaskId("task10"), TaskId("task1")}),
        artifacts=(ArtifactRef(ArtifactKind.DESIGN, "2"),),
    )
    data = to_record(event).data
    assert data["task"] == "task2"
    assert data["kind"] == "implementation"
    assert data["blocked_by"] == ["task1", "task10"]
    assert data["artifacts"] == [{"kind": "design", "at": "2"}]


def test_知らないキーと欠けたキーを読み飛ばさない():
    with pytest.raises(codec.DecodeError):
        from_record(EventRecord("RunPanicked", 1, {"cause": "429", "extra": 1}))
    with pytest.raises(codec.DecodeError):
        from_record(EventRecord("RunPanicked", 1, {}))
    with pytest.raises(codec.DecodeError):
        from_record(EventRecord("RunFinished", 1, {"ready_overview": "yes"}))


def test_値の検査に落ちた値はどこで落ちたかを付けて拒む():
    data = to_record(sample(TaskStarted)).data
    with pytest.raises(codec.DecodeError, match=r"TaskStarted\.task: TaskId の形が違う"):
        from_record(EventRecord("TaskStarted", 1, {**data, "task": "task0"}))
    with pytest.raises(codec.DecodeError, match=r"TaskStarted\.spec: タスクの件名"):
        from_record(EventRecord("TaskStarted", 1, {**data, "spec": {**data["spec"], "title": ""}}))
    with pytest.raises(codec.DecodeError, match=r"TaskMarkedStacked\.pr: int が要るところに bool"):
        from_record(EventRecord("TaskMarkedStacked", 1, {"task": "task1", "pr": True}))


def test_集合に同じ値が2つある入力を拒む():
    with pytest.raises(codec.DecodeError, match="集合に同じ値が 2 つある"):
        from_record(EventRecord("TasksStopped", 1, {"tasks": ["task1", "task1"]}))


def test_知らないイベントと新しすぎる版は読めない():
    with pytest.raises(UnknownEventType):
        from_record(EventRecord("NoSuchEvent", 1, {}))
    with pytest.raises(UnknownEventType):
        from_record(EventRecord("RunPanicked", 2, {"cause": "429"}))
    with pytest.raises(UnknownEventType):
        from_record(EventRecord("RunPanicked", 0, {"reason": "429"}))


def test_アップキャスタで古い版を今の版に読み替える():
    upcasters = {
        ("RunPanicked", 0): lambda data: ("RunPanicked", 1, {"cause": data["reason"]}),
        # 名前を変えたイベントも読み替えられる
        ("AllTasksStacked", 1): lambda data: ("AllTasksSettled", 1, data),
    }
    assert from_record(EventRecord("RunPanicked", 0, {"reason": "429"}), upcasters) == RunPanicked(
        "429"
    )
    assert from_record(EventRecord("AllTasksStacked", 1, {}), upcasters) == AllTasksSettled()


def test_輪になった読み替えで止まらなくならない():
    upcasters = {
        ("Old", 1): lambda data: ("Older", 1, data),
        ("Older", 1): lambda data: ("Old", 1, data),
    }
    with pytest.raises(UnknownEventType):
        from_record(EventRecord("Old", 1, {}), upcasters)


# --- 統合検査への改名と、古い名前を読む codec ---


def test_ステージ名Verifyで記録された実行は統合検査として読める():
    stored = {"execution": {"task": "task1", "stage": "Verify", "round": 0, "attempt": 1}}
    event = from_record(EventRecord("FixCounted", 1, {**stored, "findings": []}))
    assert event == FixCounted(ExecutionId(TaskId("task1"), StageKind.INTEGRATION_CHECK, 0, 1), ())


def test_統合検査の実行はIntegrationCheckと書きVerifyとは書かない():
    execution = ExecutionId(TaskId("task1"), StageKind.INTEGRATION_CHECK, 0, 1)
    data = to_record(FixCounted(execution, ())).data
    assert data["execution"]["stage"] == "IntegrationCheck"
    assert "Verify" not in json.dumps(data)


@dataclass(frozen=True)
class _Renamed:
    # 古いキー名を metadata に書くと、読むときだけ新しい欄として受ける
    new: int = field(metadata={codec.RENAMED_FROM: ("old",)})


def test_古いキー名の欄を新しい欄として読み書きは新しい名前だけにする():
    assert codec.from_json(_Renamed, {"old": 1}) == _Renamed(new=1)
    assert codec.from_json(_Renamed, {"new": 1}) == _Renamed(new=1)
    assert codec.to_json(_Renamed(new=1)) == {"new": 1}


def test_古いキーと新しいキーが両方あれば拒む():
    with pytest.raises(codec.DecodeError):
        codec.from_json(_Renamed, {"old": 1, "new": 2})


# --- 改名前の欄名 verify の読み替え ---


def stored(event: Event) -> dict[str, Any]:
    return json.loads(json.dumps(to_record(event).data, ensure_ascii=False))


def test_TasksPlannedの古いverifyは回帰テストとして読み軽い検査は空になる():
    data = stored(sample(TasksPlanned))
    for name in ("quick_checks", "regression_tests"):
        del data[name]
    data["verify"] = ["pytest"]
    event = from_record(EventRecord("TasksPlanned", 1, data))
    assert isinstance(event, TasksPlanned)
    assert event.regression_tests == (VerifyCommand("pytest"),)
    assert event.quick_checks == ()


def test_TasksPlannedは新しい欄名で書き古いverifyは書かない():
    event = from_record(EventRecord("TasksPlanned", 1, stored(sample(TasksPlanned))))
    assert isinstance(event, TasksPlanned)
    written = stored(event)
    assert "verify" not in json.dumps(written)
    assert {"quick_checks", "regression_tests"} <= set(written)


def test_TaskSpecを含むイベントの古いverifyはタスクのテストとして読める():
    data = stored(sample(TaskStarted))
    del data["spec"]["task_tests"]
    data["spec"]["verify"] = ["pytest a"]
    event = from_record(EventRecord("TaskStarted", 1, data))
    assert isinstance(event, TaskStarted)
    assert event.spec is not None
    assert event.spec.task_tests == (VerifyCommand("pytest a"),)


def test_Proposalを含むイベントの古いverifyは回帰テストとして読める():
    data = stored(sample(SettledPlanRecorded))
    for name in ("quick_checks", "regression_tests"):
        del data["proposal"][name]
    data["proposal"]["verify"] = ["pytest"]
    event = from_record(EventRecord("SettledPlanRecorded", 1, data))
    assert isinstance(event, SettledPlanRecorded)
    assert event.proposal.regression_tests == (VerifyCommand("pytest"),)
    assert event.proposal.quick_checks == ()


# --- 指摘の宛先 ---


def test_宛先の欄が無い古いFindingRaisedはimplとして読める():
    stored = {"finding": "R1", "rating": "must-fix", "body": "境界で落ちる"}
    event = from_record(EventRecord("FindingRaised", 1, stored))
    assert isinstance(event, FindingRaised)
    assert event.target is FindingTarget.IMPL


def test_宛先の欄が無い古いFindingCarriedはimplとして読める():
    stored = {"finding": "R1", "to_task": "task3", "rating": "must-fix", "body": "境界で落ちる"}
    event = from_record(EventRecord("FindingCarried", 1, stored))
    assert isinstance(event, FindingCarried)
    assert event.target is FindingTarget.IMPL


def test_指摘の宛先と書き換えは記録して同じ値で読み戻せる():
    raised = FindingRaised(
        FindingId("R1"), Rating.MUST_FIX, "テストが無い", target=FindingTarget.TESTS
    )
    assert to_record(raised).data["target"] == "tests"
    assert from_record(to_record(raised)) == raised
    execution = ExecutionId(TaskId("task1"), StageKind.JUDGE, 1, 1)
    retargeted = FindingRetargeted(FindingId("R1"), FindingTarget.IMPL, execution)
    assert from_record(to_record(retargeted)) == retargeted
    assert FindingRetargeted in EVENTS_BY_AGGREGATE["ReviewLedger"]


# --- 直す役の並び（RoundConcluded.fixers）と指摘の行き先（Conclude*Round.routes） ---


def test_fixersの欄が無い古いRoundConcludedは空で読める():
    event = RoundConcluded(1, 1, False, Cursor(1).enter(StageKind.FIX, 1))
    stored = to_record(event).data
    stored.pop("fixers", None)
    loaded = from_record(EventRecord("RoundConcluded", 1, stored))
    assert loaded == event
    assert isinstance(loaded, RoundConcluded) and loaded.fixers == ()


def test_直す役の並びは記録して同じ値で読み戻せる():
    event = RoundConcluded(
        1,
        1,
        False,
        Cursor(1).enter(StageKind.TEST_FIX, 1),
        fixers=(StageKind.TEST_FIX, StageKind.FIX),
    )
    assert to_record(event).data["fixers"] == ["TestFix", "Fix"]
    assert from_record(to_record(event)) == event


def test_行き先の欄が無い古いConcludeReviewRoundは空で読める():
    command = ConcludeReviewRound(
        command_id=CommandId("c1"),
        issuer=Issuer.policy("review-loop", EventId("task/task1#3")),
        task=TaskId("task1"),
        judge=ExecutionId(TaskId("task1"), StageKind.JUDGE, 1, 1),
        unresolved=(FindingId("R1"),),
        routes=(FindingRoute(FindingId("R1"), FindingTarget.TESTS),),
    )
    stored = json.loads(json.dumps(codec.to_json(command), ensure_ascii=False))
    assert stored["routes"] == [{"finding": "R1", "target": "tests"}]
    assert codec.from_json(ConcludeReviewRound, stored) == command
    del stored["routes"]
    assert codec.from_json(ConcludeReviewRound, stored).routes == ()
