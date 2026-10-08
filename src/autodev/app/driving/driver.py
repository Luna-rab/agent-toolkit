"""組み立ての根: ランディレクトリから driver を組み、メインループを回して、終わり方を終了コードにする。

```mermaid
flowchart TD
    open["events.db を開き、集約を再生する"] --> wire["受け手を登録する<br/>ポリシー（RECEIVERS の順）→ 反応"]
    wire --> fresh{"イベントがある?"}
    fresh -->|"無い（新しいラン）"| start["StartRun"]
    fresh -->|"ある（呼び直し）"| recover["起動時の後始末 → ResumeRun → 控えの知らせで統括を起こし直す"]
    start & recover --> loop["メインループを回す"]
    loop --> exit{"終わり方"}
    exit -->|"FINISHED"| ok["0"]
    exit -->|"STOPPED（パニック）"| panic["running の実行を MarkInterrupted(panic) → 3"]
    exit -->|"IDLE"| idle{"回答を待つ質問がある?（ドメインに聞く）"}
    idle -->|"ある"| wait["4"]
    idle -->|"無い"| stuck["1（進められないのに終わっていない）"]
```

ここで決めるのは、メインループの終わり方とドメインの問い（`Run.complete`・`Run.panicked`・`Questions.awaiting_answer`）の組み合わせを
表に引くことだけである。
"""

from __future__ import annotations

import logging
import shlex
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum

from ...adapters.agent.types import INTERRUPT_GRACE
from ...adapters.claude.guard import write_hook_settings
from ...adapters.process import children
from ...adapters.process.command import KILL_AFTER_SECONDS
from ...domain.aggregates.base import Aggregate
from ...domain.aggregates.questions import Questions
from ...domain.aggregates.run import Run
from ...domain.aggregates.task import Task
from ...domain.commands.run import Panic, ResumeRun, StartRun
from ...domain.events.run import RunStarted
from ...domain.policies.registry import RECEIVERS
from ...domain.streams import aggregate_for
from ...domain.supervision import Supervisor, wake_for
from ...domain.value_objects.command_id import CommandId
from ...domain.value_objects.event_id import EventId
from ...domain.value_objects.interrupt_cause import InterruptCause
from ...domain.value_objects.issuer import Issuer
from ...domain.value_objects.stream_id import StreamId
from ...infra.paths import RunPaths
from ...infra.store.eventstore import EventStore
from ...infra.store.rejections import RejectionLog
from ..stages.executor import StageExecutor, StagePrompt
from ..stages.prompts.assets import launcher
from ..stages.prompts.render import Prompts
from ..supervision.supervisors import AgentRuntimeLike, SupervisorRunner, SupervisorSetting
from .mainloop import Delivery, Inbox, LoopExit, MainLoop, Outcome, Subscriber
from .reactions import reactions
from .stopping import SignalStop

log = logging.getLogger(__name__)

#: パニックで止めた実行の MarkInterrupted の CommandId に入れる名前
PANIC = "driver-panic"
#: 呼び直されたときの ResumeRun の CommandId に入れる名前
RESUME = "driver-resume"
#: パニックで止めた実行の子が終わるまで待つ秒数。claude は interrupt から INTERRUPT_GRACE 秒で、
#: 決定的なステージの子は SIGTERM から KILL_AFTER_SECONDS 秒で kill されるので、その後まで待つ
STOP_JOIN_SECONDS = INTERRUPT_GRACE + KILL_AFTER_SECONDS + 20


class ExitCode(IntEnum):
    """終了コード。2 は使わない。"""

    #: ランを終えた（何本積んだかは status --json で見分ける）
    FINISHED = 0
    #: 起動できなかった・進められないのに終わっていない（driver の不具合）
    FAILED = 1
    #: パニック。原因を取り除いて同じラン名で呼び直す
    PANICKED = 3
    #: 回答待ちで、進められるタスクが無い。回答を置いて同じラン名で呼び直す
    AWAITING_ANSWER = 4
    #: そのランが無い（status --name で events.db が無い）。run は返さない
    RUN_NOT_FOUND = 5


def _run(aggregates: Mapping[StreamId, Aggregate]) -> Run:
    run = aggregates.get(StreamId.run())
    return run if isinstance(run, Run) else Run(StreamId.run())


def _questions(aggregates: Mapping[StreamId, Aggregate]) -> Questions:
    found = aggregates.get(StreamId.questions())
    return found if isinstance(found, Questions) else Questions(StreamId.questions())


