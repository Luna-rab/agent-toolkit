"""LLM の統括を起こす口（`app/supervision/supervisors.py`）。偽の AgentRuntime と本物の Inbox で、ターンの扱いを見る。

メインループの代わりに、検査が Inbox から札を受け取り、`settled` を呼ぶ。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from autodev.adapters.agent.types import AgentCall, AgentOutcome, Ending
from autodev.adapters.claude.guard import GUARD_ENV
from autodev.app.driving.mainloop import Inbox
from autodev.app.stages.stage_context import StagePrompt
from autodev.app.supervision.supervisors import SupervisorRunner, SupervisorSetting
from autodev.domain.commands.run import Panic, ReportSupervisorFailure
from autodev.domain.commands.task import AcceptFlow
from autodev.domain.supervision import Supervisor
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.task_id import TaskId
from autodev.infra.paths import RunPaths

T1 = TaskId("task1")
TASK = Supervisor.of(T1)
RUN = Supervisor.run()
SOURCE = EventId("task/git#5")
FLOW = {"decision": "run-flow", "runFlow": {"steps": [{"stage": "Impl"}], "respondsTo": None}}

Script = Callable[[AgentCall], AgentOutcome]


def answered(call: AgentCall, structured: dict | None = FLOW) -> AgentOutcome:
    return AgentOutcome(
        ending=Ending.RESULT, exit_code=0, session=call.session, structured=structured
    )


@dataclass
class FakeRuntime:
    script: Script
    calls: list[AgentCall] = field(default_factory=list)
    #: 立っている間、wait は interrupt されるまで返らない
    hold: threading.Event = field(default_factory=threading.Event)

    def start(self, call: AgentCall) -> FakeProcess:
        self.calls.append(call)
        return FakeProcess(self, call)


@dataclass
class FakeProcess:
    runtime: FakeRuntime
    call: AgentCall
    stopped: threading.Event = field(default_factory=threading.Event)

    def wait(self) -> AgentOutcome:
        if self.runtime.hold.is_set():
            self.stopped.wait(5)
            return AgentOutcome(ending=Ending.KILLED, exit_code=None, session=self.call.session)
        return self.runtime.script(self.call)

    def interrupt(self, reason: str) -> None:
        self.stopped.set()


@dataclass
class Rig:
    root: Path
    script: Script = answered
    inbox: Inbox = field(default_factory=Inbox)
    #: 対象リポジトリを読んだスレッドの名前
    readers: list[str] = field(default_factory=list)
    runtime: FakeRuntime = field(init=False)

    def __post_init__(self) -> None:
        self.runtime = FakeRuntime(self.script)

    @property
    def paths(self) -> RunPaths:
        return RunPaths(RunName("add-cache"), self.root)

    def runner(self, resumes: int = 0) -> SupervisorRunner:
        def repository() -> str:
            self.readers.append(threading.current_thread().name)
            return "/repo"

        return SupervisorRunner(
            self.runtime,
            self.inbox,
            SupervisorSetting(self.paths, repository=repository, resumes=lambda: resumes),
        )

    def take(self, refused: str | None = None, *, settle: bool = True):
        """スレッドが返した札を 1 つ受け取り、メインループが処理し終えたことにする。

        `settle` が偽なら、処理し終えたと知らせない（その前に driver が落ちた）。
        """
        item = self.inbox.take(5)
        assert item is not None and not hasattr(item, "reason"), item
        if settle and item.settled is not None:
            item.settled(refused)
        return item.command


def prompt(text: str = "## `<通知>`\n\n知らせ") -> StagePrompt:
    return StagePrompt(text)


def test_配り直しと控えからの起こし直しが重なっても同じ知らせでは1回だけ起こす(tmp_path: Path):
    rig = Rig(tmp_path)
    runner = rig.runner()
    runner.wake(TASK, prompt(), SOURCE)
    runner.restore(lambda supervisor, source: prompt())
    runner.wake(TASK, prompt(), SOURCE)
    assert isinstance(rig.take(), AcceptFlow)
    assert rig.inbox.outstanding == 0
    assert len(rig.runtime.calls) == 1
    # 判断が確定した知らせは、呼び直した driver の配り直しでも起こさない
    again = rig.runner()
    again.restore(lambda supervisor, source: prompt())
    again.wake(TASK, prompt(), SOURCE)
    assert rig.inbox.outstanding == 0
    assert len(rig.runtime.calls) == 1


def test_判断のidは知らせだけで決まり差し戻しても呼び直しても同じになる(tmp_path: Path):
    """拒まれた /0 → 直した判断が通る → 起こし終えたと控える前に落ちる → 呼び直した driver が起こし直す、
    の順でも、起こし直した判断は通った判断と同じ id になり、`has_command` で弾かれる。"""
    rig = Rig(tmp_path)
    rig.runner().wake(TASK, prompt(), SOURCE)
    expected = CommandId.derived(SOURCE, "supervisor-task-task1")
    assert rig.take(refused="拒んだ").command_id == expected
    # 通ったが、settled（起こし終えたと控える）の前に落ちた
    assert rig.take(settle=False).command_id == expected
    resumed = Rig(tmp_path)
    resumed.runner(resumes=1).restore(lambda supervisor, source: prompt())
    assert resumed.take().command_id == expected


def test_応じなかったとRunが受けた知らせは呼び直しても起こさず控えを片付ける(tmp_path: Path):
    rig = Rig(tmp_path)
    runner = rig.runner()
    runner.sessions.session(TASK)
    runner.sessions.add_pending(TASK, SOURCE)
    old, _ = runner.sessions.session(TASK)
    runner.restore(lambda supervisor, source: prompt(), reported=lambda source: source == SOURCE)
    assert rig.inbox.outstanding == 0
    assert rig.runtime.calls == []
    assert runner.sessions.pending() == []
    # 次に起こすときは、新しいセッションで知らせから始める（上げ終えた後と同じ）
    assert runner.sessions.session(TASK)[0] != old


def test_ターンの途中で止めた知らせは呼び直したdriverが同じセッションで起こし直す(tmp_path: Path):
    rig = Rig(tmp_path)
    rig.runtime.hold.set()
    runner = rig.runner()
    runner.wake(TASK, prompt(), SOURCE)
    while not rig.runtime.calls:
        threading.Event().wait(0.01)
    runner.shutdown()
    # 止めたターンは何も渡さない（札は取り消す）
    assert rig.take() is None
    rig.runtime.hold.clear()
    again = rig.runner()
    again.restore(lambda supervisor, source: prompt("## `<通知>`\n\n控えから"))
    assert isinstance(rig.take(), AcceptFlow)
    first, second = rig.runtime.calls
    assert second.prompt == "## `<通知>`\n\n控えから"
    assert second.session == first.session
    assert again.sessions.pending() == []


def test_落ち続けたら新しいセッションの知らせからもう1回起こしそれでも駄目なら上げる(
    tmp_path: Path,
):
    def script(call: AgentCall) -> AgentOutcome:
        return AgentOutcome(ending=Ending.NO_RESULT, exit_code=1, session=call.session, stderr="x")

    rig = Rig(tmp_path, script)
    runner = rig.runner()
    runner.wake(TASK, prompt("知らせ"), SOURCE)
    failure = rig.take()
    assert isinstance(failure, ReportSupervisorFailure)
    assert (failure.supervisor, failure.notice) == (T1, SOURCE)
    calls = list(rig.runtime.calls)
    assert len(calls) == 3
    assert calls[0].session == calls[1].session != calls[2].session
    assert [c.prompt for c in calls] == ["知らせ"] * 3
    # 上げた知らせは起こし終えたと控え、次は新しいセッションで起こす
    assert runner.sessions.pending() == []
    rig.runtime.script = answered
    runner.wake(TASK, prompt(), EventId("run#9"))
    rig.take()
    assert rig.runtime.calls[-1].session not in {c.session for c in calls}


def test_こちらが止めたなら止めた理由を落ちた理由にする(tmp_path: Path):
    """interrupt の result の `errors` は診断の文で、止めた理由ではない（段 6 の実測）。"""

    def script(call: AgentCall) -> AgentOutcome:
        return AgentOutcome(
            ending=Ending.RESULT,
            exit_code=1,
            session=call.session,
            subtype="error_during_execution",
            is_error=True,
            text="落ちた",
            interrupted="制限時間を超えた",
        )

    rig = Rig(tmp_path, script)
    rig.runner().wake(TASK, prompt(), SOURCE)
    failure = rig.take()
    assert isinstance(failure, ReportSupervisorFailure)
    assert failure.reason.endswith(": 制限時間を超えた")


def test_判断を返さずに普通に終わったら同じセッションに差し戻す(tmp_path: Path):
    outcomes = iter([None, FLOW])

    def script(call: AgentCall) -> AgentOutcome:
        return answered(call, next(outcomes))

    rig = Rig(tmp_path, script)
    rig.runner().wake(TASK, prompt(), SOURCE)
    assert isinstance(rig.take(), AcceptFlow)
    first, second = rig.runtime.calls
    assert "判断が返っていない" in (second.prompt or "")
    assert second.session == first.session


def test_パニックを返したら次の統括のターンを起こさない(tmp_path: Path):
    def script(call: AgentCall) -> AgentOutcome:
        return AgentOutcome(
            ending=Ending.RESULT,
            exit_code=1,
            session=call.session,
            is_error=True,
            rate_limited=True,
        )

    rig = Rig(tmp_path, script)
    runner = rig.runner(resumes=2)
    runner.wake(TASK, prompt(), SOURCE)
    panic = rig.take()
    assert isinstance(panic, Panic)
    # 呼び直した後に同じ知らせでまた当たっても、前のパニックとは別のコマンドになる
    assert panic.command_id == CommandId.derived(SOURCE, "supervisor-panic-task-task1", 2)
    runner.wake(RUN, prompt(), EventId("run#3"))
    assert rig.inbox.outstanding == 0
    assert len(rig.runtime.calls) == 1
    # 起こせなかった知らせは控えに残り、呼び直した driver が起こす
    assert (RUN, EventId("run#3")) in runner.sessions.pending()


def test_対象リポジトリは起こす時点にメインループのスレッドで読む(tmp_path: Path):
    rig = Rig(tmp_path)
    rig.runner().wake(TASK, prompt(), SOURCE)
    rig.take(refused="拒んだ")
    rig.take()
    # 差し戻したターンも、起こした時点の値を使う
    assert rig.readers == [threading.current_thread().name]
    for call in rig.runtime.calls:
        guard = json.loads((call.env or {})[GUARD_ENV])
        assert guard["context"]["targetRepo"] == "/repo"
