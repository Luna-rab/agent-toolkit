from __future__ import annotations

from enum import Enum


class IssuerKind(Enum):
    """コマンドを出した者の種類。driver が記録するので、ステージや統括は名乗れない。"""

    #: ラン統括（LLM）
    RUN_SUPERVISOR = "run-supervisor"
    #: タスクの統括。実装タスクは LLM、計画タスクと git 管理タスクはプログラム
    TASK_SUPERVISOR = "task-supervisor"
    POLICY = "policy"
    #: 反応（副作用を終えた後の続き）
    REACTION = "reaction"
    #: 実行器（ステージの結果と証拠を集めた変換層）
    EXECUTOR = "executor"
    #: `/autodev` の CLI（`requests` から届いた要求）
    CLI = "cli"
    #: アプリケーション層そのもの（パニック・起動時の後始末）
    DRIVER = "driver"
