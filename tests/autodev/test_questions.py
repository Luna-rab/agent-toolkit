"""Questions 集約（`domain/aggregates/questions.py`）。"""

from __future__ import annotations

import pytest
from autodev_harness import CLI, POLICY, RUN_SUPERVISOR, Loop, new_id

from autodev.domain.aggregates.base import Rejected
from autodev.domain.aggregates.questions import Questions, QuestionStatus
from autodev.domain.commands.questions import AnswerQuestion, PostQuestion, WithdrawQuestions
from autodev.domain.events.questions import QuestionAnswered, QuestionPosted, QuestionWithdrawn
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.stream_id import StreamId

Q = QuestionId("q-scope")
ESCALATION = EventId("run#7")


def post(question: QuestionId = Q, body: str = "どちらにする？") -> PostQuestion:
    return PostQuestion(
        command_id=new_id(),
        issuer=RUN_SUPERVISOR,
        question=question,
        body=body,
        escalation=ESCALATION,
    )


def answer(question: QuestionId = Q, text: str = "A にする") -> AnswerQuestion:
    return AnswerQuestion(command_id=new_id(), issuer=CLI, question=question, answer=text)


@pytest.fixture
def loop() -> Loop[Questions]:
    return Loop(Questions(StreamId.questions()))


def test_質問を出して答えると回答は経路を持って下りる(loop: Loop[Questions]):
    assert loop(post()) == [QuestionPosted(Q, "どちらにする？", ESCALATION)]
    assert loop(answer()) == [QuestionAnswered(Q, "A にする", ESCALATION)]
    assert loop.aggregate.questions[Q].status is QuestionStatus.ANSWERED
    assert loop.replayed().questions == loop.aggregate.questions


def test_QuestionIdは1回だけ使う(loop: Loop[Questions]):
    loop(post())
    with pytest.raises(Rejected, match="もう使った QuestionId"):
        loop(post(body="別の質問"))
    # 答えた後も同じ id は使えない
    loop(answer())
    with pytest.raises(Rejected, match="もう使った QuestionId"):
        loop(post())
    loop(post(QuestionId("q-other")))


def test_1つのエスカレーションに回答を待つ質問は1つだけ(loop: Loop[Questions]):
    loop(post())
    with pytest.raises(Rejected, match="回答を待っている質問 q-scope がある"):
        loop(post(QuestionId("q-again")))
    # 経路の無い質問は重ねてよい。答えた後は、同じエスカレーションでまた聞ける
    loop(
        PostQuestion(
            command_id=new_id(), issuer=RUN_SUPERVISOR, question=QuestionId("q-x"), body="?"
        )
    )
    loop(answer())
    loop(post(QuestionId("q-again")))


def test_回答できるのはopenの質問だけ(loop: Loop[Questions]):
    with pytest.raises(Rejected, match="という質問は無い"):
        loop(answer())
    loop(post())
    loop(answer())
    with pytest.raises(Rejected, match="回答できるのは open の質問だけ"):
        loop(answer(text="やっぱり B"))


def withdraw(escalation: EventId = ESCALATION) -> WithdrawQuestions:
    return WithdrawQuestions(
        command_id=new_id(), issuer=POLICY, escalation=escalation, reason="タスクを止めた"
    )


def test_経路のエスカレーションが閉じたら回答を待つ質問を取り下げる(loop: Loop[Questions]):
    loop(post())
    # ほかのエスカレーションが閉じても、取り下げない
    assert loop(withdraw(EventId("run#8"))) == []
    assert loop(withdraw()) == [QuestionWithdrawn(Q, ESCALATION, "タスクを止めた")]
    assert loop.aggregate.questions[Q].status is QuestionStatus.WITHDRAWN
    assert not loop.aggregate.awaiting_answer
    assert loop.replayed().questions == loop.aggregate.questions
    # 拒む理由に、取り下げた理由を添える（`autodev answer` が /autodev にそのまま返す。S6）
    with pytest.raises(
        Rejected, match="取り下げた質問で、回答は使われない。取り下げた理由: タスクを止めた"
    ):
        loop(answer())
    # 取り下げた後は、同じエスカレーションでまた聞ける。答えた質問は取り下げない
    loop(post(QuestionId("q-again")))
    loop(answer(QuestionId("q-again")))
    assert loop(withdraw()) == []


def test_空の質問と空の回答を拒む(loop: Loop[Questions]):
    with pytest.raises(Rejected, match="質問の本文が空"):
        loop(post(body="  "))
    loop(post())
    with pytest.raises(Rejected, match="回答が空"):
        loop(answer(text=""))


def test_質問を出せるのはラン統括だけで答えられるのはCLIだけ(loop: Loop[Questions]):
    with pytest.raises(Rejected, match="cli は出せない"):
        loop(PostQuestion(command_id=new_id(), issuer=CLI, question=Q, body="x"))
    loop(post())
    with pytest.raises(Rejected, match="run-supervisor は出せない"):
        loop(AnswerQuestion(command_id=new_id(), issuer=RUN_SUPERVISOR, question=Q, answer="x"))
