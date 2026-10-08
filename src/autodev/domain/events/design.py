"""Design 集約が出すイベント（ストリーム `design`）。"""

from __future__ import annotations

from dataclasses import dataclass

from ..value_objects.artifact_ref import ArtifactRef
from ..value_objects.design_version import DesignVersion
from ..value_objects.execution_id import ExecutionId
from ..value_objects.finding_summary import FindingSummary
from ..value_objects.proposal import Proposal
from .base import Event


@dataclass(frozen=True)
class DesignProposed(Event):
    proposal: Proposal
    #: 提案を出した計画タスクが持っていた、ラン共通の成果物（brief・codemap）の在りか
    artifacts: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True)
class DesignRevised(Event):
    """直した提案の新しい版が入った（Revise の結果は ProposeDesign で入れる）。"""

    proposal: Proposal
    artifacts: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True)
class DesignRevisionStarted(Event):
    """Revise を起動する前に、ラウンドを 1 つ使った。"""

    round: int
    #: 回答を待っていた所から続けるなら、その回答。Revise の入力に足す
    answer: str | None = None
    #: 直すと決めた判定（DesignJudge の実行）。Task が今のラウンドの判定かを確かめる
    execution: ExecutionId | None = None


@dataclass(frozen=True)
class DesignSettled(Event):
    """提案を確定した。

    `appendix` は確定した時点で open だった must-fix 以外の指摘で、反応が設計ファイル
    （`proposal.design` の版）の末尾に書き足し、ポリシーが `execution` の判定として rejected にする。
    """

    proposal: Proposal
    #: 確定を決めた判定（DesignJudge の実行）
    execution: ExecutionId
    appendix: tuple[FindingSummary, ...] = ()
    #: ラン共通の成果物（brief・codemap と、確定した design の版）。RecordSettledPlan の artifacts
    artifacts: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True)
class DesignReverted(Event):
    """DesignJudge が、今の提案は前の版の形に戻ったと判定した。回答が来るまで直さない。"""

    to_version: DesignVersion
    #: そう判定した DesignJudge の実行
    execution: ExecutionId | None = None


@dataclass(frozen=True)
class DesignAmbiguous(Event):
    """DesignJudge が、設計の受入条件が曖昧だと判定した。回答が来るまで直さない。"""

    execution: ExecutionId


@dataclass(frozen=True)
class DesignRoundsExhausted(Event):
    rounds: int
    #: must-fix を残した判定（DesignJudge の実行）
    execution: ExecutionId | None = None


@dataclass(frozen=True)
class DesignRoundsReset(Event):
    answer: str


@dataclass(frozen=True)
class DesignProposalAbandoned(Event):
    """確定していない提案を捨てた（再計画を頼まれた）。"""

    design: DesignVersion | None
    reason: str
