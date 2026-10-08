from __future__ import annotations

from dataclasses import dataclass

from .branch_name import BranchName


@dataclass(frozen=True)
class CutPoint:
    """CutBranch が切る元。"""

    start: BranchName
    #: 切る元がランの base か（`refs` の並びを決める）
    run_base: bool = False
    #: ブランチを作らず、HEAD を切る元のコミットに固定した worktree にする（読むだけの stack-top）
    detached: bool = False
    #: 切った元のコミットが、相手のタスクのブランチの根元（`Task.base_commit`）になるか。積み直す
    #: タスクは前に積んだブランチの先端から切るので、そこは根元ではない。根元を上書きすると、Rebase
    #: が載せ直すコミットが 0 件になる（根元は前に覚えたまま、Rebase がそこから上を載せ直す）
    roots_branch: bool = True

    @classmethod
    def for_overview(cls, run_base: BranchName) -> CutPoint:
        """概要ブランチを切る元。ランの base から切る。"""
        return cls(run_base, run_base=True)

    def refs(self) -> tuple[str, ...]:
        """切る元の参照の候補を、使う順に並べる。実在する最初の 1 つを使い、どれも無ければ最後の 1 つを使う。

        ランの base は対象リポジトリの外で進むので、手元より origin の方が新しいことがある。origin を先に
        見る。autodev が切ったブランチは手元が正しいので、手元の名前だけを見る。
        """
        if self.run_base:
            return (f"origin/{self.start}", str(self.start))
        return (str(self.start),)
