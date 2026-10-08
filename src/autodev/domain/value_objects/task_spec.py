from __future__ import annotations

from dataclasses import dataclass, field

from ..codec import RENAMED_FROM
from .base import non_blank
from .verify_command import VerifyCommand


@dataclass(frozen=True)
class TaskSpec:
    """タスクの中身。計画ステージが返し、再計画で書き換わる。"""

    title: str
    dod: str = ""
    acceptance: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    #: 入口（次のステージが読み始める場所）
    entry_points: tuple[str, ...] = ()
    #: 境界の形（公開する型・関数の形）
    boundary: str = ""
    #: このタスクのテストを流す検証コマンド。実装タスクの ConfirmRed と Gate で流す
    task_tests: tuple[VerifyCommand, ...] = field(default=(), metadata={RENAMED_FROM: ("verify",)})

    def __post_init__(self) -> None:
        non_blank("タスクの件名", self.title)
