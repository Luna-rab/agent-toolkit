"""実行器の検査で使う偽物と、tmp_path に作る本物の git リポジトリ（bare の origin を含む）。

本物の claude・gh は叩かない。claude は `FakeRuntime`（AgentRuntime の受け口の偽物）、gh は
`test_adapter_forge.FakeGh`（引数を記録する偽のコマンド）に差し替える。
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from test_adapter_forge import FakeGh

from autodev.adapters.agent.types import AgentCall, AgentOutcome, Ending, Progress
from autodev.adapters.github.forge import Forge
from autodev.adapters.github.git import Git
from autodev.adapters.process.process import ProcessRunner
from autodev.app.driving.mainloop import Inbox
from autodev.app.stages.executor import Executor, StagePrompt
from autodev.app.stages.stage_context import RunSetting, StageContext
from autodev.domain.aggregates.base import Aggregate
from autodev.domain.aggregates.review_ledger import ReviewLedger
from autodev.domain.aggregates.run import Run
from autodev.domain.aggregates.stack import Stack
from autodev.domain.aggregates.task import Task
from autodev.domain.commands.base import Command
from autodev.domain.events.base import Event
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.session_id import SessionId
from autodev.domain.value_objects.stream_id import StreamId
from autodev.infra.paths import RunPaths

RUN = RunName("r")
BASE = BranchName("main")


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def commit(cwd: Path, path: str, text: str, message: str = "change") -> str:
    target = cwd / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    sh(cwd, "add", path)
    sh(cwd, "commit", "-q", "-m", message)
    return sh(cwd, "rev-parse", "HEAD").strip()


def make_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """origin（bare）と、それを clone した対象リポジトリ。"""
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    commit(work, "a.txt", "1\n2\n3\n", "first")
    sh(work, "push", "-q", "origin", "main")
    sh(work, "remote", "set-head", "origin", "--auto")
    return work


# --- claude の偽物 ---


def outcome(
    call: AgentCall, structured: Mapping[str, Any] | None = None, **fields: Any
) -> AgentOutcome:
    """result が返って終わった（既定は成功）。本物はセッションを開けたら init を出すので、既定は init を
    受けたことにする（受けずに `--resume` から終わると、続けられなかったと数える）。"""
    values: dict[str, Any] = {
        "ending": Ending.RESULT,
        "exit_code": 0,
        "session": call.session,
        "subtype": "success",
        "structured": structured,
        "log_path": call.log_path,
        "initialized": True,
    }
    values.update(fields)
    return AgentOutcome(**values)


Behavior = Callable[[AgentCall, "FakeProcess"], AgentOutcome]


class FakeProcess:
    def __init__(
        self, call: AgentCall, behave: Behavior, on_progress: Callable[[Progress], None] | None
    ) -> None:
        self.call = call
        self.behave = behave
        self.on_progress = on_progress
        self.interrupted: str | None = None
        self.stop = threading.Event()

    def wait(self) -> AgentOutcome:
        Path(self.call.log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.call.log_path, "a", encoding="utf-8") as log:
            log.write('{"type": "autodev/call"}\n')
        return self.behave(self.call, self)

    def interrupt(self, reason: str) -> None:
        self.interrupted = reason
        self.stop.set()

    def kill(self) -> None:
        self.stop.set()


@dataclass
class FakeRuntime:
    behaviors: list[Behavior] = field(default_factory=list)
    calls: list[AgentCall] = field(default_factory=list)
    processes: list[FakeProcess] = field(default_factory=list)

    def start(
        self, call: AgentCall, on_progress: Callable[[Progress], None] | None = None
    ) -> FakeProcess:
        self.calls.append(call)
        process = FakeProcess(call, self.behaviors.pop(0), on_progress)
        self.processes.append(process)
        return process


@dataclass
class FakePrompts:
    calls: list[tuple[str, StageContext]] = field(default_factory=list)

    def prompt(
        self, context: StageContext, aggregates: Mapping[StreamId, Aggregate]
    ) -> StagePrompt:
        self.calls.append(("prompt", context))
        return StagePrompt(f"プロンプト {context.execution}", "必須ルール")

    def continuation(self, context: StageContext) -> str:
        self.calls.append(("continuation", context))
        return "止めたところから続けて"


# --- メインループの代わり ---


def factory(stream: StreamId) -> Aggregate:
    if stream.value.startswith("task/"):
        return Task(stream)
    if stream.is_review:
        return ReviewLedger(stream)
    if stream == StreamId.stack():
        return Stack(stream)
    return Run(stream)


class World:
    """集約を持ち、札で返ったコマンドを処理する（メインループの代わり）。"""

    def __init__(self) -> None:
        self.aggregates: dict[StreamId, Aggregate] = {}
        self.events: list[Event] = []
        self.inbox = Inbox()

    def __call__(self, command: Command) -> list[Event]:
        aggregate = self.aggregates.get(command.target) or factory(command.target)
        events = aggregate.handle(command)
        for event in events:
            aggregate.apply(event, command.command_id)
        self.aggregates[command.target] = aggregate
        self.events += events
        return events

    def task(self, stream: StreamId) -> Task:
        found = self.aggregates[stream]
        assert isinstance(found, Task)
        return found

    def submitted(self) -> list[Command]:
        found: list[Command] = []
        while (item := self.inbox.take(0)) is not None:
            command = getattr(item, "command", None)
            if command is not None:
                found.append(command)
        return found

    def deliver(self) -> list[Event]:
        """札で返ったコマンドを、返った順に処理する。"""
        events: list[Event] = []
        for command in self.submitted():
            events += self(command)
        return events


@dataclass
class Env:
    repo: Path
    paths: RunPaths
    setting: RunSetting
    world: World
    runtime: FakeRuntime
    prompts: FakePrompts
    gh: FakeGh
    executor: Executor
    git: Git
    sessions: list[SessionId]

    def stage_log(self, execution: ExecutionId) -> Path:
        return self.paths.stage_log(execution)

    def begin(self, execution: ExecutionId) -> list[Event]:
        self.executor.begin(execution, self.world.inbox.expect(execution))
        self.executor.join()
        return self.world.deliver()

    def run(self, execution: ExecutionId) -> list[Event]:
        self.executor.run(execution, self.world.inbox.expect(execution))
        self.executor.join()
        return self.world.deliver()


def make_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **setting: Any) -> Env:
    repo = make_repo(tmp_path, monkeypatch)
    paths = RunPaths(RUN, tmp_path / "state" / "r")
    run_setting = RunSetting(
        paths=paths, repository=repo, base=BASE, instruction="キャッシュを足す", **setting
    )
    world = World()
    runtime = FakeRuntime()
    prompts = FakePrompts()
    gh = FakeGh(tmp_path, monkeypatch)
    git = Git(repo)
    sessions = [SessionId(f"00000000-0000-4000-8000-{n:012d}") for n in range(1, 50)]
    issued = iter(sessions)
    executor = Executor(
        setting=run_setting,
        aggregates=lambda: world.aggregates,
        prompts=prompts,
        runtime=runtime,
        git=git,
        forge=Forge(gh=str(gh.path)),
        runner=ProcessRunner(timeout=60),
        clock=lambda: "2026-10-02T00:00:00Z",
        new_session=lambda: next(issued),
    )
    return Env(repo, paths, run_setting, world, runtime, prompts, gh, executor, git, sessions)