def run_complete(aggregates: Mapping[StreamId, Aggregate]) -> bool:
    return _run(aggregates).complete


def panic_cause(aggregates: Mapping[StreamId, Aggregate]) -> str | None:
    return _run(aggregates).panic_cause


def exit_code(outcome: Outcome, aggregates: Mapping[StreamId, Aggregate]) -> ExitCode:
    """メインループの終わり方を、ドメインの問いと合わせて終了コードにする。"""
    if outcome.exit is LoopExit.FINISHED:
        return ExitCode.FINISHED
    if outcome.exit is LoopExit.STOPPED:
        return ExitCode.PANICKED if _run(aggregates).panicked else ExitCode.FAILED
    # 進められるものが無い。回答を待っているのか、待つものも無く止まったのかを見分ける
    if _questions(aggregates).awaiting_answer:
        return ExitCode.AWAITING_ANSWER
    return ExitCode.FAILED


@dataclass(frozen=True)
class DriverParts:
    """実行器を組むのに要る、driver の部品。"""

    paths: RunPaths
    inbox: Inbox
    aggregates: Callable[[], Mapping[StreamId, Aggregate]]
    prompts: Prompts
    #: 確定したイベントの列（実行器が RunStarted から対象リポジトリ・base・指示を読む。
    #: `stage_context.run_setting`）
    history: Callable[[], Sequence[Delivery]] = lambda: ()


@dataclass(frozen=True)
class StartRequest:
    """新しいラン名で `autodev run` を呼んだときの StartRun の中身（CLI が組む）。"""

    command: StartRun


