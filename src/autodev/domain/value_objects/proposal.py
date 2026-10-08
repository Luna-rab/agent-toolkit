from __future__ import annotations

from dataclasses import dataclass, field

from ..codec import RENAMED_FROM
from .base import InvalidValue
from .design_version import DesignVersion
from .finding_transfer import FindingTransfer
from .planned_task import PlannedTask
from .task_id import TaskId
from .verify_command import VerifyCommand


@dataclass(frozen=True)
class Proposal:
    """計画ステージ（Plan・Replan・Revise）が返した、確定前の提案。

    設計の本文は `design/v<版>.md` にあり、ここは版の番号だけを持つ（イベントが本文で膨らまない）。
    タスクの一覧はポリシーが ApplyPlan に変えるときに要るので、ここに持つ。
    """

    design: DesignVersion
    tasks: tuple[PlannedTask, ...]
    #: ラン共通の軽い検査。Gate と、git 管理タスクの統合検査で流す
    quick_checks: tuple[VerifyCommand, ...] = ()
    #: ラン共通の回帰テスト。git 管理タスクの統合検査でだけ流す
    regression_tests: tuple[VerifyCommand, ...] = field(
        default=(), metadata={RENAMED_FROM: ("verify",)}
    )
    #: 止める候補（走っている実装タスク）
    stop: frozenset[TaskId] = frozenset()
    #: 破棄する候補（積んだタスク）。再利用できない理由は設計の本文に書く
    discard: frozenset[TaskId] = frozenset()
    carry: tuple[FindingTransfer, ...] = ()
    #: 計画ステージが自分の判断で決めたこと。概要 PR の判断ログに載せる
    decisions: tuple[str, ...] = ()
    #: 計画ステージがスコープの外にしたもの。概要 PR の判断ログに載せる
    deferrals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise InvalidValue("提案の中でタスクの id が重なっている")
