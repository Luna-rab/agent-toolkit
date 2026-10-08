"""AgentRouter: AgentCall.agent に従って実装を選ぶ。AgentCall の既定。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from autodev.adapters.agent.router import AgentRouter, UnknownAgent
from autodev.adapters.agent.types import AgentCall, Progress
from autodev.app.driving import assembly
from autodev.domain.value_objects.model_class import AgentKind
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.session_id import SessionId
from autodev.infra.paths import RunPaths
from autodev.infra.repo_config import RepoConfig

SESSION = SessionId("0b6f3c1e-9a8d-4c2b-8e7f-1a2b3c4d5e6f")


class Recorder:
    """start を受けた呼び出しを控え、決まった物を返す偽の実装。"""

    def __init__(self) -> None:
        self.calls: list[tuple[AgentCall, Callable[[Progress], None] | None]] = []
        self.process = object()

    def start(self, call: AgentCall, on_progress: Callable[[Progress], None] | None = None) -> Any:
        self.calls.append((call, on_progress))
        return self.process


def make_call(**fields: Any) -> AgentCall:
    values: dict[str, Any] = {
        "prompt": "やること",
        "cwd": "/w",
        "session": SESSION,
        "resume": False,
        "log_path": "/l",
    }
    values.update(fields)
    return AgentCall(**values)


def test_codexの呼び出しはcodexの実装のstartだけを呼びその返り値を返す():
    claude, codex = Recorder(), Recorder()
    router = AgentRouter({AgentKind.CLAUDE: claude, AgentKind.CODEX: codex})
    call = make_call(agent=AgentKind.CODEX)
    seen: list[Progress] = []
    assert router.start(call, seen.append) is codex.process
    assert codex.calls == [(call, seen.append)]
    assert claude.calls == []


def test_claudeの呼び出しはclaudeの実装のstartだけを呼びその返り値を返す():
    claude, codex = Recorder(), Recorder()
    router = AgentRouter({AgentKind.CLAUDE: claude, AgentKind.CODEX: codex})
    call = make_call(agent=AgentKind.CLAUDE)
    assert router.start(call) is claude.process
    assert claude.calls == [(call, None)]
    assert codex.calls == []


def test_登録の無いagentの呼び出しはUnknownAgentになりほかの実装は呼ばない():
    claude = Recorder()
    router = AgentRouter({AgentKind.CLAUDE: claude})
    with pytest.raises(UnknownAgent):
        router.start(make_call(agent=AgentKind.CODEX))
    assert claude.calls == []
    assert issubclass(UnknownAgent, ValueError)


def test_build_driverのAgentRouterはcopilotのAgentCallをCopilotRuntimeに渡す(
    monkeypatch: pytest.MonkeyPatch,
):
    copilot = Recorder()
    built: dict[str, Any] = {}
    monkeypatch.setattr(assembly, "CopilotRuntime", lambda *args, **kwargs: copilot)
    monkeypatch.setattr(assembly, "Driver", lambda *args, **kwargs: built.update(kwargs))
    assembly.build_driver(RunPaths.of(RunName("add-ttl")), RepoConfig())
    call = make_call(agent=AgentKind("copilot"))
    router = built["runtime"]
    assert router.start(call) is copilot.process
    assert copilot.calls == [(call, None)]


def test_build_driverのAgentRouterはclaudeとcodexの呼び出しをcopilotに渡さない(
    monkeypatch: pytest.MonkeyPatch,
):
    copilot = Recorder()
    built: dict[str, Any] = {}
    monkeypatch.setattr(assembly, "CopilotRuntime", lambda *args, **kwargs: copilot)
    monkeypatch.setattr(assembly, "Driver", lambda *args, **kwargs: built.update(kwargs))
    claude, codex = Recorder(), Recorder()
    monkeypatch.setattr(assembly, "AgentRuntime", lambda *args, **kwargs: claude)
    monkeypatch.setattr(assembly, "CodexRuntime", lambda *args, **kwargs: codex)
    assembly.build_driver(RunPaths.of(RunName("add-ttl")), RepoConfig())
    router = built["runtime"]
    router.start(make_call(agent=AgentKind.CLAUDE))
    router.start(make_call(agent=AgentKind.CODEX))
    assert len(claude.calls) == 1
    assert len(codex.calls) == 1
    assert copilot.calls == []


def test_agentを指定しないAgentCallはclaudeでrun_dirはNone():
    call = make_call()
    assert call.agent is AgentKind.CLAUDE
    assert call.run_dir is None
