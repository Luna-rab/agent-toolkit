"""ReviewLedger が出すイベント（ストリーム `review/<TaskId>`・`review/design`）。"""

from __future__ import annotations

from dataclasses import dataclass

from ..value_objects.design_judgement import DesignJudgement
from ..value_objects.design_version import DesignVersion
from ..value_objects.execution_id import ExecutionId
from ..value_objects.finding_id import FindingId
from ..value_objects.finding_origin import FindingOrigin
from ..value_objects.finding_summary import FindingSummary
from ..value_objects.finding_target import FindingTarget
from ..value_objects.location import Location
from ..value_objects.rating import Rating
from ..value_objects.stall_cause import StallCause
from ..value_objects.task_id import TaskId
from .base import Event


@dataclass(frozen=True)
class FindingRaised(Event):
    finding: FindingId
    rating: Rating
    body: str
    location: Location | None = None
    carried_from: FindingOrigin | None = None
    #: 立てたステージの実行
    source: ExecutionId | None = None
    #: 設計の台帳の指摘だけが持つ。立てたときにレビューした設計の版
    design: DesignVersion | None = None
    target: FindingTarget = FindingTarget.IMPL


@dataclass(frozen=True)
class FindingRetargeted(Event):
    """Judge が、open に残す指摘の宛先を書き換えた。"""

    finding: FindingId
    target: FindingTarget
    execution: ExecutionId


@dataclass(frozen=True)
class FindingCommented(Event):
    finding: FindingId
    body: str
    author: ExecutionId | None = None


@dataclass(frozen=True)
class FindingClosed(Event):
    finding: FindingId
    comment: str
    execution: ExecutionId


@dataclass(frozen=True)
class FindingRejected(Event):
    finding: FindingId
    comment: str
    execution: ExecutionId


@dataclass(frozen=True)
class FindingReopened(Event):
    finding: FindingId
    comment: str
    #: 開き直した判定の実行（レビューの指摘は Judge・DesignJudge、Gate の項目の指摘は Gate）
    execution: ExecutionId | None = None


@dataclass(frozen=True)
class FixCounted(Event):
    """修正を 1 回受けた。そのとき open だった指摘の、修正を受けた回数が 1 増える。"""

    execution: ExecutionId
    findings: tuple[FindingId, ...]


@dataclass(frozen=True)
class FindingStalled(Event):
    """判定の後も、修正を STALL_AFTER_FIXES 回以上受けたまま open に残った。

    判定を締めたとき（RecordJudgement・RecordGateResult）に、条件を満たす指摘ごとに 1 つ出る。
    **ただの記録で、これを受けてエスカレーションを出さない。** 停滞のエスカレーションは、同じ
    コマンドの最後に出る FindingsEvaluated の `stalled` を受けて 1 回だけ上げる。
    台帳は `fixes` を覚え、そこから STALL_AFTER_FIXES 回の修正を受けるまで、同じ指摘を停滞に
    しない（回答の後にすぐまた上げない）。
    """

    finding: FindingId
    fixes: int


@dataclass(frozen=True)
class FindingsEvaluated(Event):
    """判定を締めた。その判定する者（Judge・DesignJudge・Gate）が判定する指摘のうち、open のものと、
    停滞したものを持つ。

    ポリシーは台帳を読めないので、ReviewLoop の次の手（抜ける・Fix・stall で上げる）、DesignLoop の
    次の手（確定する・Revise・前の版に戻った・曖昧）、Gate が落ちた後の手（Fix・stall で上げる）を、
    このイベントだけで決められるようにする。停滞が無くても必ず出る。設計の台帳では、今の提案に付いた
    指摘だけを数える。ジャッジの分類（停滞の原因・設計の分類）は、判定と一緒に届いたものを写す。
    """

    execution: ExecutionId
    open_findings: tuple[FindingSummary, ...]
    stalled: tuple[FindingId, ...] = ()
    #: Judge が停滞に付けた分類（ConcludeReviewRound の cause）
    stall_cause: StallCause | None = None
    #: 設計の台帳: DesignJudge が判定した提案の版（SettleDesign の design）
    design: DesignVersion | None = None
    #: 設計の台帳: DesignJudge の設計の分類（MarkReverted・MarkAmbiguous）
    design_cause: DesignJudgement | None = None


@dataclass(frozen=True)
class CommentRefused(Event):
    """ステージの結果のコメントが、台帳に無い指摘を指していた。状態は変えない（記録だけ）。

    コメントは受け渡しを待たないので（結果を待つ Task がいない）、拒否にせず、受けなかったことを残す。
    """

    finding: FindingId
    reason: str
    author: ExecutionId | None = None


@dataclass(frozen=True)
class CarryRefused(Event):
    """再計画の提案が移すとした指摘を、移せなかった（無い・open でない・もう移した・移す先が違う）。

    提案を反映した後に分かる中身の問題なので、拒否にせず、移さなかったことを残す。状態は変えない。
    """

    finding: FindingId
    to_task: TaskId
    reason: str


@dataclass(frozen=True)
class ResultReceived(Event):
    """ステージの結果を受けた（Design・ReviewLedger・Stack が出す）。ポリシーが ConfirmHandoff を出す。"""

    #: 結果を返したステージの実行
    source: ExecutionId


@dataclass(frozen=True)
class ResultRefused(Event):
    """ステージの結果を受けられなかった（Design・ReviewLedger・Stack が出す）。中身の問題（使った版・
    確定していない提案がある・無い指摘を判定した など）は、拒否ではなくこれで出す。ポリシーが
    ConfirmHandoff（refused）を出し、Task が result-refused で上げる。状態は変えない。"""

    source: ExecutionId
    reason: str


@dataclass(frozen=True)
class ProposalTracked(Event):
    """設計の台帳が、数える指摘を `design` の版から後に絞った（新しい提案が入った）。"""

    design: DesignVersion


@dataclass(frozen=True)
class FindingCarried(Event):
    """移す元を carried にした。移した先に立てるポリシーが要るので、指摘の中身を載せる。"""

    finding: FindingId
    to_task: TaskId
    rating: Rating
    body: str
    location: Location | None = None
    target: FindingTarget = FindingTarget.IMPL
