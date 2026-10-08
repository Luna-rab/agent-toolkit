"""Design のイベントを受けるポリシー。"""

from __future__ import annotations

from ..commands.base import Command
from ..commands.review_ledger import JudgeFinding, TrackProposal
from ..commands.run import RecordSettledPlan
from ..commands.task import ConcludeDesignRound, Escalate
from ..events.design import (
    DesignAmbiguous,
    DesignProposed,
    DesignReverted,
    DesignRevisionStarted,
    DesignRoundsExhausted,
    DesignSettled,
)
from ..value_objects.design_cause import DesignCause
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.event_id import EventId
from ..value_objects.finding_status import FindingStatus
from ..value_objects.hint import Hint
from ..value_objects.pointers import Pointers
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId
from .base import Rule, Stamp

_E = EscalationKind
_PLANNING = TaskId.planning()


def track_proposal(e: DesignProposed, src: EventId, stamp: Stamp) -> list[Command]:
    return [TrackProposal(**stamp(), ledger=StreamId.design_review(), design=e.proposal.design)]


def record_settled(e: DesignSettled, src: EventId, stamp: Stamp) -> list[Command]:
    return [RecordSettledPlan(**stamp(), proposal=e.proposal, artifacts=e.artifacts)]


def conclude_settled(e: DesignSettled, src: EventId, stamp: Stamp) -> list[Command]:
    return [
        ConcludeDesignRound(**stamp(), task=_PLANNING, judge=e.execution, settled=e.proposal.design)
    ]


def reject_appendix(e: DesignSettled, src: EventId, stamp: Stamp) -> list[Command]:
    """must-fix 以外の指摘は設計ファイルの末尾に回し（反応）、確定を決めた判定で rejected にする。"""
    return [
        JudgeFinding(
            **stamp(),
            ledger=StreamId.design_review(),
            finding=f.finding,
            to=FindingStatus.REJECTED,
            comment="must-fix 以外なので、設計ファイルの末尾に書き足した",
            execution=e.execution,
        )
        for f in e.appendix
    ]


def conclude_revision(e: DesignRevisionStarted, src: EventId, stamp: Stamp) -> list[Command]:
    if e.execution is None:
        return []
    return [ConcludeDesignRound(**stamp(), task=_PLANNING, judge=e.execution)]


def escalate_design_wait(kind: EscalationKind) -> Rule:
    """設計が回答を待つ（前の版に戻った・曖昧・ラウンドを使い切った）ことを、計画タスクから上げる。"""
    cause = {_E.DESIGN_REVERTED: DesignCause.REVERTED, _E.DESIGN_AMBIGUOUS: DesignCause.AMBIGUOUS}

    def rule(
        e: DesignReverted | DesignAmbiguous | DesignRoundsExhausted, src: EventId, stamp: Stamp
    ) -> list[Command]:
        return [
            Escalate(
                **stamp(),
                task=_PLANNING,
                kind=kind,
                pointers=Pointers(),
                hint=Hint(design_cause=cause.get(kind)),
                origin=e.execution,
            )
        ]

    return rule
