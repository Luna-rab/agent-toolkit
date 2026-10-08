"""フロー（タスク統括が組むステージの並び）と、フローのどこまで進んだか（`Cursor`）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..stages.kinds import StepArgument
from ..value_objects.artifact_kind import ArtifactKind
from ..value_objects.base import InvalidValue
from ..value_objects.git_job import GitJob
from ..value_objects.stage_kind import StageKind
from ..value_objects.task_kind import TaskKind


@dataclass(frozen=True)
class Reviewers:
    """ReviewLoop の引数。1 ラウンド目に並列で走らせるレビューと、2 ラウンド目からのレビュー。

    顔ぶれが規則に合うか（1 つ以上・レビューのステージだけ・重複なし）は、拒んだ理由を統括に返せる
    ように、作るときではなく FlowValidator が確かめる。
    """

    first: tuple[StageKind, ...]
    #: 省くと first と同じ
    later: tuple[StageKind, ...] | None = None

    def for_round(self, round: int) -> tuple[StageKind, ...]:
        if round <= 1 or self.later is None:
            return self.first
        return self.later


@dataclass(frozen=True)
class FlowStep:
    stage: StageKind
    #: 書けるのは StageSpec.arguments に REVIEWERS を持つステージ（ReviewLoop）だけ
    reviewers: Reviewers | None = None
    #: タスクの間続くセッションのステージ（Impl）を、新しいセッションでやり直す
    #: （堂々巡りのときなど。続けたセッションで落ち続けることがある）
    fresh_session: bool = False
    #: 統括からステージへの言葉（確かめてほしいこと・直し方の方針など）。実行器がプロンプトに
    #: 埋めるだけで、ドメインは中身を解釈しない
    instruction: str | None = None

    @property
    def arguments(self) -> frozenset[StepArgument]:
        """この段に書いてある引数。"""
        return frozenset({StepArgument.REVIEWERS} if self.reviewers is not None else ())


@dataclass(frozen=True)
class Flow:
    """検査を通ったフロー。版の番号は、Task が受け入れるたびに 1 つ上げる。

    git 管理タスクのフローは、Stack から取り出した仕事 1 つ（`job`）を処理する。仕事の相手（タスク・
    ブランチ・base・閉じる所）は、実行器もポリシーもここから読む。
    """

    steps: tuple[FlowStep, ...]
    version: int
    job: GitJob | None = None

    def __post_init__(self) -> None:
        if not self.steps:
            raise InvalidValue("フローが空")
        if self.version < 1:
            raise InvalidValue(f"フローの版は 1 以上: {self.version}")

    def at(self, cursor: Cursor) -> FlowStep | None:
        """cursor が指す段。フローを終えていれば None。"""
        if cursor.step < len(self.steps):
            return self.steps[cursor.step]
        return None

    def last_index_of(self, stage: StageKind, before: int) -> int | None:
        """`before` より前で、最後に `stage` を置いた段の添字（Gate の不合格で戻る ReviewLoop を探す）。"""
        for index in range(min(before, len(self.steps)) - 1, -1, -1):
            if self.steps[index].stage is stage:
                return index
        return None


@dataclass(frozen=True)
class Cursor:
    """フローのどこまで進んだか。合成ステージの中なら、中のステージとラウンドも持つ。

    合成ステージの中の次のステージを決めるのは Task で、Cursor は位置を持つだけである。
    """

    #: Flow.steps の添字。len(steps) ならフローを終えた
    step: int = 0
    #: 合成ステージの中のステージ。合成ステージの外なら None
    inner: StageKind | None = None
    #: 合成ステージの中のラウンド（1 から）。外なら 0
    round: int = 0

    def __post_init__(self) -> None:
        if self.step < 0:
            raise InvalidValue(f"cursor の段は 0 以上: {self.step}")
        if (self.inner is None) != (self.round == 0):
            raise InvalidValue("合成ステージの中にいるときだけ、ラウンドを 1 から数える")

    def next_step(self) -> Cursor:
        return Cursor(self.step + 1)

    def enter(self, inner: StageKind, round: int) -> Cursor:
        return Cursor(self.step, inner, round)

    def is_done(self, flow: Flow) -> bool:
        return self.step >= len(flow.steps)


#: タスクの種類ごとに、フローの終わりまでに作っていなければならない成果物。実装タスクだけが
#: 持つ（計画タスクと git 管理タスクはランが終わるまで続く）
REQUIRED_AT_END: Mapping[TaskKind, frozenset[ArtifactKind]] = {
    TaskKind.IMPLEMENTATION: frozenset({ArtifactKind.GATED, ArtifactKind.PR_BODY}),
}
