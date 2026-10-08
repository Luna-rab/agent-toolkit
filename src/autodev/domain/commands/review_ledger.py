"""ReviewLedger（指摘の台帳）が受けるコマンド。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..value_objects.base import InvalidValue
from ..value_objects.design_judgement import DesignJudgement
from ..value_objects.design_version import DesignVersion
from ..value_objects.execution_id import ExecutionId
from ..value_objects.finding_id import FindingId
from ..value_objects.finding_origin import FindingOrigin
from ..value_objects.finding_status import FindingStatus
from ..value_objects.finding_target import FindingTarget
from ..value_objects.finding_verdict import FindingVerdict
from ..value_objects.gate_item_result import GateItemResult
from ..value_objects.issuer_kind import IssuerKind
from ..value_objects.location import Location
from ..value_objects.rating import Rating
from ..value_objects.reported_finding import ReportedFinding
from ..value_objects.stall_cause import StallCause
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId
from .base import Command

_K = IssuerKind


@dataclass(frozen=True, kw_only=True)
class ReviewCommand(Command):
    AGGREGATE: ClassVar[str] = "ReviewLedger"
    #: `review/<TaskId>` か `review/design`
    ledger: StreamId

    def __post_init__(self) -> None:
        if not self.ledger.is_review:
            raise InvalidValue(f"指摘の台帳のストリームではない: {self.ledger}")

    @property
    def target(self) -> StreamId:
        return self.ledger


@dataclass(frozen=True, kw_only=True)
class RecordFindings(ReviewCommand):
    """見る役（Review・AdversarialReview・DesignReview）と Expect が挙げた指摘を、まとめて立てる。

    受けたら ResultReceived、中身の問題（本文が空・見た版が今の提案より古い）なら ResultRefused を出す。
    1 件ずつの RaiseFinding にしないのは、受けた／受けられないをステージの結果 1 つに 1 回で返すため。
    """

    ISSUERS = frozenset({_K.POLICY})
    #: 指摘を挙げたステージの実行
    source: ExecutionId
    findings: tuple[ReportedFinding, ...]
    #: 設計の台帳に立てるときだけ要る。DesignReview が見た提案の版（StageCompleted.reviewed）
    design: DesignVersion | None = None


@dataclass(frozen=True, kw_only=True)
class RaiseFinding(ReviewCommand):
    """指摘を 1 件立てる（移した指摘を、移した先に立てる。FindingCarried を受けて）。

    ステージの結果の指摘は RecordFindings、Gate の項目の指摘（G-）は RecordGateResult が立てる。
    """

    ISSUERS = frozenset({_K.POLICY})
    rating: Rating
    body: str
    location: Location | None = None
    carried_from: FindingOrigin | None = None
    source: ExecutionId | None = None
    #: 設計の台帳に立てるときだけ要る。レビューした設計の版
    design: DesignVersion | None = None
    #: `target` は `ReviewCommand.target`（届け先の台帳）なので、指摘の宛先は別の名前にする
    finding_target: FindingTarget = FindingTarget.IMPL


@dataclass(frozen=True, kw_only=True)
class CommentFinding(ReviewCommand):
    ISSUERS = frozenset({_K.POLICY})
    finding: FindingId
    body: str
    author: ExecutionId | None = None


@dataclass(frozen=True, kw_only=True)
class JudgeFinding(ReviewCommand):
    """JudgeCapability: `execution` がその指摘を判定する者の実行であるものだけを、台帳が受ける。"""

    ISSUERS = frozenset({_K.POLICY})
    finding: FindingId
    to: FindingStatus
    comment: str
    execution: ExecutionId


@dataclass(frozen=True, kw_only=True)
class CountFix(ReviewCommand):
    ISSUERS = frozenset({_K.POLICY})
    #: 完了した Fix の実行
    execution: ExecutionId


@dataclass(frozen=True, kw_only=True)
class RecordJudgement(ReviewCommand):
    """Judge・DesignJudge の判定を当て、判定を締める。

    判定をすべて当ててから、修正を STALL_AFTER_FIXES 回以上受けたまま open に残った指摘ごとに
    FindingStalled を出し、最後に FindingsEvaluated（ジャッジの分類を写す）を出す。中身の問題（無い
    指摘・許されない遷移・コメントが空・見た版が古い）があれば、どれも当てずに ResultRefused を出す。
    """

    ISSUERS = frozenset({_K.POLICY})
    #: 判定を返した Judge・DesignJudge の実行
    execution: ExecutionId
    verdicts: tuple[FindingVerdict, ...] = ()
    #: Judge が停滞に付けた分類（タスクの台帳だけ）
    stall_cause: StallCause | None = None
    #: 設計の台帳だけ: DesignJudge が見た提案の版（StageCompleted.reviewed）と、設計の分類
    design: DesignVersion | None = None
    design_cause: DesignJudgement | None = None


@dataclass(frozen=True, kw_only=True)
class RecordGateResult(ReviewCommand):
    """Gate の結果で、Gate の項目の指摘（G-）を判定する（Gate の StageCompleted・GateFailed を受けて）。

    落ちた項目の指摘は開き（閉じていれば開き直し）、通った項目の open の指摘は閉じる。判定を
    締めるので、RecordJudgement と同じく FindingStalled と FindingsEvaluated も出す。
    """

    ISSUERS = frozenset({_K.POLICY})
    #: Gate の実行
    execution: ExecutionId
    #: 落ちた項目（GateFailed.failed）。Gate が通ったなら空
    failed: tuple[GateItemResult, ...] = ()


@dataclass(frozen=True, kw_only=True)
class TrackProposal(ReviewCommand):
    """設計の台帳が数える指摘を、新しい提案の版から後に絞る（DesignProposed を受けて）。"""

    ISSUERS = frozenset({_K.POLICY})
    design: DesignVersion


@dataclass(frozen=True, kw_only=True)
class CarryFinding(ReviewCommand):
    """宛先は移す元の台帳。"""

    ISSUERS = frozenset({_K.POLICY})
    finding: FindingId
    to_task: TaskId
