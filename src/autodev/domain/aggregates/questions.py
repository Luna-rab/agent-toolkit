"""Questions 集約。ラン統括から `/autodev` へ上げた質問と、その回答。

質問は、それを出すきっかけになった Run の側のエスカレーション（経路）を持ち、回答はその経路を
逆にたどって下りる。回答をどこで 1 回だけ使えるかは、回答を使う判断（answer・replan）を受ける
Run が確かめる（`run.py` の `_usable_answer`）。ここが確かめるのは、QuestionId を 1 回だけ使う
（同じ id で 2 つ目の質問を出さない）ことと、1 つのエスカレーションに open の質問は 1 つだけで
あることと、回答できるのは open の質問だけであること。経路のエスカレーションが回答以外で閉じたら、
質問は取り下げる（`WithdrawQuestions`。回答を待ったまま終了コード 4 で止まらない）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from ..commands.questions import AnswerQuestion, PostQuestion, WithdrawQuestions
from ..events.base import Event
from ..events.questions import QuestionAnswered, QuestionPosted, QuestionWithdrawn
from ..value_objects.event_id import EventId
from ..value_objects.question_id import QuestionId
from ..value_objects.stream_id import StreamId
from .base import Aggregate, Rejected, applies, handles


class QuestionStatus(Enum):
    OPEN = "open"
    ANSWERED = "answered"
    #: 経路のエスカレーションが回答以外で閉じた（タスクを止めた・再計画した など）。回答を待たない
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class Question:
    id: QuestionId
    body: str
    #: 経路。この質問を出すきっかけになった Run の側のエスカレーション
    escalation: EventId | None
    status: QuestionStatus = QuestionStatus.OPEN
    answer: str | None = None
    #: 取り下げた理由（経路のエスカレーションを閉じた理由）。回答を拒むときに添える
    withdrawn_reason: str | None = None


class Questions(Aggregate):
    NAME = "Questions"

    def __init__(self, stream: StreamId) -> None:
        super().__init__(stream)
        self.questions: dict[QuestionId, Question] = {}

    @property
    def awaiting_answer(self) -> bool:
        """ユーザーの回答を待っている質問がある。

        進められるものが無くなった driver が、終了コード 4（回答待ち）で終えてよいかの問い。これが
        偽なのに進められないなら、回答を待っているのではない。
        """
        return bool(self.open_questions)

    @property
    def open_questions(self) -> tuple[Question, ...]:
        """回答を待っている質問（出した順）。"""
        return tuple(q for q in self.questions.values() if q.status is QuestionStatus.OPEN)

    @handles(PostQuestion)
    def _post(self, command: PostQuestion) -> list[Event]:
        if command.question in self.questions:
            # 同じ id を 2 つの質問に使うと、回答がどちらの質問へのものか分からなくなる
            raise Rejected(
                f"{command.question} はもう使った QuestionId。質問ごとに新しい id にする"
            )
        if not command.body.strip():
            raise Rejected("質問の本文が空")
        if command.escalation is not None and (
            waiting := next(
                (
                    q.id
                    for q in self.questions.values()
                    if q.escalation == command.escalation and q.status is QuestionStatus.OPEN
                ),
                None,
            )
        ):
            # 1 つのエスカレーションに、回答を待つ質問は 1 つだけ。ユーザーが受ける上げ（統括が応じ
            # なかった）はポリシーがすぐ質問にするので、ラン統括が同じ上げで質問を重ねる（その回答で
            # 自分の上げを閉じる）こともここで止まる
            raise Rejected(
                f"{command.escalation} には回答を待っている質問 {waiting} がある。回答が届いてから聞く"
            )
        return [QuestionPosted(command.question, command.body, command.escalation)]

    @handles(AnswerQuestion)
    def _answer(self, command: AnswerQuestion) -> list[Event]:
        question = self.questions.get(command.question)
        if question is None:
            raise Rejected(f"{command.question} という質問は無い")
        if question.status is QuestionStatus.WITHDRAWN:
            # `autodev answer` が要求を足す前にこの判断を借りて、理由ごと /autodev に返す（S6）
            raise Rejected(
                f"{command.question} は取り下げた質問で、回答は使われない。"
                f"取り下げた理由: {question.withdrawn_reason}"
            )
        if question.status is not QuestionStatus.OPEN:
            raise Rejected(
                f"{command.question} は {question.status.value} で、回答できるのは open の質問だけ"
            )
        if not command.answer.strip():
            raise Rejected("回答が空")
        return [QuestionAnswered(command.question, command.answer, question.escalation)]

    @handles(WithdrawQuestions)
    def _withdraw(self, command: WithdrawQuestions) -> list[Event]:
        # ポリシーは開いた質問があるかを知らずに、閉じたエスカレーションごとに出す。無ければ何もしない
        return [
            QuestionWithdrawn(q.id, command.escalation, command.reason)
            for q in self.questions.values()
            if q.escalation == command.escalation and q.status is QuestionStatus.OPEN
        ]

    @applies(QuestionPosted)
    def _on_posted(self, event: QuestionPosted) -> None:
        self.questions[event.question] = Question(event.question, event.body, event.escalation)

    @applies(QuestionAnswered)
    def _on_answered(self, event: QuestionAnswered) -> None:
        self.questions[event.question] = replace(
            self.questions[event.question], status=QuestionStatus.ANSWERED, answer=event.answer
        )

    @applies(QuestionWithdrawn)
    def _on_withdrawn(self, event: QuestionWithdrawn) -> None:
        self.questions[event.question] = replace(
            self.questions[event.question],
            status=QuestionStatus.WITHDRAWN,
            withdrawn_reason=event.reason,
        )
