"""Design 集約が受けるコマンド。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..value_objects.artifact_ref import ArtifactRef
from ..value_objects.design_version import DesignVersion
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.execution_id import ExecutionId
from ..value_objects.finding_summary import FindingSummary
from ..value_objects.issuer_kind import IssuerKind
from ..value_objects.proposal import Proposal
from ..value_objects.stream_id import StreamId
from .base import Command

_K = IssuerKind


@dataclass(frozen=True, kw_only=True)
class DesignCommand(Command):
    AGGREGATE: ClassVar[str] = "Design"

    @property
    def target(self) -> StreamId:
        return StreamId.design()


@dataclass(frozen=True, kw_only=True)
class ProposeDesign(DesignCommand):
    """Plan・Replan・Revise の結果の提案を入れる。

    新しい提案（Plan・Replan）か、今の提案の新しい版（Revise）かは、`execution` のステージで決まる。
    受けたら ResultReceived、中身の問題（確定していない提案がすでにある・使った版の番号など）なら
    ResultRefused を出す。
    """

    ISSUERS = frozenset({_K.POLICY})
    proposal: Proposal
    #: 提案を返したステージの実行
    execution: ExecutionId
    #: ラン共通の成果物（brief・codemap）の在りか（StageCompleted.shared）。確定したら Run へ渡る
    artifacts: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ReviseDesign(DesignCommand):
    """Revise を起動する前に出す。"""

    ISSUERS = frozenset({_K.POLICY})
    #: must-fix を残した判定（設計の台帳の FindingsEvaluated の DesignJudge の実行）
    execution: ExecutionId


@dataclass(frozen=True, kw_only=True)
class ResumeDesign(DesignCommand):
    """回答を待っていた設計を、回答を持って Revise から続ける（ResetDesignRounds を一般化したもの）。

    `kind` は答えたエスカレーションの種類（design-rounds-exhausted・design-reverted・
    design-ambiguous）で、Design が待っている原因と合わなければ拒む。
    """

    ISSUERS = frozenset({_K.POLICY})
    kind: EscalationKind
    answer: str


@dataclass(frozen=True, kw_only=True)
class SettleDesign(DesignCommand):
    """設計の台帳の FindingsEvaluated を受けて出す。Design は台帳を読めないので、open の指摘を載せる。"""

    ISSUERS = frozenset({_K.POLICY})
    #: 判定を締めた DesignJudge の実行
    execution: ExecutionId
    #: 判定した設計の版
    design: DesignVersion
    #: そのときの設計の台帳の open の指摘
    open_findings: tuple[FindingSummary, ...] = ()


@dataclass(frozen=True, kw_only=True)
class MarkReverted(DesignCommand):
    ISSUERS = frozenset({_K.POLICY})
    to_version: DesignVersion
    #: そう判定した DesignJudge の実行
    execution: ExecutionId


@dataclass(frozen=True, kw_only=True)
class MarkAmbiguous(DesignCommand):
    """DesignJudge が設計の受入条件を曖昧と判定した（DesignCause の ambiguous）。"""

    ISSUERS = frozenset({_K.POLICY})
    execution: ExecutionId


@dataclass(frozen=True, kw_only=True)
class DiscardProposal(DesignCommand):
    """確定していない提案を捨てる（ReplanRequested を受けて）。"""

    ISSUERS = frozenset({_K.POLICY})
    reason: str
