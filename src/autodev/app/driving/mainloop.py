"""メインループ。

driver の中で状態を読み書きするのは、このループ 1 本だけ。コマンドの出どころ（ポリシー・反応・
実行器・統括・`/autodev` の CLI）はどれもここを通るので、集約の読み書きにロックは要らない。

```mermaid
flowchart TD
    start["起動: events を頭から再生"] --> drain["未配達の分を受け手に配り切る"]
    drain --> left["running のままの実行をドメインに聞き、<br/>札を持っていないものを MarkInterrupted"]
    left --> deliver{"受け手への未配達がある?"}
    deliver -->|"ある"| recv["受け手に渡し、返ったコマンドを処理し、チェックポイントを進める"] --> deliver
    deliver -->|"無い"| inbox{"スレッドの結果・requests の要求がある?"}
    inbox -->|"ある"| proc["コマンドを処理する"] --> deliver
    inbox -->|"無く、待っている札も無い"| fin{"終わった?（ドメインに聞く）"}
    fin -->|"終わった"| done["FINISHED"]
    fin -->|"まだ"| idle["IDLE"]
```

ここに書くのは、受け手の登録と配達・コマンドの処理の手順だけで、「何が起きたら何をするか」は
書かない。それはドメインのポリシーと集約が決め、反応はポートで実行するだけである。

- **受け手** は、ポリシーと反応のどちらも `Subscriber` として登録する。イベントを受けて、処理して
  ほしいコマンドの一覧を返す（無ければ空）。反応は副作用を起こしてから、続きが要るときだけ
  コマンドを返す。返したコマンドはチェックポイントを進める前に処理するので、落ちても受け直しで
  もう一度出る（同じ `CommandId` なので 2 回目は何もしない）
- **時間のかかること**（ステージと統括の起動）は、反応が `Inbox.expect` で受け取りの札を取って
  別のスレッドに渡す。スレッドは結果のコマンドを札で返す。結果はメモリで渡すので、渡す前に driver
  が落ちたら、次の起動で running のまま残った実行として `interrupted` にする
- **起動時は、先に未配達の分を配り切ってから** running の実行を interrupted にする。逆にすると、
  配り直した古い `StageStarted` を受けた反応が、もう interrupted になった実行の起動を頼まれる。
  配り直しで反応が札を取った実行（走らせ直した実行）は、interrupted にしない
"""

from __future__ import annotations

import bisect
import logging
import queue
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType, TracebackType
from typing import Protocol, runtime_checkable

from ...domain import codec
from ...domain.aggregates.base import Aggregate, Rejected
from ...domain.commands.base import Command
from ...domain.commands.task import MarkInterrupted
from ...domain.events.base import Event
from ...domain.value_objects.command_id import CommandId
from ...domain.value_objects.event_id import EventId
from ...domain.value_objects.execution_id import ExecutionId
from ...domain.value_objects.interrupt_cause import InterruptCause
from ...domain.value_objects.issuer import Issuer
from ...domain.value_objects.stream_id import StreamId
from ...infra.paths import RunPaths
from ...infra.status.status import prune_progress
from ...infra.store.eventstore import AggregateFactory, EventStore, StoredEvent, decoded, replay
from ...infra.store.rejections import Rejection
from ...infra.store.requests import Request, RequestBox, UnreadableRequest

log = logging.getLogger(__name__)

#: スレッドの結果を待つ 1 回の長さ（秒）。この間隔で requests も見に行く
POLL_INTERVAL = 0.5

#: 起動時の後始末（MarkInterrupted）の CommandId に入れる名前
STARTUP = "driver-startup"


@dataclass(frozen=True)
class Delivery:
    """受け手に渡す 1 つのイベント。"""

    seq: int
    event_id: EventId
    event: Event
    #: このイベントを出したコマンド
    command_id: CommandId
    at: str

    @classmethod
    def of(cls, stored: StoredEvent, event: Event) -> Delivery:
        return cls(stored.seq, stored.event_id, event, stored.command_id, stored.at)


