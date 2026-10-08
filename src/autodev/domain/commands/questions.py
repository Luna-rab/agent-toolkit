"""Questions 集約が受けるコマンド。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..value_objects.event_id import EventId
from ..value_objects.issuer_kind import IssuerKind
from ..value_objects.question_id import QuestionId
from ..value_objects.stream_id import StreamId
from .base import Command

_K = IssuerKind


@dataclass(frozen=True, kw_only=True)
class QuestionsCommand(Command):
    AGGREGATE: ClassVar[str] = "Questions"

    @property
    def target(self) -> StreamId:
        return StreamId.questions()


@dataclass(frozen=True, kw_only=True)
class PostQuestion(QuestionsCommand):
    #: ポリシーが出すのは、ラン統括が応じなかったとき（ラン統括の 1 段上は /autodev）
    ISSUERS = frozenset({_K.RUN_SUPERVISOR, _K.POLICY})
    question: QuestionId
    body: str
    #: 経路。この質問を出すきっかけになった Run の側のエスカレーション
    escalation: EventId | None = None


@dataclass(frozen=True, kw_only=True)
class AnswerQuestion(QuestionsCommand):
    ISSUERS = frozenset({_K.CLI})
    question: QuestionId
    answer: str


@dataclass(frozen=True, kw_only=True)
class WithdrawQuestions(QuestionsCommand):
    """Run の側のエスカレーションが閉じたので、それを経路に持つ回答待ちの質問を取り下げる。"""

    ISSUERS = frozenset({_K.POLICY})
    escalation: EventId
    #: エスカレーションを閉じた理由（EscalationClosed.reason）
    reason: str
