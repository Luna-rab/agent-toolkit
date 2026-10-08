from __future__ import annotations

from dataclasses import dataclass

from .base import InvalidValue, non_blank
from .decision_origin import DecisionOrigin
from .question_id import QuestionId


@dataclass(frozen=True)
class Decision:
    """回答で決めたこと。出どころを持つ。

    ユーザーの回答は、その質問の QuestionId を持つ。ラン統括が自分で答えたものは持たない。
    """

    text: str
    origin: DecisionOrigin
    question: QuestionId | None = None

    def __post_init__(self) -> None:
        non_blank("回答", self.text)
        if (self.origin is DecisionOrigin.USER) != (self.question is not None):
            raise InvalidValue("ユーザーの回答だけが QuestionId を持つ")

    @property
    def is_human(self) -> bool:
        """ラン統括が答えるときの根拠にしてよいのはこれだけ。"""
        return self.origin is DecisionOrigin.USER
