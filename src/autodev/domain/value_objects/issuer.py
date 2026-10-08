from __future__ import annotations

from dataclasses import dataclass

from .base import InvalidValue
from .event_id import EventId
from .execution_id import ExecutionId
from .issuer_kind import IssuerKind
from .session_id import SessionId
from .task_id import TaskId
from .task_kind import TaskKind


@dataclass(frozen=True)
class Issuer:
    """コマンドを出した者。`kind` ごとに要る欄が決まっている。

    タスクの統括は、実装タスクなら LLM（セッションを持つ）、計画タスクと git 管理タスクなら
    プログラム（セッションを持たない）で、タスクの id の種類で見分ける。名乗りが宛先と合うかは、
    集約の土台が確かめる（`aggregates/base.py` の「名乗りは宛先と合う」）。
    """

    kind: IssuerKind
    #: TASK_SUPERVISOR: どのタスクの統括か
    task: TaskId | None = None
    #: LLM の統括（ラン統括と実装タスクの統括）のセッション
    session: SessionId | None = None
    #: POLICY・REACTION: 受け手の名前
    name: str | None = None
    #: POLICY・REACTION: 受けたイベント
    event: EventId | None = None
    #: EXECUTOR: どの実行の結果か
    execution: ExecutionId | None = None

    def __post_init__(self) -> None:
        needs = {
            IssuerKind.RUN_SUPERVISOR: ("session",),
            IssuerKind.TASK_SUPERVISOR: ("task",),
            IssuerKind.POLICY: ("name", "event"),
            IssuerKind.REACTION: ("name", "event"),
            IssuerKind.EXECUTOR: ("execution",),
        }.get(self.kind, ())
        missing = [field for field in needs if getattr(self, field) is None]
        if missing:
            raise InvalidValue(f"{self.kind.value} の Issuer に {', '.join(missing)} が無い")
        if self.kind is IssuerKind.TASK_SUPERVISOR and self.task is not None:
            llm = self.task.kind is TaskKind.IMPLEMENTATION
            if llm != (self.session is not None):
                raise InvalidValue(
                    "セッションを持つのは実装タスクの統括（LLM）だけ。"
                    f"計画タスクと git 管理タスクの統括はプログラム: {self.task}"
                )

    @classmethod
    def run_supervisor(cls, session: SessionId) -> Issuer:
        return cls(IssuerKind.RUN_SUPERVISOR, session=session)

    @classmethod
    def task_supervisor(cls, task: TaskId, session: SessionId | None = None) -> Issuer:
        return cls(IssuerKind.TASK_SUPERVISOR, task=task, session=session)

    @classmethod
    def policy(cls, name: str, event: EventId) -> Issuer:
        return cls(IssuerKind.POLICY, name=name, event=event)

    @classmethod
    def reaction(cls, name: str, event: EventId) -> Issuer:
        return cls(IssuerKind.REACTION, name=name, event=event)

    @classmethod
    def executor(cls, execution: ExecutionId) -> Issuer:
        return cls(IssuerKind.EXECUTOR, execution=execution)

    @classmethod
    def cli(cls) -> Issuer:
        return cls(IssuerKind.CLI)

    @classmethod
    def driver(cls) -> Issuer:
        return cls(IssuerKind.DRIVER)
