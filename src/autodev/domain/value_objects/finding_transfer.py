from __future__ import annotations

from dataclasses import dataclass

from .finding_id import FindingId
from .task_id import TaskId


@dataclass(frozen=True)
class FindingTransfer:
    """再計画で、未解決の指摘を別のタスクへ移す。"""

    finding: FindingId
    from_task: TaskId
    to_task: TaskId
