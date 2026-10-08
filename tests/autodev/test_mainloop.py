"""メインループ（`app/driving/mainloop.py`）。偽の集約（`autodev_fakes`）と偽のポリシーで、配達とコマンドの処理の手順を確かめる。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from autodev_fakes import (
    HEAD,
    Queue,
    Recorder,
    Work,
    chain,
    enqueue,
    execution,
    factory,
    requested,
    task,
)

from autodev.app.driving.mainloop import Delivery, Inbox, LoopExit, MainLoop, Subscriber
from autodev.domain.aggregates.base import Aggregate
from autodev.domain.aggregates.task import Task
from autodev.domain.commands.base import Command
from autodev.domain.commands.task import (
    AcceptFlow,
    BeginStage,
    MarkInterrupted,
    OpenTask,
    ResumeInterrupted,
    ResumeStage,
)
from autodev.domain.events.stack import GitJobQueued
from autodev.domain.events.task import StageInterrupted, StageStarted
from autodev.domain.flow.flow import FlowStep, Reviewers
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.artifact_ref import ArtifactRef
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.interrupt_cause import InterruptCause
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.issuer_kind import IssuerKind
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.session_id import SessionId
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_kind import TaskKind
from autodev.domain.value_objects.task_spec import TaskSpec
from autodev.infra.paths import RunPaths
from autodev.infra.status.status import read_progress, write_progress
from autodev.infra.store import db
from autodev.infra.store.eventstore import EventStore
from autodev.infra.store.rejections import Rejection
from autodev.infra.store.requests import RequestBox

STACK = StreamId.stack()
BRANCH = BranchName("stack/r--task1")
STARTUP = InterruptCause.STARTUP
SESSION = SessionId("0f8fad5b-d9cb-469f-a165-70867728950e")


class Crash(Exception):
    """driver が落ちた代わり。"""


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "events.db"


@pytest.fixture
def rejections() -> list[Rejection]:
    return []


def open_loop(
    path: Path, subscribers: list[Subscriber], rejections: list[Rejection], **kwargs
) -> MainLoop:
    return MainLoop(
        EventStore.open(path), factory, subscribers, on_rejected=rejections.append, **kwargs
    )


def types_in(path: Path) -> list[str]:
    with EventStore.open(path) as store:
        return [s.record.type for s in store.read_all()]


def test_ポリシーが返したコマンドを処理して進めなくなったら終える(
    path: Path, rejections: list[Rejection]
):
    recorder = Recorder()
    loop = open_loop(path, [chain(last=3), recorder.subscriber()], rejections)
    loop.process(enqueue(1, "c1"))
    assert loop.run().exit is LoopExit.IDLE
    queue = loop.aggregates[STACK]
    assert isinstance(queue, Queue)
    assert queue.waiting == [task(1), task(2), task(3)]
    # どの受け手も全部を受け、チェックポイントは最後まで進んでいる
    assert recorder.seen == [1, 2, 3]
    with EventStore.open(path) as store:
        assert store.checkpoints() == {"chain": 3, "recorder": 3}
        assert [s.command_id for s in store.read_all()][1:] == [
            CommandId("stack#1/chain/0"),
            CommandId("stack#2/chain/0"),
        ]
    assert rejections == []


def test_同じイベントは登録した順に受け手へ渡す(path: Path, rejections: list[Rejection]):
    order: list[tuple[str, int]] = []

    def named(name: str) -> Subscriber:
        def receive(delivery: Delivery) -> list[Command]:
            order.append((name, delivery.seq))
            return []

        return Subscriber(name, receive)

    loop = open_loop(path, [chain(last=2), named("b"), named("a")], rejections)
    loop.process(enqueue(1, "c1"))
    loop.run()
    assert order == [("b", 1), ("a", 1), ("b", 2), ("a", 2)]


def test_同じcommand_idのコマンドは2回処理しない(path: Path, rejections: list[Rejection]):
    loop = open_loop(path, [], rejections)
    assert loop.process(enqueue(1, "c1")) is None
    assert loop.process(enqueue(1, "c1")) is None
    assert types_in(path) == ["GitJobQueued"]
    assert rejections == []
    # 開き直した driver でも弾く
    again = open_loop(path, [], rejections)
    assert again.process(enqueue(1, "c1")) is None
    assert types_in(path) == ["GitJobQueued"]


def test_拒んだコマンドは記録して理由を返し何も書かない(path: Path, rejections: list[Rejection]):
    loop = open_loop(path, [], rejections)
    loop.process(enqueue(1, "c1"))
    assert loop.process(enqueue(1, "c2")) == "すでに列にある"
    assert types_in(path) == ["GitJobQueued"]
    assert [(r.type, r.command_id, r.reason) for r in rejections] == [
        ("EnqueueStack", "c2", "すでに列にある")
    ]
    assert rejections[0].issuer == {
        "kind": "policy",
        "task": None,
        "session": None,
        "name": "test",
        "event": "stack#1",
        "execution": None,
    }


def test_落ちた後はチェックポイントの後ろから配り直し出したコマンドは2回処理しない(
    path: Path, rejections: list[Rejection]
):
    crashed = False

    def flaky(delivery: Delivery) -> Iterator[Command]:
        """1 回目は、コマンドを 1 つ処理させた後、チェックポイントを進める前に落ちる。"""
        nonlocal crashed
        yield from chain(last=3, name="flaky").receive(delivery)
        if not crashed and delivery.seq == 2:
            crashed = True
            raise Crash

    recorder = Recorder()
    subscribers = [Subscriber("flaky", flaky), recorder.subscriber()]
    loop = open_loop(path, subscribers, rejections)
    loop.process(enqueue(1, "c1"))
    with pytest.raises(Crash):
        loop.run()
    assert types_in(path) == ["GitJobQueued"] * 3
    with EventStore.open(path) as store:
        assert store.checkpoints() == {"flaky": 1, "recorder": 1}

    # 起き直す。再生した状態から続け、flaky は seq 2 から受け直す
    again = open_loop(path, subscribers, rejections)
    assert again.run().exit is LoopExit.IDLE
    assert types_in(path) == ["GitJobQueued"] * 3
    queue = again.aggregates[STACK]
    assert isinstance(queue, Queue)
    assert queue.waiting == [task(1), task(2), task(3)]
    assert recorder.seen == [1, 2, 3]
    assert rejections == []


def test_足した受け手は過去のイベントに反応せず足したことを記録する(
    path: Path, rejections: list[Rejection], caplog: pytest.LogCaptureFixture
):
    loop = open_loop(path, [], rejections)
    loop.process(enqueue(1, "c1"))
    loop.process(enqueue(2, "c2"))
    loop.run()
    recorder = Recorder()
    added = open_loop(path, [recorder.subscriber()], rejections)
    assert "受け手 recorder を足した。seq 2 より前" in caplog.text
    # 配る前に落ちても、次の起動で始めの位置がずれないように、すぐに書いてある
    with EventStore.open(path) as store:
        assert store.checkpoints() == {"recorder": 2}
    added.process(enqueue(3, "c3"))
    assert added.run().exit is LoopExit.IDLE
    assert recorder.seen == [3]


def test_イベントの無いランで登録した受け手は最初のイベントから受ける(
    path: Path, rejections: list[Rejection]
):
    recorder = Recorder()
    open_loop(path, [recorder.subscriber()], rejections)
    with EventStore.open(path) as store:
        store.append(CommandId("c1"), STACK, 0, [requested(1)])
    # 登録した後・配る前に落ちた driver の代わりに、開き直す
    assert open_loop(path, [recorder.subscriber()], rejections).run().exit is LoopExit.IDLE
    assert recorder.seen == [1]


def test_受け手の名前が重なれば始めない(path: Path, rejections: list[Rejection]):
    with pytest.raises(ValueError, match="重なっている"):
        open_loop(path, [chain(3), chain(3)], rejections)


def test_requestsの要求を拾って処理し消す(path: Path, rejections: list[Rejection]):
    EventStore.open(path).close()
    with RequestBox.open(path) as box:
        box.put(ResumeStage, {"task": task(1), "execution": execution(1)})
        # 走っている実行の再開は偽の Task が拒む。拒んだ要求も消し、理由を記録する
        box.put(ResumeStage, {"task": task(1), "execution": execution(1)})
    with EventStore.open(path) as store, db.transaction(store.connection):
        store.connection.execute(
            "INSERT INTO requests (id, type, data, at) VALUES ('request/bad', 'Nope', '{}', 'x')"
        )
    loop = open_loop(path, [], rejections)
    assert loop.run().exit is LoopExit.IDLE
    assert types_in(path) == ["StageStarted"]
    with EventStore.open(path) as store:
        (started,) = store.read_all()
        assert started.command_id.value.startswith("request/")
        assert RequestBox(store.connection).pending() == []
    assert [(r.type, r.reason) for r in rejections] == [
        ("ResumeStage", "走っている"),
        ("Nope", "要求を読めない: 知らないコマンド: Nope"),
    ]
    assert [r.issuer["kind"] for r in rejections] == [IssuerKind.CLI.value] * 2


def test_requestsの処理で落ちた要求は記録して消し再生し直して続ける(
    path: Path, rejections: list[Rejection], caplog: pytest.LogCaptureFixture
):
    broken = StreamId.task(task(9))

    def fragile(stream: StreamId) -> Aggregate:
        if stream == broken:
            raise RuntimeError("壊れた集約")
        return factory(stream)

    EventStore.open(path).close()
    with RequestBox.open(path) as box:
        box.put(ResumeStage, {"task": task(9), "execution": execution(9)})
        box.put(ResumeStage, {"task": task(1), "execution": execution(1)})
    loop = MainLoop(EventStore.open(path), fragile, [], on_rejected=rejections.append)
    assert loop.run().exit is LoopExit.IDLE
    assert types_in(path) == ["StageStarted"]
    assert [(r.type, r.reason) for r in rejections] == [
        ("ResumeStage", "要求の処理で落ちた: RuntimeError: 壊れた集約")
    ]
    assert "の処理で落ちた" in caplog.text
    with EventStore.open(path) as store:
        assert RequestBox(store.connection).pending() == []
    work = loop.aggregates[StreamId.task(task(1))]
    assert isinstance(work, Work)
    assert work.running == {execution(1)}


def test_起動時にrunningのまま残った実行をドメインに聞いてinterruptedにする(
    path: Path, rejections: list[Rejection]
):
    with EventStore.open(path) as store:
        store.append(
            CommandId("seed1"),
            StreamId.task(task(1)),
            0,
            [StageStarted(execution(1), HEAD), StageStarted(execution(1, attempt=2), HEAD)],
        )
        store.append(
            CommandId("seed2"),
            StreamId.task(task(2)),
            0,
            [StageStarted(execution(2), HEAD), StageInterrupted(execution(2), STARTUP)],
        )
    loop = open_loop(path, [], rejections)
    assert loop.run().exit is LoopExit.IDLE
    with EventStore.open(path) as store:
        added = store.read_all(after=4)
    assert [(s.decode(), s.command_id) for s in added] == [
        (StageInterrupted(execution(1), STARTUP), CommandId("task/task1#2/driver-startup/0")),
        (
            StageInterrupted(execution(1, attempt=2), STARTUP),
            CommandId("task/task1#2/driver-startup/1"),
        ),
    ]
    # もう一度起きても、running のものが無いので何も出さない
    open_loop(path, [], rejections).run()
    assert len(types_in(path)) == 6
    assert rejections == []


def real_task_factory(stream: StreamId) -> Aggregate:
    """本物の Task を入れる（偽の集約では、ドメインが running の実行に答えるかを確かめられない）。"""
    return Task(stream)


def test_本物のTaskでも起動時にrunningの実行をinterruptedにし続きから再開できる(
    path: Path, rejections: list[Rejection]
):
    t1 = task(1)
    impl = ExecutionId(t1, StageKind.IMPL, 0, 1)
    supervisor = Issuer.task_supervisor(t1, SESSION)
    first = MainLoop(EventStore.open(path), real_task_factory, [], on_rejected=rejections.append)
    for command in (
        OpenTask(
            command_id=CommandId("open"),
            issuer=Issuer.policy("open-task", EventId("run#2")),
            task=t1,
            kind=TaskKind.IMPLEMENTATION,
            spec=TaskSpec("キャッシュ"),
            artifacts=(ArtifactRef(ArtifactKind.DESIGN, "1"),),
            branch=BranchName("stack/demo--task-1"),
        ),
        AcceptFlow(
            command_id=CommandId("flow"),
            issuer=supervisor,
            task=t1,
            steps=(
                FlowStep(StageKind.IMPL),
                FlowStep(StageKind.REVIEW_LOOP, reviewers=Reviewers((StageKind.REVIEW,))),
                FlowStep(StageKind.GATE),
                FlowStep(StageKind.WRITE_PR_BODY),
            ),
        ),
        BeginStage(
            command_id=CommandId("begin"),
            issuer=Issuer.executor(impl),
            task=t1,
            execution=impl,
            head=HEAD,
            session=SESSION,
        ),
    ):
        assert first.process(command) is None
    # Impl を走らせている間に driver が落ちた。起き直した driver はドメインに running の実行を聞く
    loop = MainLoop(EventStore.open(path), real_task_factory, [], on_rejected=rejections.append)
    assert loop.run().exit is LoopExit.IDLE
    work = loop.aggregates[StreamId.task(t1)]
    assert isinstance(work, Task)
    assert work.running_executions() == []
    assert work.executions[impl].interrupted_by is InterruptCause.STARTUP
    with EventStore.open(path) as store:
        assert store.read_all()[-1].decode() == StageInterrupted(impl, STARTUP)
    # 呼び直されたら（RunResumed を受けたポリシーの ResumeInterrupted）、同じ実行を続きから再開する
    resumed = ResumeInterrupted(
        command_id=CommandId("resume"), issuer=Issuer.policy("resume", EventId("run#9")), task=t1
    )
    assert loop.process(resumed) is None
    assert work.running_executions() == [impl]
    # CLI の ResumeStage でも再開できる（もう走っているので、ここでは拒まれる）
    again = ResumeStage(command_id=CommandId("again"), issuer=Issuer.cli(), task=t1, execution=impl)
    assert loop.process(again) == f"{impl} は interrupted・deferred でない（running）"
    loop.process(
        MarkInterrupted(
            command_id=CommandId("crash2"), issuer=Issuer.driver(), task=t1, execution=impl
        )
    )
    assert (
        loop.process(
            ResumeStage(
                command_id=CommandId("again2"), issuer=Issuer.cli(), task=t1, execution=impl
            )
        )
        is None
    )
    assert work.running_executions() == [impl]


def test_未配達のStageStartedがあっても反応はrunningの実行にしか起動を頼まれない(
    tmp_path: Path, rejections: list[Rejection]
):
    """前の driver は e2 を始めて配り、e1 を始めた所で配る前に落ちた。"""
    paths = RunPaths(RunName("demo"), tmp_path)
    path = paths.events_db
    e1, e2 = execution(1), execution(2)
    with EventStore.open(path) as store:
        store.append(CommandId("seed2"), StreamId.task(task(2)), 0, [StageStarted(e2, HEAD)])
        store.append(CommandId("seed1"), StreamId.task(task(1)), 0, [StageStarted(e1, HEAD)])
        store.set_checkpoint("launch", 1)
        store.set_checkpoint("recorder", 1)
    for e in (e1, e2, execution(3)):
        write_progress(paths, e, {"turns": 1})

    inbox = Inbox()
    launched: list[tuple[ExecutionId, bool]] = []
    holder: list[MainLoop] = []

    def launch(delivery: Delivery) -> list[Command]:
        """偽の反応。StageStarted を受けたら札を取ってステージを走らせる（ここでは少し待って返す）。"""
        event = delivery.event
        if isinstance(event, StageStarted):
            work = holder[0].aggregates[StreamId.task(event.execution.task)]
            assert isinstance(work, Work)
            launched.append((event.execution, event.execution in work.running))
            ticket = inbox.expect(event.execution)
            threading.Timer(0.05, ticket.cancel).start()
        return []

    recorder = Recorder()
    loop = open_loop(
        path,
        [Subscriber("launch", launch), recorder.subscriber()],
        rejections,
        inbox=inbox,
        paths=paths,
        poll_interval=0.01,
    )
    holder.append(loop)
    assert loop.run().exit is LoopExit.IDLE
    # 起動を頼まれたのは、配り直した e1 だけで、そのとき e1 は running だった
    assert launched == [(e1, True)]
    # 札を持っている e1 は interrupted にせず、誰も見ていない e2 だけを interrupted にした
    with EventStore.open(path) as store:
        added = store.read_all(after=2)
    assert [(s.decode(), s.command_id) for s in added] == [
        (StageInterrupted(e2, STARTUP), CommandId("task/task2#1/driver-startup/0"))
    ]
    # 配り直しの順番: 未配達の seq 2 を配り切ってから、後始末の seq 3 を配った
    assert recorder.seen == [2, 3]
    # 走っていない実行の進み具合は片付けた
    assert list(read_progress(paths)) == [str(e1)]
    assert rejections == []


def test_runを2回呼んでも起動時の後始末は1回だけ(path: Path, rejections: list[Rejection]):
    with EventStore.open(path) as store:
        store.append(
            CommandId("seed"), StreamId.task(task(1)), 0, [StageStarted(execution(1), HEAD)]
        )
    loop = open_loop(path, [], rejections)
    loop.run()
    resume = ResumeStage(
        command_id=CommandId("r1"), issuer=Issuer.cli(), task=task(1), execution=execution(1)
    )
    assert loop.process(resume) is None
    loop.run()
    assert types_in(path) == ["StageStarted", "StageInterrupted", "StageStarted"]


def test_スレッドの結果を待って処理し拒まれたら出した者に返す(
    path: Path, rejections: list[Rejection]
):
    replies: list[tuple[str, str]] = []
    inbox = Inbox()

    def launch(delivery: Delivery) -> list[Command]:
        """偽の反応。task1 が列に入ったら、別のスレッドで少し待ってから結果を返す。"""
        event = delivery.event
        if isinstance(event, GitJobQueued) and event.job.task == task(1):
            for number, command_id in ((2, "t1"), (2, "t2")):
                ticket = inbox.expect()

                def work(ticket=ticket, number=number, command_id=command_id) -> None:
                    with ticket:
                        ticket.submit(
                            enqueue(number, command_id),
                            reply=lambda command, reason: replies.append(
                                (command.command_id.value, reason)
                            ),
                        )

                threading.Timer(0.05, work).start()
        return []

    loop = open_loop(
        path, [Subscriber("launch", launch)], rejections, inbox=inbox, poll_interval=0.01
    )
    loop.process(enqueue(1, "c1"))
    assert loop.run().exit is LoopExit.IDLE
    queue = loop.aggregates[STACK]
    assert isinstance(queue, Queue)
    assert queue.waiting == [task(1), task(2)]
    assert sorted(replies) in ([("t1", "すでに列にある")], [("t2", "すでに列にある")])
    assert inbox.outstanding == 0


def test_札は1回しか使えない(path: Path, rejections: list[Rejection]):
    inbox = Inbox()
    ticket = inbox.expect()
    ticket.cancel()
    with pytest.raises(RuntimeError):
        ticket.submit(enqueue(1, "c1"))
    loop = open_loop(path, [], rejections, inbox=inbox)
    assert loop.run().exit is LoopExit.IDLE
    assert inbox.outstanding == 0


def test_スレッドが札を返さずに落ちてもwithで札が返る(path: Path, rejections: list[Rejection]):
    inbox = Inbox()
    ticket = inbox.expect(execution(1))
    assert inbox.holds(execution(1))

    def work() -> None:
        try:
            with ticket:
                raise Crash
        except Crash:
            pass

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert open_loop(path, [], rejections, inbox=inbox).run().exit is LoopExit.IDLE
    assert inbox.outstanding == 0
    assert not inbox.holds(execution(1))


def test_ランを終えても渡しかけの札の結果を待って処理する(path: Path, rejections: list[Rejection]):
    inbox = Inbox()
    ticket = inbox.expect()
    threading.Timer(0.05, lambda: ticket.submit(enqueue(1, "late"))).start()
    loop = open_loop(path, [], rejections, inbox=inbox, finished=lambda _: True, poll_interval=0.01)
    assert loop.run().exit is LoopExit.FINISHED
    assert types_in(path) == ["GitJobQueued"]
    assert inbox.outstanding == 0


def test_ドメインが終えたと答えたら終える(path: Path, rejections: list[Rejection]):
    def finished(aggregates) -> bool:
        queue = aggregates.get(STACK)
        return isinstance(queue, Queue) and len(queue.waiting) >= 2

    recorder = Recorder()
    loop = open_loop(path, [chain(last=5), recorder.subscriber()], rejections, finished=finished)
    loop.process(enqueue(1, "c1"))
    assert loop.run().exit is LoopExit.FINISHED
    # 終える前に、未配達のイベントを配り終えている
    assert recorder.seen == list(range(1, len(recorder.seen) + 1))
    with EventStore.open(path) as store:
        assert store.checkpoints()["recorder"] == store.last_seq()


def test_止められたら理由を持って終える(path: Path, rejections: list[Rejection]):
    inbox = Inbox()
    inbox.expect()
    inbox.stop("429")
    outcome = open_loop(path, [], rejections, inbox=inbox).run()
    assert (outcome.exit, outcome.reason) == (LoopExit.STOPPED, "429")


class CountingStore(EventStore):
    """チェックポイントを書いた回数を数える。"""

    writes: list[dict[str, int]]

    def set_checkpoints(self, seqs) -> None:
        self.writes.append(dict(seqs))
        super().set_checkpoints(seqs)


def test_1つのイベントを全部の受け手に配ってからチェックポイントを1回で書く(
    path: Path, rejections: list[Rejection]
):
    store = CountingStore(db.connect(path))
    store.writes = []
    recorders = [Recorder(f"r{i}") for i in range(5)]
    loop = MainLoop(
        store,
        factory,
        [chain(last=3), *(r.subscriber() for r in recorders)],
        on_rejected=rejections.append,
    )
    store.writes.clear()
    loop.process(enqueue(1, "c1"))
    assert loop.run().exit is LoopExit.IDLE
    names = {"chain", *(r.name for r in recorders)}
    # イベント 3 つに、書くのは 3 回。どの回も、そのイベントを受けた受け手の全部を書く
    assert store.writes == [dict.fromkeys(names, seq) for seq in (1, 2, 3)]


def test_足した受け手は自分の位置から受け既にいる受け手とまとめて書く(
    path: Path, rejections: list[Rejection]
):
    old = Recorder("old")
    loop = open_loop(path, [old.subscriber()], rejections)
    loop.process(enqueue(1, "c1"))
    loop.run()
    new = Recorder("new")
    again = open_loop(path, [old.subscriber(), new.subscriber()], rejections)
    again.process(enqueue(2, "c2"))
    assert again.run().exit is LoopExit.IDLE
    assert (old.seen, new.seen) == ([1, 2], [2])


def test_ドメインが止まると答えたら未配達を配り終えてから止まる(
    path: Path, rejections: list[Rejection]
):
    recorder = Recorder()

    def halted(aggregates) -> str | None:
        queue = aggregates.get(STACK)
        return "パニック" if isinstance(queue, Queue) and len(queue.waiting) >= 2 else None

    inbox = Inbox()
    inbox.expect()  # 返らない札があっても待たない
    loop = open_loop(
        path, [chain(last=3), recorder.subscriber()], rejections, halted=halted, inbox=inbox
    )
    loop.process(enqueue(1, "c1"))
    outcome = loop.run()
    assert (outcome.exit, outcome.reason) == (LoopExit.STOPPED, "パニック")
    # 止まると答えた後も、未配達（ポリシーが続けて出した分も）を配り終えている
    assert recorder.seen == [1, 2, 3]


def test_処理し終えた知らせは通っても拒まれても届く(path: Path, rejections: list[Rejection]):
    inbox = Inbox()
    settled: list[str | None] = []
    for command_id in ("a", "b"):
        inbox.expect().submit(enqueue(1, command_id), settled=settled.append)
    inbox.expect().cancel(settled=settled.append)
    assert open_loop(path, [], rejections, inbox=inbox).run().exit is LoopExit.IDLE
    assert settled == [None, "すでに列にある", None]


def test_走っている実行を理由を付けてinterruptedにする(path: Path, rejections: list[Rejection]):
    with EventStore.open(path) as store:
        store.append(
            CommandId("seed"), StreamId.task(task(1)), 0, [StageStarted(execution(1), HEAD)]
        )
    loop = open_loop(path, [], rejections)
    loop.interrupt_running(InterruptCause.PANIC, "driver-panic")
    with EventStore.open(path) as store:
        (added,) = store.read_all(after=1)
    assert added.command_id == CommandId("task/task1#1/driver-panic/0")
    assert [delivery.seq for delivery in loop.history] == [1, 2]
