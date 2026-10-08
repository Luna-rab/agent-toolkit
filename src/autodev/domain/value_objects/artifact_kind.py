from __future__ import annotations

from enum import Enum


class ArtifactKind(Enum):
    """ステージが作り、後のステージが要るもの。

    `proposal`（確定前の設計の提案）は、Plan・Replan の
    produces と DesignLoop の needs に「提案」として現れるので足した。FlowValidator が計画タスクの
    フローも同じ規則で照合できるようにするためである。
    """

    BRIEF = "brief"
    CODEMAP = "codemap"
    PROPOSAL = "proposal"
    DESIGN = "design"
    TESTS = "tests"
    #: 期待値が空のテストがある（TestGen が報告した）。Expect が期待値を書いたら外れる
    AWAITING_EXPECTATIONS = "awaiting-expectations"
    RED_TESTS = "red-tests"
    IMPL = "impl"
    REVIEWED = "reviewed"
    GATED = "gated"
    PR_BODY = "pr-body"
    #: 引き継いだタスクが解き直す衝突（統合に失敗したタスクを差し込みで引き継いだ）。在りかは
    #: 引き継ぎ元のタスクの id で、ファイルの一覧は TaskOpened.conflicts にある。差し込んだタスクの
    #: ResolveConflict が書いてよいファイル（WriteScope.LISTED）になる
    CONFLICTS = "conflicts"

    @property
    def committed(self) -> bool:
        """作ったコミットが実物になる成果物か。始めた時点から HEAD が進んだことで作ったと確かめ、
        在りかは HEAD にする。"""
        return self in (ArtifactKind.TESTS, ArtifactKind.IMPL)


#: ラン共通の成果物。計画タスクが作り、TaskStarted・ScopeChanged で実装タスクへ渡る
RUN_SHARED_ARTIFACTS: frozenset[ArtifactKind] = frozenset(
    {ArtifactKind.BRIEF, ArtifactKind.CODEMAP, ArtifactKind.DESIGN}
)
