"""モデルのクラスと、クラスごとのモデルと effort。"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum

from .base import InvalidValue, Text, non_blank


class ModelClass(Enum):
    """ステージと統括の役割の区分。系統の違うモデルを当てたい役割（レビュー）を、ほかと分けて替えられる。"""

    #: 統括・計画・設計の書き直し・ジャッジ。判断を出し、後ろで誰も確かめない
    LEAD = "lead"
    #: 設計とコードのレビュー
    REVIEW = "review"
    #: テスト・実装・修正・衝突の解消
    IMPLEMENT = "implement"
    #: PR の題と本文
    WRITE = "write"


class Effort(Enum):
    """エージェントの effort が受ける値。`minimal` は Codex だけが受ける。"""

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class AgentKind(Enum):
    """ステージと統括を走らせるエージェント。"""

    CLAUDE = "claude"
    CODEX = "codex"
    COPILOT = "copilot"

    @property
    def efforts(self) -> frozenset[Effort]:
        return _EFFORTS[self]


_EFFORTS = {
    AgentKind.CLAUDE: frozenset({Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX}),
    AgentKind.CODEX: frozenset(
        {Effort.MINIMAL, Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH}
    ),
    AgentKind.COPILOT: frozenset(Effort),
}


class ModelName(Text):
    """エージェントの `--model` に渡す名前。claude の別名（opus）・正式な ID（claude-opus-5-5）、codex のモデル名（gpt-5.5）を通す。"""

    def _check(self) -> None:
        non_blank("モデルの名前", self.value)


@dataclass(frozen=True)
class ModelChoice:
    model: ModelName
    effort: Effort
    agent: AgentKind = AgentKind.CLAUDE

    def __post_init__(self) -> None:
        if self.effort not in self.agent.efforts:
            usable = ", ".join(e.value for e in Effort if e in self.agent.efforts)
            raise InvalidValue(
                f"{self.agent.value} は effort {self.effort.value} を受けない（使えるのは {usable}）"
            )
        if self.agent is AgentKind.COPILOT and self.model.value.strip().lower() == "auto":
            raise InvalidValue("copilot には具体的なモデル名を書く。auto は使えない")


@dataclass(frozen=True)
class ModelClasses:
    """4 つのクラスそれぞれのモデルと effort。欄の名前は `ModelClass` の値に揃える（JSON の鍵になる）。"""

    lead: ModelChoice
    review: ModelChoice
    implement: ModelChoice
    write: ModelChoice

    @classmethod
    def default(cls) -> ModelClasses:
        opus, sonnet = ModelName("opus"), ModelName("sonnet")
        return cls(
            lead=ModelChoice(opus, Effort.HIGH),
            review=ModelChoice(opus, Effort.MEDIUM),
            implement=ModelChoice(sonnet, Effort.MEDIUM),
            write=ModelChoice(sonnet, Effort.MEDIUM),
        )

    def of(self, cls: ModelClass) -> ModelChoice:
        return getattr(self, cls.value)

    def with_choice(
        self,
        cls: ModelClass,
        *,
        model: ModelName | None = None,
        effort: Effort | None = None,
        agent: AgentKind | None = None,
    ) -> ModelClasses:
        now = self.of(cls)
        chosen = ModelChoice(model or now.model, effort or now.effort, agent or now.agent)
        return dataclasses.replace(self, **{cls.value: chosen})
