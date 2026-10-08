"""Questions 集約が出すイベント（ストリーム `questions`）。"""

from __future__ import annotations

from dataclasses import dataclass

from ..value_objects.event_id import EventId
from ..value_objects.question_id import QuestionId
from .base import Event


@dataclass(frozen=True)
class QuestionPosted(Event):
    question: QuestionId
    body: str
    #: 経路。この質問を出すきっかけになった Run の側のエスカレーション
    escalation: EventId | None = None


@dataclass(frozen=True)
class QuestionAnswered(Event):
    question: QuestionId
    answer: str
    escalation: EventId | None = None


@dataclass(frozen=True)
class QuestionWithdrawn(Event):
    """回答を待っていた質問を、経路のエスカレーションが回答以外で閉じたので取り下げた。"""

    question: QuestionId
    escalation: EventId
    #: エスカレーションを閉じた理由
    reason: str
