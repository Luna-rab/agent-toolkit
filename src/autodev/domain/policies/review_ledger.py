"""ReviewLedger のイベントを受けるポリシー。"""

from __future__ import annotations

from ..commands.base import Command
from ..commands.design import MarkAmbiguous, MarkReverted, ReviseDesign, SettleDesign
from ..commands.review_ledger import RaiseFinding
from ..commands.task import ConcludeGateRound, ConcludeReviewRound, ConfirmHandoff
from ..events.review_ledger import FindingCarried, FindingsEvaluated, ResultReceived, ResultRefused
from ..value_objects.design_cause import DesignCause
from ..value_objects.event_id import EventId
from ..value_objects.finding_origin import FindingOrigin
from ..value_objects.finding_route import FindingRoute
from ..value_objects.rating import Rating
from ..value_objects.stage_kind import StageKind
from ..value_objects.stream_id import StreamId
from .base import Stamp

_S = StageKind


def conclude_round(e: FindingsEvaluated, src: EventId, stamp: Stamp) -> list[Command]:
    """判定を締めた台帳の答えで、ラウンドを締める（Judge・Gate）か、設計の行き先を決める（DesignJudge）。"""
    judge = e.execution
    unresolved = tuple(f.finding for f in e.open_findings)
    routes = tuple(FindingRoute(f.finding, f.target) for f in e.open_findings)
    if judge.stage is _S.JUDGE:
        return [
            ConcludeReviewRound(
                **stamp(),
                task=judge.task,
                judge=judge,
                unresolved=unresolved,
                stalled=e.stalled,
                cause=e.stall_cause,
                routes=routes,
            )
        ]
    if judge.stage is _S.GATE:
        return [
            ConcludeGateRound(
                **stamp(),
                task=judge.task,
                gate=judge,
                unresolved=unresolved,
                stalled=e.stalled,
                routes=routes,
            )
        ]
    # 設計の台帳: 前の版に戻った・曖昧なら回答を待つ。must-fix が残れば直す。無ければ確定する
    cause = e.design_cause
    if cause is not None and cause.cause is DesignCause.REVERTED:
        assert cause.reverted_to is not None, "前の版に戻ったなら、戻った版を持つ"
        return [MarkReverted(**stamp(), to_version=cause.reverted_to, execution=judge)]
    if cause is not None and cause.cause is DesignCause.AMBIGUOUS:
        return [MarkAmbiguous(**stamp(), execution=judge)]
    if any(f.rating is Rating.MUST_FIX for f in e.open_findings):
        return [ReviseDesign(**stamp(), execution=judge)]
    assert e.design is not None, "DesignJudge の判定は、見た版を持つ"
    return [
        SettleDesign(**stamp(), execution=judge, design=e.design, open_findings=e.open_findings)
    ]


def confirm_received(e: ResultReceived, src: EventId, stamp: Stamp) -> list[Command]:
    return [ConfirmHandoff(**stamp(), task=e.source.task, execution=e.source)]


def confirm_refused(e: ResultRefused, src: EventId, stamp: Stamp) -> list[Command]:
    return [ConfirmHandoff(**stamp(), task=e.source.task, execution=e.source, refused=e.reason)]


def raise_carried(e: FindingCarried, src: EventId, stamp: Stamp) -> list[Command]:
    return [
        RaiseFinding(
            **stamp(),
            ledger=StreamId.review(e.to_task),
            rating=e.rating,
            body=e.body,
            location=e.location,
            carried_from=FindingOrigin(src.stream, e.finding),
            finding_target=e.target,
        )
    ]
