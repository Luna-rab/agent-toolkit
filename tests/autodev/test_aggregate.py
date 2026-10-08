"""集約の土台（`domain/aggregates/base.py`）。小さな集約を 1 つ組んで、振り分け・再生・拒否を確かめる。"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass

import pytest

from autodev import domain
from autodev.domain.aggregates.base import Aggregate, Rejected, UnknownEvent, applies, handles
from autodev.domain.commands.registry import COMMANDS_BY_AGGREGATE
from autodev.domain.commands.run import EscalateToRun, StartTask
from autodev.domain.commands.stack import AppendEntry, EnqueueStack, TakeNextGitJob
from autodev.domain.commands.task import AcceptFlow, ReportStageResult
from autodev.domain.events.base import Event
from autodev.domain.events.registry import EVENTS_BY_AGGREGATE
from autodev.domain.events.run import RunPanicked
from autodev.domain.flow.flow import FlowStep
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.evidence import Evidence
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.pointers import Pointers
from autodev.domain.value_objects.pr_number import PrNumber
from autodev.domain.value_objects.session_id import SessionId
from autodev.domain.value_objects.stack_entry import StackEntry
from autodev.domain.value_objects.stage_exit import StageExit
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId

STACK = StreamId.stack()
GIT = Issuer.task_supervisor(TaskId.git())
POLICY = Issuer.policy("enqueue-on-gated", EventId("task/task2#9"))


@dataclass(frozen=True)
class StackRequested(Event):
    """検査用の集約だけが出すイベント（ドメインの表には入れない）。"""

    task: TaskId
    branch: BranchName


@dataclass(frozen=True)
class StackRequestTaken(Event):
    task: TaskId
    branch: BranchName


class Queue(Aggregate):
    """積む順番待ちの列だけを持つ、検査用の集約。"""

    def __init__(self, stream: StreamId) -> None:
        super().__init__(stream)
        self.waiting: list[TaskId] = []
        self.taken: TaskId | None = None
        self.taken_as: EventId | None = None

    @handles(EnqueueStack)
    def _enqueue(self, command: EnqueueStack) -> list[Event]:
        if command.task in self.waiting:
            raise Rejected("すでに列にある")
        return [StackRequested(command.task, command.branch)]

    @handles(TakeNextGitJob)
    def _take(self, command: TakeNextGitJob) -> list[Event]:
        if not self.waiting:
            raise Rejected("列が空")
        task = self.waiting[0]
        return [StackRequestTaken(task, BranchName(f"stack/r--{task}"))]

    @applies(StackRequested)
    def _requested(self, event: StackRequested) -> None:
        self.waiting.append(event.task)

    @applies(StackRequestTaken)
    def _taken(self, event: StackRequestTaken) -> None:
        self.taken = self.waiting.pop(0)
        self.taken_as = self.event_id


def enqueue(task: str, command_id: str = "c1") -> EnqueueStack:
    return EnqueueStack(
        command_id=CommandId(command_id),
        issuer=POLICY,
        task=TaskId(task),
        branch=BranchName(f"stack/r--{task}"),
    )


def take(command_id: str = "c9") -> TakeNextGitJob:
    return TakeNextGitJob(command_id=CommandId(command_id), issuer=GIT)


def run(queue: Queue, command) -> list[Event]:
    """メインループの代わり。handle で決めたイベントを当てる。"""
    events = queue.handle(command)
    for event in events:
        queue.apply(event, command.command_id)
    return events


def test_handleは状態を変えずapplyが変える():
    queue = Queue(STACK)
    events = queue.handle(enqueue("task1"))
    assert events == [StackRequested(TaskId("task1"), BranchName("stack/r--task1"))]
    assert queue.waiting == []
    assert queue.version == 0
    queue.apply(events[0], CommandId("c1"))
    assert queue.waiting == [TaskId("task1")]
    assert queue.version == 1


def test_イベントの列を再生すると同じ状態になる():
    live = Queue(STACK)
    history = []
    for command in (enqueue("task1", "c1"), enqueue("task2", "c2"), take("c3")):
        history += [(event, command.command_id) for event in run(live, command)]
    replayed = Queue.replay(STACK, history)
    assert (replayed.waiting, replayed.taken, replayed.version) == (
        live.waiting,
        live.taken,
        live.version,
    )
    assert replayed.waiting == [TaskId("task2")]


def test_これから出すイベントのidとapplyの中のidが合う():
    queue = Queue(STACK)
    run(queue, enqueue("task1"))
    predicted = queue.next_event_id()
    assert predicted == EventId("stack#2")
    run(queue, take())
    assert queue.taken_as == predicted == queue.event_id


def test_知らないイベントを当てると例外で版は進まない():
    queue = Queue(STACK)
    with pytest.raises(UnknownEvent):
        queue.apply(RunPanicked("429"), CommandId("c1"))
    assert queue.version == 0


def test_処理済みのコマンドは2回目に何もしない():
    queue = Queue(STACK)
    run(queue, enqueue("task1", "c1"))
    assert queue.handle(enqueue("task1", "c1")) == []
    # 再生した後も、どのコマンドを処理したかを覚えている
    replayed = Queue.replay(
        STACK, [(StackRequested(TaskId("task1"), BranchName("stack/r--task1")), CommandId("c1"))]
    )
    assert replayed.handle(enqueue("task1", "c1")) == []
    # id が違えば、同じ中身でも新しいコマンドとして判断する
    with pytest.raises(Rejected, match="すでに列にある"):
        replayed.handle(enqueue("task1", "c2"))


def test_受けないコマンドを拒む():
    entry = StackEntry(TaskId("task1"), BranchName("b"), PrNumber(1), BranchName("main"))
    with pytest.raises(Rejected, match="AppendEntry は Queue が受けるコマンドではない"):
        Queue(STACK).handle(AppendEntry(command_id=CommandId("c1"), issuer=GIT, entry=entry))


def test_出してよい者でなければ拒む():
    command = EnqueueStack(
        command_id=CommandId("c1"),
        issuer=Issuer.cli(),
        task=TaskId("task1"),
        branch=BranchName("stack/r--task1"),
    )
    with pytest.raises(Rejected, match="cli は出せない"):
        Queue(STACK).handle(command)


def test_宛先が違うコマンドを拒む():
    command = StartTask(command_id=CommandId("c1"), issuer=POLICY, task=TaskId("task1"))
    with pytest.raises(Rejected, match="宛先は run"):
        Queue(STACK).handle(command)


def test_振り分けの一覧を引ける():
    assert Queue.handled_commands() == {EnqueueStack, TakeNextGitJob}
    assert Queue.applied_events() == {StackRequested, StackRequestTaken}


def test_振り分けは子の集約に引き継がれる():
    class Child(Queue):
        @applies(RunPanicked)
        def _panicked(self, event: RunPanicked) -> None:
            pass

    assert Child.applied_events() == {StackRequested, StackRequestTaken, RunPanicked}
    assert Queue.applied_events() == {StackRequested, StackRequestTaken}


def test_子の集約は同じ名前のメソッドで振り分けを上書きできる():
    class Child(Queue):
        @handles(EnqueueStack)
        def _enqueue(self, command: EnqueueStack) -> list[Event]:
            raise Rejected("子が受けた")

    with pytest.raises(Rejected, match="子が受けた"):
        Child(STACK).handle(enqueue("task1"))


def test_同じコマンドやイベントに2つのメソッドを付けるとクラスを作れない():
    with pytest.raises(TypeError, match="EnqueueStack に _a と _b"):

        class TwoHandlers(Aggregate):
            @handles(EnqueueStack)
            def _a(self, command: EnqueueStack) -> list[Event]:
                return []

            @handles(EnqueueStack)
            def _b(self, command: EnqueueStack) -> list[Event]:
                return []

    with pytest.raises(TypeError, match="StackRequested に _requested と _again"):

        class Child(Queue):
            @applies(StackRequested)
            def _again(self, event: StackRequested) -> None:
                pass


def test_当てられないイベントを返したら追記の前に例外にする():
    class Leaky(Queue):
        @handles(EnqueueStack)
        def _enqueue(self, command: EnqueueStack) -> list[Event]:
            return [StackRequested(command.task, command.branch), RunPanicked("x")]

    queue = Leaky(STACK)
    with pytest.raises(UnknownEvent, match="RunPanicked"):
        queue.handle(enqueue("task1"))
    assert queue.version == 0


# --- 名乗りは宛先と合う ---

SESSION = SessionId("0f8fad5b-d9cb-469f-a165-70867728950e")
TASK2 = TaskId("task2")


class Probe(Aggregate):
    """何も受けない集約。名乗りの照合は振り分けの前なので、照合を通ると「受けない」で拒まれる。"""


NOT_HANDLED = "が受けるコマンドではない"


def accept_flow(issuer: Issuer, task: TaskId = TASK2) -> AcceptFlow:
    return AcceptFlow(
        command_id=CommandId("c1"), issuer=issuer, task=task, steps=(FlowStep(StageKind.IMPL),)
    )


def report(issuer: Issuer, execution: ExecutionId) -> ReportStageResult:
    return ReportStageResult(
        command_id=CommandId("c1"),
        issuer=issuer,
        task=TASK2,
        execution=execution,
        evidence=Evidence(StageExit.OK, result_valid=True),
        pointers=Pointers(),
    )


def test_タスクの統括は自分のタスクのコマンドだけを出せる():
    probe = Probe(StreamId.task(TASK2))
    with pytest.raises(Rejected, match=NOT_HANDLED):
        probe.handle(accept_flow(Issuer.task_supervisor(TASK2, SESSION)))
    with pytest.raises(Rejected, match="task2 の統括だけ（task3 の統括が名乗った）"):
        probe.handle(accept_flow(Issuer.task_supervisor(TaskId("task3"), SESSION)))
    with pytest.raises(Rejected, match="task2 の統括だけ（git の統括が名乗った）"):
        probe.handle(accept_flow(Issuer.task_supervisor(TaskId.git())))


def test_別のタスクの名でラン統括へ上げられない():
    def escalate(issuer: Issuer) -> EscalateToRun:
        return EscalateToRun(
            command_id=CommandId("c1"),
            issuer=issuer,
            task=TASK2,
            kind=EscalationKind.NEEDS_REPLAN,
            reason="範囲の外",
            pointers=Pointers(),
        )

    probe = Probe(StreamId.run())
    with pytest.raises(Rejected, match=NOT_HANDLED):
        probe.handle(escalate(Issuer.task_supervisor(TASK2, SESSION)))
    with pytest.raises(Rejected, match="task2 の統括だけ"):
        probe.handle(escalate(Issuer.task_supervisor(TaskId("task3"), SESSION)))


def test_スタックを変えられるのはgit管理タスクの統括だけ():
    with pytest.raises(Rejected, match="git の統括だけ（task2 の統括が名乗った）"):
        Queue(STACK).handle(take_by(Issuer.task_supervisor(TASK2, SESSION)))
    with pytest.raises(Rejected, match="git の統括だけ（planning の統括が名乗った）"):
        Queue(STACK).handle(take_by(Issuer.task_supervisor(TaskId.planning())))
    with pytest.raises(Rejected, match="列が空"):
        Queue(STACK).handle(take_by(GIT))


def take_by(issuer: Issuer) -> TakeNextGitJob:
    return TakeNextGitJob(command_id=CommandId("c9"), issuer=issuer)


def test_実行器は自分の実行の結果だけを出せる():
    mine = ExecutionId(TASK2, StageKind.IMPL, 0, 1)
    other = ExecutionId(TASK2, StageKind.IMPL, 0, 2)
    probe = Probe(StreamId.task(TASK2))
    with pytest.raises(Rejected, match=NOT_HANDLED):
        probe.handle(report(Issuer.executor(mine), mine))
    with pytest.raises(Rejected, match="の実行器だけ"):
        probe.handle(report(Issuer.executor(other), mine))


# --- 具象の集約が表どおりに振り分けを持つか ---


def concrete_aggregates() -> list[type[Aggregate]]:
    """ドメイン層に定義した集約（NAME を持つもの）。次の段で集約を足すと、ここに自動で入る。"""
    for module in pkgutil.walk_packages(domain.__path__, prefix=f"{domain.__name__}."):
        importlib.import_module(module.name)
    found: list[type[Aggregate]] = []
    pending = list(Aggregate.__subclasses__())
    while pending:
        cls = pending.pop()
        pending += cls.__subclasses__()
        if cls.__module__.startswith(f"{domain.__name__}.") and cls.NAME:
            found.append(cls)
    return sorted(found, key=lambda cls: cls.NAME)


def coverage_gaps(cls: type[Aggregate]) -> list[str]:
    gaps: list[str] = []
    commands = set(COMMANDS_BY_AGGREGATE.get(cls.NAME, ()))
    events = set(EVENTS_BY_AGGREGATE.get(cls.NAME, ()))
    if cls.NAME not in COMMANDS_BY_AGGREGATE:
        gaps.append(f"{cls.NAME} は表に無い集約の名前")
    gaps += [f"受けていない: {c.__name__}" for c in commands - cls.handled_commands()]
    gaps += [f"表に無いのに受ける: {c.__name__}" for c in cls.handled_commands() - commands]
    gaps += [f"当てていない: {e.__name__}" for e in events - cls.applied_events()]
    gaps += [f"表に無いのに当てる: {e.__name__}" for e in cls.applied_events() - events]
    return sorted(gaps)


@pytest.mark.parametrize("cls", concrete_aggregates(), ids=lambda cls: cls.NAME)
def test_具象の集約は表どおりのコマンドを受けイベントを当てる(cls: type[Aggregate]):
    assert coverage_gaps(cls) == []


def test_表との食い違いを見つける():
    class Stack(Queue):
        NAME = "Stack"

    gaps = coverage_gaps(Stack)
    assert "受けていない: AppendEntry" in gaps
    assert "当てていない: TaskStacked" in gaps
    # 検査用のイベントは表に無いので、当てると食い違いになる
    assert "表に無いのに当てる: StackRequested" in gaps
    assert not any("EnqueueStack" in gap for gap in gaps)

    class Unknown(Queue):
        NAME = "Nope"

    assert "Nope は表に無い集約の名前" in coverage_gaps(Unknown)