@dataclass(frozen=True)
class Subscriber:
    """受け手（ポリシーか反応）。

    `name` はチェックポイントの鍵で、ポリシーと反応が出すコマンドの id（`CommandId.derived`）にも
    入る。**足した受け手（チェックポイントの無い名前）は、過去のイベントに反応しない。** 登録した
    時点の最後のイベントから受け始める。名前を変えた受け手も同じで、変える前のイベントは受けない。
    過去のイベントに反応させると、反応がもう済んだ副作用（ステージの起動など）をやり直す。
    """

    name: str
    receive: Callable[[Delivery], Iterable[Command]]


@runtime_checkable
class RunningExecutions(Protocol):
    """running のまま残っている実行をドメインに聞く問い。

    どれが running かはドメインが決める。メインループは、この問いを持つ集約に聞いて、返った実行の
    うち札を持っていないものに `MarkInterrupted` を出すだけ。
    """

    def running_executions(self) -> Iterable[ExecutionId]: ...


#: 拒んだコマンドを、出した者に返す（統括なら同じセッションに差し戻す）。
#: **メインループのスレッドで呼ばれる。** 呼んでいる間は、ほかのコマンドもイベントも進まない。
#: 重い処理（統括を起こし直すなど）は中でせず、`Inbox.expect` で札を取って別のスレッドに渡す
Reply = Callable[[Command, str], None]

#: 渡した結果をメインループが処理し終えた知らせ（拒んだなら理由、通ったなら None）。`Reply` と同じく
#: メインループのスレッドで呼ばれる。続きの仕事（同じ統括の次のターン）を、前の判断が確定してから
#: 始めるのに使う
Settled = Callable[[str | None], None]


class LoopExit(Enum):
    #: ドメインが「ランを終えた」と答え、待っている札も無い
    FINISHED = "finished"
    #: 進められるものが無い（未配達も、待っている札も、requests も無い）
    IDLE = "idle"
    #: `Inbox.stop` で止められた（パニックなど）
    STOPPED = "stopped"


@dataclass(frozen=True)
class Outcome:
    exit: LoopExit
    reason: str | None = None


# --- スレッドからの受け取り ---


@dataclass(frozen=True)
class _Submission:
    #: None なら、返す結果が無い（cancel）
    command: Command | None
    reply: Reply | None
    execution: ExecutionId | None
    settled: Settled | None = None


@dataclass(frozen=True)
class _Stop:
    reason: str


class Ticket:
    """スレッドの結果を 1 回だけ返す札。返すまで、メインループは IDLE・FINISHED で終わらない。

    スレッドの中では `with` で使う。submit も cancel もせずに抜けたら（例外を含む）、cancel する。
    札が返らないと、メインループはいつまでもその結果を待つ::

        ticket = inbox.expect(execution)
        def work() -> None:
            with ticket:
                ticket.submit(run_stage())
    """

    def __init__(
        self, items: queue.Queue[_Submission | _Stop], execution: ExecutionId | None
    ) -> None:
        self._items = items
        self._execution = execution
        self._used = False
        self._lock = threading.Lock()

    def submit(
        self, command: Command, reply: Reply | None = None, settled: Settled | None = None
    ) -> None:
        """結果のコマンドを 1 つ渡す。拒まれたら、理由を `reply` に返す（`Reply` の注意を見る）。

        統括の判断 1 つ・ステージの結果 1 つが、コマンド 1 つにあたる。`settled` は、通っても
        拒まれても、処理し終えたときに呼ぶ。
        """
        self._put(command, reply, settled)

    def cancel(self, settled: Settled | None = None) -> None:
        """返す結果が無い（スレッドが何も出さずに終わった）。`settled` は受け取ったときに呼ぶ。"""
        self._put(None, None, settled)

    @property
    def used(self) -> bool:
        with self._lock:
            return self._used

    def __enter__(self) -> Ticket:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self.used:
            self.cancel()

    def _put(self, command: Command | None, reply: Reply | None, settled: Settled | None) -> None:
        with self._lock:
            if self._used:
                raise RuntimeError("この札はもう使った")
            self._used = True
        self._items.put(_Submission(command, reply, self._execution, settled))


