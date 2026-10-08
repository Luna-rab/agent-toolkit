"""インフラとメインループの検査で使う、偽の集約と偽のポリシー。

本物の集約とポリシーは別の段で作るので、ここではイベントストアと配達の手順を確かめるのに足りる
だけの小さなものを組む。
"""

from __future__ import annotations

from collections.abc import Iterable

from autodev.app.driving.mainloop import Delivery, Subscriber
from autodev.domain.aggregates.base import Aggregate, Rejected, applies, handles
from autodev.domain.commands.base import Command
from autodev.domain.commands.stack import EnqueueStack
from autodev.domain.commands.task import MarkInterrupted, ResumeStage
from autodev.domain.events.base import Event
from autodev.domain.events.stack import GitJobQueued
from autodev.domain.events.task import StageInterrupted, StageStarted
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.commit_sha import CommitSha
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.git_job import GitJob
from autodev.domain.value_objects.git_job_kind import GitJobKind
from autodev.domain.value_objects.interrupt_cause import InterruptCause
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId

HEAD = CommitSha("0" * 40)
POLICY = Issuer.policy("test", EventId("stack#1"))


def requested(number: int) -> GitJobQueued:
    """`task<number>` を積む仕事を列に入れたイベント。"""
    job = GitJob(
        number, GitJobKind.STACK, task=task(number), branch=BranchName(f"stack/r--task{number}")
    )
    return GitJobQueued(job)


class Queue(Aggregate):
    """積む順番待ちの列（`stack` のストリーム）。"""

    def __init__(self, stream: StreamId) -> None:
        super().__init__(stream)
        self.waiting: list[TaskId] = []

    @handles(EnqueueStack)
    def _enqueue(self, command: EnqueueStack) -> list[Event]:
        if command.task in self.waiting:
            raise Rejected("すでに列にある")
        return [requested(int(command.task.value.removeprefix("task")))]

    @applies(GitJobQueued)
    def _requested(self, event: GitJobQueued) -> None:
        assert event.job.task is not None
        self.waiting.append(event.job.task)


class Work(Aggregate):
    """走っている実行だけを持つ Task（`task/<id>` のストリーム）。起動時の問いに答える。"""

    def __init__(self, stream: StreamId) -> None:
        super().__init__(stream)
        self.running: set[ExecutionId] = set()

    def running_executions(self) -> Iterable[ExecutionId]:
        return sorted(self.running, key=str)

    @handles(MarkInterrupted)
    def _interrupt(self, command: MarkInterrupted) -> list[Event]:
        if command.execution not in self.running:
            raise Rejected("走っていない")
        return [StageInterrupted(command.execution, InterruptCause.STARTUP)]

    @handles(ResumeStage)
    def _resume(self, command: ResumeStage) -> list[Event]:
        if command.execution in self.running:
            raise Rejected("走っている")
        return [StageStarted(command.execution, HEAD)]

    @applies(StageStarted)
    def _started(self, event: StageStarted) -> None:
        self.running.add(event.execution)

    @applies(StageInterrupted)
    def _interrupted(self, event: StageInterrupted) -> None:
        self.running.discard(event.execution)


def factory(stream: StreamId) -> Aggregate:
    if stream == StreamId.stack():
        return Queue(stream)
    if stream.value.startswith("task/"):
        return Work(stream)
    raise AssertionError(f"検査で使わないストリーム: {stream}")


def task(number: int) -> TaskId:
    return TaskId.numbered(number)


def execution(number: int, attempt: int = 1) -> ExecutionId:
    return ExecutionId(task(number), StageKind.IMPL, 0, attempt)


def enqueue(number: int, command_id: str) -> EnqueueStack:
    return EnqueueStack(
        command_id=CommandId(command_id),
        issuer=POLICY,
        task=task(number),
        branch=BranchName(f"stack/r--task{number}"),
    )


def chain(last: int, name: str = "chain") -> Subscriber:
    """偽のポリシー。`task<N>` が列に入ったら、`last` まで `task<N+1>` を入れる。"""

    def receive(delivery: Delivery) -> list[Command]:
        event = delivery.event
        if not isinstance(event, GitJobQueued) or event.job.task is None:
            return []
        number = int(event.job.task.value.removeprefix("task"))
        if number >= last:
            return []
        return [
            EnqueueStack(
                command_id=CommandId.derived(delivery.event_id, name),
                issuer=Issuer.policy(name, delivery.event_id),
                task=task(number + 1),
                branch=BranchName(f"stack/r--task{number + 1}"),
            )
        ]

    return Subscriber(name, receive)


class Recorder:
    """偽の反応。受けたイベントの seq を覚えるだけ。"""

    def __init__(self, name: str = "recorder") -> None:
        self.name = name
        self.seen: list[int] = []

    def subscriber(self) -> Subscriber:
        def receive(delivery: Delivery) -> list[Command]:
            self.seen.append(delivery.seq)
            return []

        return Subscriber(self.name, receive)