class Driver:
    """ランディレクトリ 1 つの driver。`drive` を 1 回呼んで終わる。"""

    def __init__(
        self,
        paths: RunPaths,
        *,
        executor: Callable[[DriverParts], StageExecutor],
        runtime: AgentRuntimeLike,
        status_command: str | None = None,
        poll_interval: float | None = None,
        supervisor_setting: Callable[[SupervisorSetting], SupervisorSetting] = lambda s: s,
    ) -> None:
        self.paths = paths
        self.inbox = Inbox()
        self._loop: MainLoop | None = None
        command = status_command if status_command is not None else default_status_command(paths)
        self.prompts = Prompts(paths, lambda: self.loop.history, command)
        self.supervisors = SupervisorRunner(
            runtime,
            self.inbox,
            supervisor_setting(
                SupervisorSetting(
                    paths,
                    repository=self._repository,
                    resumes=lambda: _run(self.loop.aggregates).resumes,
                    models=lambda: _run(self.loop.aggregates).models,
                )
            ),
        )
        self.executor = executor(
            DriverParts(
                paths,
                self.inbox,
                lambda: self.loop.aggregates,
                self.prompts,
                lambda: self.loop.history,
            )
        )
        self._poll_interval = poll_interval

    @property
    def loop(self) -> MainLoop:
        if self._loop is None:
            raise RuntimeError("メインループをまだ組んでいない")
        return self._loop

    def _repository(self) -> str:
        """対象リポジトリの手元の checkout（RunStarted に書いてある）。"""
        for delivery in self.loop.history:
            if isinstance(delivery.event, RunStarted):
                return delivery.event.repository.value
        return str(self.paths.root)

    def _subscribers(self) -> list[Subscriber]:
        policies = [
            Subscriber(name, lambda d, receive=receive: receive(d.event, d.event_id))
            for name, receive in RECEIVERS.items()
        ]
        return policies + reactions(
            paths=self.paths,
            aggregates=lambda: self.loop.aggregates,
            inbox=self.inbox,
            executor=self.executor,
            supervisors=self.supervisors,
            prompts=self.prompts,
        )

    def _restore_prompt(self, supervisor: Supervisor, source: EventId) -> StagePrompt | None:
        """控えに残った知らせのプロンプト。起こしたイベントを確定したイベントの列から探す。"""
        delivery = next((d for d in self.loop.history if d.event_id == source), None)
        if delivery is None:
            return None
        wake = wake_for(delivery.event, delivery.event_id)
        if wake is None or wake.supervisor != supervisor:
            return None
        return self.prompts.wake(wake, delivery, self.loop.aggregates)

    def _begin_left_requested(self) -> None:
        """前の driver が BeginStage を受け取る前に止まった実行を、実行器に始めさせ直す。

        どれが始まっていないかはドメインに聞く（`Task.requested_executions`）。配り直しで反応がもう
        頼んだ実行（札を持っている）は除く。
        """
        for aggregate in list(self.loop.aggregates.values()):
            if not isinstance(aggregate, Task):
                continue
            for execution in aggregate.requested_executions():
                if not self.inbox.holds(execution):
                    self.executor.begin(execution, self.inbox.expect(execution))

    def drive(self, start: StartRequest | None = None) -> ExitCode:
        """ランを進め、終了コードを返す。`start` は新しいラン名のときだけ渡す。

        シグナルのハンドラは頭から子を待ち終えるまで付けておく（`app/driving/stopping.py`）。付いていない間に
        シグナルが来ると、子を待たずに driver だけが終わり、子が残る。
        """
        self.paths.root.mkdir(parents=True, exist_ok=True)
        write_hook_settings(self.paths.guard)
        children.track_in(self.paths.children)
        stopper = SignalStop(
            panic=self._submit_panic, kill=self._kill_children, stop=self.inbox.stop
        )
        try:
            with stopper:
                return self._drive(start, stopper)
        finally:
            children.track_in(None)

    def _submit_panic(self, cause: str, rejected: Callable[[str], None]) -> None:
        self.inbox.expect().submit(
            Panic(
                command_id=CommandId(f"signal/{uuid.uuid4().hex}"),
                issuer=Issuer.driver(),
                cause=cause,
            ),
            reply=lambda _command, reason: rejected(reason),
        )

    def _kill_children(self) -> None:
        for child in children.kill_survivors(self.paths.children):
            log.warning("子をグループごと止めた: pid %d（%s）", child.pid, child.command)

    def _drive(self, start: StartRequest | None, stopper: SignalStop) -> ExitCode:
        store = EventStore.open(self.paths.events_db)
        try:
            kwargs = {} if self._poll_interval is None else {"poll_interval": self._poll_interval}
            self._loop = MainLoop(
                store,
                aggregate_for,
                self._subscribers(),
                on_rejected=RejectionLog(self.paths.rejections),
                finished=run_complete,
                halted=panic_cause,
                inbox=self.inbox,
                paths=self.paths,
                **kwargs,
            )
            loop = self._loop
            fresh = store.last_seq() == 0
            if fresh != (start is not None):
                # 新しいランに指示が無い・既にあるランに指示を足した（黙って捨てない）
                log.error("ラン %s: %s", self.paths.name, "指示が無い" if fresh else "既にある")
                return ExitCode.FAILED
            if start is not None:
                if (reason := loop.process(start.command)) is not None:
                    log.error("ランを始められない: %s", reason)
                    return ExitCode.FAILED
            else:
                loop.recover()
                run = _run(loop.aggregates)
                loop.process(
                    ResumeRun(
                        command_id=CommandId.derived(run.event_id, RESUME),
                        issuer=Issuer.driver(),
                    )
                )
                self.supervisors.restore(
                    self._restore_prompt, reported=_run(loop.aggregates).reported_failure
                )
                self._begin_left_requested()
            outcome = loop.run()
            stopped = outcome.exit is LoopExit.STOPPED
            if stopped and (_run(loop.aggregates).panicked or stopper.received):
                # 走っている実行を interrupted にし、反応（実行器の interrupt）に止めさせる
                loop.interrupt_running(InterruptCause.PANIC, PANIC)
                loop.drain()
                # 子は新しいセッションで起こしているので、driver が先に終わると止まらずに残る。
                # ステージと統括を同時に止め、同じ締め切りまで待つ
                self.supervisors.shutdown()
                deadline = time.monotonic() + STOP_JOIN_SECONDS
                self.executor.join(max(0.0, deadline - time.monotonic()))
                self.supervisors.join(max(0.0, deadline - time.monotonic()))
                if left := children.survivors(self.paths.children):
                    log.error(
                        "待ち切れずに残った子: %s",
                        ", ".join(f"pid {c.pid}（{c.command}）" for c in left),
                    )
            if stopper.received and outcome.exit is not LoopExit.FINISHED:
                return ExitCode.PANICKED
            return exit_code(outcome, loop.aggregates)
        finally:
            self.supervisors.shutdown()
            store.close()


def default_status_command(paths: RunPaths) -> str:
    """統括に渡す `<状態を読むコマンド>`。CLI の `status --json` の形が決まったら、そちらに合わせる。"""
    return f"{shlex.quote(launcher())} status --json --name {paths.name}"
