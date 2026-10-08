from __future__ import annotations

from dataclasses import dataclass

from .design_cause import DesignCause
from .design_version import DesignVersion


@dataclass(frozen=True)
class DesignJudgement:
    """DesignJudge の設計の分類（MarkReverted・MarkAmbiguous）。"""

    cause: DesignCause
    #: 前の版に戻ったなら、戻った先の版
    reverted_to: DesignVersion | None = None
    #: そう判定した理由（回答を待つエスカレーションの理由になる）
    reason: str = ""
    #: 受入条件が曖昧なら、ユーザーに聞くこと
    question: str | None = None