class Inbox:
    """ステージと統括のスレッドが、メインループに結果を渡す口。スレッドをまたいでよい。"""

    def __init__(self) -> None:
        self._queue: queue.Queue[_Submission | _Stop] = queue.Queue()
        self._lock = threading.Lock()
        #: 札を出して、まだメインループが受け取っていない数。受け取った時に減らすので、
        #: 0 なら待っている結果は無い（スレッドが渡した直後でも、キューに残っていれば 0 にならない）
        self._outstanding = 0
        #: 実行ごとの、受け取っていない札の数。この driver がその実行を見ているか
        self._live: dict[ExecutionId, int] = {}

    def expect(self, execution: ExecutionId | None = None) -> Ticket:
        """札を取る。ステージを走らせる札には `execution` を渡す（起動時に interrupted にしないため）。"""
        with self._lock:
            self._outstanding += 1
            if execution is not None:
                self._live[execution] = self._live.get(execution, 0) + 1
        return Ticket(self._queue, execution)

    def stop(self, reason: str) -> None:
        self._queue.put(_Stop(reason))

    @property
    def outstanding(self) -> int:
        with self._lock:
            return self._outstanding

    def holds(self, execution: ExecutionId) -> bool:
        """その実行の札で、まだ受け取っていないものがあるか。"""
        with self._lock:
            return execution in self._live

    def take(self, wait: float) -> _Submission | _Stop | None:
        """メインループだけが呼ぶ。`wait` 秒まで待つ（0 なら待たない）。"""
        try:
            item = self._queue.get(timeout=wait) if wait > 0 else self._queue.get_nowait()
        except queue.Empty:
            return None
        if isinstance(item, _Submission):
            with self._lock:
                self._outstanding -= 1
                if (execution := item.execution) is not None:
                    self._live[execution] -= 1
                    if not self._live[execution]:
                        del self._live[execution]
        return item


# --- ループ ---


