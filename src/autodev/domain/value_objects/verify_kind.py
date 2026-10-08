from __future__ import annotations

from enum import Enum


class VerifyKind(Enum):
    """検証コマンドの種類。値は計画の提案の欄名。"""

    #: 計画がタスクごとに選ぶ、そのタスクのテスト
    TASK_TESTS = "taskTests"
    #: lint・型検査のように速く、どのタスクを積んだ時点でも通るもの
    QUICK_CHECKS = "quickChecks"
    #: テスト全体のように遅く、ほかの場所を壊していないかを確かめるもの
    REGRESSION_TESTS = "regressionTests"
