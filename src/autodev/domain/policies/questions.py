"""Questions のイベントを受けるポリシー。"""

from __future__ import annotations

from ..commands.base import Command
from ..commands.run import RecordAnswer
from ..events.questions import QuestionAnswered
from ..value_objects.event_id import EventId
from .base import Stamp


def record_answer(e: QuestionAnswered, src: EventId, stamp: Stamp) -> list[Command]:
    return [RecordAnswer(**stamp(), question=e.question, answer=e.answer, escalation=e.escalation)]
