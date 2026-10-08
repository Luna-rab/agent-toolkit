"""AgentRouter: `AgentCall.agent` に従って、Claude・Codex・Copilot の実装へ振り分ける。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ...domain.value_objects.model_class import AgentKind
from .types import AgentCall, Progress


class UnknownAgent(ValueError):
    """登録の無い agent の AgentCall を start した。"""


class AgentRouter:
    def __init__(self, runtimes: Mapping[AgentKind, Any]) -> None:
        self._runtimes = dict(runtimes)

    def start(self, call: AgentCall, on_progress: Callable[[Progress], None] | None = None) -> Any:
        runtime = self._runtimes.get(call.agent)
        if runtime is None:
            raise UnknownAgent(f"{call.agent.value} の実装が登録されていない")
        return runtime.start(call, on_progress)