class MainLoop:
    def __init__(
        self,
        store: EventStore,
        factory: AggregateFactory,
        subscribers: Sequence[Subscriber],
        *,
        on_rejected: Callable[[Rejection], None],
        finished: Callable[[Mapping[StreamId, Aggregate]], bool] = lambda _: False,
        halted: Callable[[Mapping[StreamId, Aggregate]], str | None] = lambda _: None,
        inbox: Inbox | None = None,
        paths: RunPaths | None = None,
        poll_interval: float = POLL_INTERVAL,
    ) -> None:
        """起動時の再生と、受け手の登録をここで行う。

        - `factory`: ストリームから、イベントが無いときの集約を作る
        - `on_rejected`: 拒んだコマンドの記録（`logs/`）。出した者への差し戻しは `Ticket.submit` の
          `reply` で別に行う
        - `finished`: ランを終えたかをドメインに聞く問い
        - `halted`: 止まるべきか（パニックした など）をドメインに聞く問い。理由を返したら、未配達を
          配り終えた所で STOPPED で終える。待っている札の結果は待たない
        - `paths`: 渡せば、起動時に走っていない実行の進み具合（`progress/`）を片付ける
        """
        names = [subscriber.name for subscriber in subscribers]
        if duplicated := sorted({name for name in names if names.count(name) > 1}):
            raise ValueError(
                f"受け手の名前が重なっている（チェックポイントが混ざる）: {duplicated}"
            )
        self._store = store
        self._requests = RequestBox(store.connection, store.clock)
        self._factory = factory
        self._subscribers = tuple(subscribers)
        self._on_rejected = on_rejected
        self._finished = finished
        self._halted = halted
        self.inbox = Inbox() if inbox is None else inbox
        self._paths = paths
        self._poll_interval = poll_interval
        self._recovered = False

        self._aggregates: dict[StreamId, Aggregate] = {}
        #: 集約を読むだけの見え方（`finished` の問いに渡す）
        self.aggregates: Mapping[StreamId, Aggregate] = MappingProxyType(self._aggregates)
        self._log: list[Delivery] = []
        self._seqs: list[int] = []
        self._replay()
        self._checkpoints = store.checkpoints()
        self._register(names)

    def run(self) -> Outcome:
        self.recover()
        while True:
            if (delivery := self._next_delivery()) is not None:
                self._deliver(*delivery)
                continue
            if (reason := self._halted(self.aggregates)) is not None:
                return Outcome(LoopExit.STOPPED, reason)
            finished = self._finished(self.aggregates)
            item = self.inbox.take(0)
            # 終えた後の requests は処理しない（行は残るので、捨てたことにはならない）
            if item is None and not finished and (request := self._requests.next()) is not None:
                self._process_request(request)
                continue
            if item is None:
                if self.inbox.outstanding == 0:
                    return Outcome(LoopExit.FINISHED if finished else LoopExit.IDLE)
                # ランを終えていても、渡しかけの結果を黙って捨てない。札が返るまで待つ
                item = self.inbox.take(self._poll_interval)
                if item is None:
                    continue
            if isinstance(item, _Stop):
                return Outcome(LoopExit.STOPPED, item.reason)
            self._process_submission(item)

    def process(self, command: Command) -> str | None:
        """コマンドを 1 つ処理する。拒んだら理由を返す。

        同じ id のコマンドが出したイベントがすでにあれば、何もしない（受け直しで 2 回目に来た）。
        通ったら、出たイベントを 1 つのトランザクションで追記して確定させてから、集約に当てる。
        逆にすると、追記の前に落ちたとき、起きていないことを集約が覚えている。
        """
        if self._store.has_command(command.command_id):
            return None
        stream = command.target
        aggregate = self._aggregates.get(stream)
        if aggregate is None:
            aggregate = self._factory(stream)
        try:
            events = aggregate.handle(command)
        except Rejected as rejected:
            self._on_rejected(Rejection.of(command, rejected.reason, self._store.clock()))
            return rejected.reason
        if not events:
            return None
        stored = self._store.append(command.command_id, stream, aggregate.version, events)
        self._aggregates[stream] = aggregate
        for row, event in zip(stored, events, strict=True):
            # ここで落ちたら、集約は途中まで変わっている。続けずに落とし、再生し直す
            aggregate.apply(event, command.command_id)
            self._log.append(Delivery.of(row, event))
            self._seqs.append(row.seq)
        return None

    # --- 起動 ---

    def _replay(self) -> None:
        """events を頭から読み直し、集約と配る列を作り直す。チェックポイントはそのまま。"""
        history = decoded(self._store.read_all())
        self._aggregates.clear()
        self._aggregates.update(replay(history, self._factory))
        self._log = [Delivery.of(stored, event) for stored, event in history]
        self._seqs = [delivery.seq for delivery in self._log]

    def _register(self, names: Sequence[str]) -> None:
        """チェックポイントの無い受け手を、今の最後の seq から始めると記録する。

        すぐに書くのは、最初に配る前に落ちたとき、次の起動で始めの位置がずれないようにするため
        （書かないと、その間に足されたイベントを飛ばす）。
        """
        last = self._seqs[-1] if self._seqs else 0
        added = {name: last for name in names if name not in self._checkpoints}
        self._store.set_checkpoints(added)
        self._checkpoints.update(added)
        if last:
            for name in added:
                log.warning("受け手 %s を足した。seq %d より前のイベントには反応しない", name, last)

    @property
    def history(self) -> Sequence[Delivery]:
        """確定したイベントの全部（seq の順）。読むだけ。メインループのスレッドからだけ読む。"""
        return self._log

    def recover(self) -> None:
        """起動時の後始末。何度呼んでも 1 回だけ（`run` も最初に呼ぶ）。

        呼び直されたランで ResumeRun を出すなら、これの後に出す。先に出すと、まだ running のままの
        実行は再開の相手にならず、その後の後始末で interrupted になって止まったままになる。
        """
        if self._recovered:
            return
        self._recovered = True
        self.drain()
        self._interrupt_left_running()
        if self._paths is not None:
            prune_progress(self._paths, self._running())

    def drain(self) -> None:
        """未配達のイベントを受け手に配り切る。スレッドの結果と requests は受け取らない。"""
        while (delivery := self._next_delivery()) is not None:
            self._deliver(*delivery)

    def interrupt_running(
        self,
        cause: InterruptCause,
        name: str,
        keep: Callable[[ExecutionId], bool] = lambda _: False,
    ) -> None:
        """running の実行をドメインに聞き、`cause` で interrupted にする（`keep` が真のものを除く）。

        `name` は CommandId に入る名前。id は MarkInterrupted を出す前の版から決めるので、落ちて
        やり直しても同じ id になる。
        """
        for aggregate in list(self._aggregates.values()):
            if not isinstance(aggregate, RunningExecutions):
                continue
            # 1 つ目を当てると版が進むので、先に取る
            base = aggregate.event_id
            left = [e for e in aggregate.running_executions() if not keep(e)]
            for index, execution in enumerate(left):
                self.process(
                    MarkInterrupted(
                        command_id=CommandId.derived(base, name, index),
                        issuer=Issuer.driver(),
                        task=execution.task,
                        execution=execution,
                        cause=cause,
                    )
                )

    def _running(self) -> list[ExecutionId]:
        return [
            execution
            for aggregate in self._aggregates.values()
            if isinstance(aggregate, RunningExecutions)
            for execution in aggregate.running_executions()
        ]

    def _interrupt_left_running(self) -> None:
        """前の driver が走らせていた実行は、もう誰も見ていない。interrupted にして、再開をドメインに任せる。

        配り直しで反応が札を取った実行は、この driver が見ているので除く。
        """
        self.interrupt_running(InterruptCause.STARTUP, STARTUP, keep=self.inbox.holds)

    # --- 配達 ---

    def _next_delivery(self) -> tuple[tuple[Subscriber, ...], Delivery] | None:
        """seq の一番小さい未配達と、それをまだ受けていない受け手（登録した順）。"""
        best: int | None = None
        waiting: list[Subscriber] = []
        for subscriber in self._subscribers:
            index = bisect.bisect_right(self._seqs, self._checkpoints[subscriber.name])
            if index >= len(self._log) or (best is not None and index > best):
                continue
            if best is None or index < best:
                best, waiting = index, []
            waiting.append(subscriber)
        if best is None:
            return None
        return tuple(waiting), self._log[best]

    def _deliver(self, subscribers: Sequence[Subscriber], delivery: Delivery) -> None:
        """1 つのイベントを受け手に配り終えてから、チェックポイントを 1 回のトランザクションで書く。

        途中で落ちたら、このイベントのチェックポイントはどの受け手も進んでいないので、次の起動で
        全員が受け直す。受け直しで同じコマンドが出ても `CommandId` で弾かれ、反応は 2 回呼ばれても
        同じ結果になるように作るので、正しさは受け手ごとに書くときと変わらない。
        """
        for subscriber in subscribers:
            for command in subscriber.receive(delivery):
                # 返ったコマンドはどれも独立に処理する。拒んだことは on_rejected に残る
                self.process(command)
        done = {subscriber.name: delivery.seq for subscriber in subscribers}
        self._store.set_checkpoints(done)
        self._checkpoints.update(done)

    # --- 外からの受け取り ---

    def _process_submission(self, submission: _Submission) -> None:
        reason: str | None = None
        if (command := submission.command) is not None:
            reason = self.process(command)
            if reason is not None and submission.reply is not None:
                submission.reply(command, reason)
        if submission.settled is not None:
            submission.settled(reason)

    def _process_request(self, request: Request) -> None:
        """`requests` の 1 行を処理して消す。

        読めない行と、処理の途中で落ちた行も、記録してから消す。残すと先頭でつまずき続け、後ろの
        要求もランも進まない。処理の途中で落ちたときは、集約が途中まで変わっているかもしれないので、
        events から再生し直してから続ける。
        """
        failure: str | None = None
        crashed = False
        try:
            command = request.to_command()
        except UnreadableRequest as e:
            failure = f"要求を読めない: {e}"
        else:
            try:
                self.process(command)
            except Exception as e:
                failure = f"要求の処理で落ちた: {type(e).__name__}: {e}"
                crashed = True
                log.exception("requests の %s（%s）の処理で落ちた", request.id, request.type)
        if failure is not None:
            self._on_rejected(
                Rejection(
                    at=self._store.clock(),
                    type=request.type,
                    command_id=request.id,
                    issuer=codec.to_json(Issuer.cli()),
                    reason=failure,
                )
            )
        self._requests.delete(request.id)
        if crashed:
            self._replay()
