from __future__ import annotations

from dataclasses import dataclass

from .artifact_kind import ArtifactKind
from .base import non_blank


@dataclass(frozen=True)
class ArtifactRef:
    """成果物と、実物の在りか。

    `at` の中身は種類で決まる。`tests` は TestGen（Expect が期待値を書いたらそのコミット）の
    CommitSha、`design` と `proposal` は版の番号（`proposal` は、実行器が提案の本文を書き出した
    `design/v<版>.md` の版）、`conflicts` は引き継ぎ元のタスクの id、ほかはランディレクトリからの
    パスかコミット。
    """

    kind: ArtifactKind
    at: str

    def __post_init__(self) -> None:
        non_blank("成果物の在りか", self.at)
