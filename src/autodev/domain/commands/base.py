"""コマンド。

コマンドは「〜せよ」という命令で、宛先の集約は 1 つ（`target`）。どれも `command_id` と、出した者
（`issuer`）を持つ。出してよい者は `ISSUERS` に書き、外れた者のコマンドは集約の土台が拒む
（`aggregates/base.py`）。出す者は driver が記録するので、ステージや統括は名乗りで偽れない。

計画タスクと git 管理タスクの統括（プログラム）は、`TASK_SUPERVISOR` として出す。タスクの統括が
名乗ってよいタスクは `supervised_task`、実行器が名乗ってよい実行は `reported_execution` で、
集約の土台がどちらも名乗りと照らし合わせる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..value_objects.command_id import CommandId
from ..value_objects.execution_id import ExecutionId
from ..value_objects.issuer import Issuer
from ..value_objects.issuer_kind import IssuerKind
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId


@dataclass(frozen=True, kw_only=True)
class Command:
    command_id: CommandId
    issuer: Issuer

    #: 出してよい者
    ISSUERS: ClassVar[frozenset[IssuerKind]] = frozenset()
    #: 宛先の集約の名前（`events/registry.py` の `EVENTS_BY_AGGREGATE` のキーと同じ）
    AGGREGATE: ClassVar[str] = ""

    @property
    def target(self) -> StreamId:
        raise NotImplementedError

    @property
    def supervised_task(self) -> TaskId | None:
        """タスクの統括が出すとき、名乗ってよいタスク。None なら、タスクの統括は出せない。"""
        return None

    @property
    def reported_execution(self) -> ExecutionId | None:
        """実行器が出すとき、名乗ってよい実行。None なら、実行器は出せない。"""
        execution = getattr(self, "execution", None)
        return execution if isinstance(execution, ExecutionId) else None
