from __future__ import annotations

from enum import Enum


class EscalationKind(Enum):
    """エスカレーションの種類。"""

    STALL = "stall"
    DESIGN_GAP = "design-gap"
    TEST_CONFLICT = "test-conflict"
    ASK = "ask"
    RED_CHECK_FAILED = "red-check-failed"
    QUICK_CHECK_FAILED = "quick-check-failed"
    SUPERVISOR_FINDING = "supervisor-finding"
    UNTESTED_CHANGE = "untested-change"
    GATE_UNFIXABLE = "gate-unfixable"
    STAGE_ERRORS = "stage-errors"
    DESIGN_AMBIGUOUS = "design-ambiguous"
    DESIGN_REVERTED = "design-reverted"
    DESIGN_ROUNDS_EXHAUSTED = "design-rounds-exhausted"
    INTEGRATION_FAILED = "integration-failed"
    NEEDS_REPLAN = "needs-replan"
    NEEDS_HUMAN = "needs-human"
    QUESTION = "question"
    #: ステージの結果を受け取る側（Design・指摘の台帳・Stack）が受けなかった（使った版が古い・
    #: 確定していない提案がある・無い指摘を判定した、など）。cursor は進めない
    RESULT_REFUSED = "result-refused"
    #: LLM の統括が、差し戻しと呼び直しを使い切っても知らせに応じなかった（driver が上げる）。
    #: タスク統括ならラン統括が、ラン統括ならユーザーが受ける（EscalationRouter）
    SUPERVISOR_FAILED = "supervisor-failed"
