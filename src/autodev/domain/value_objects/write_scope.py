from __future__ import annotations

from enum import Enum


class WriteScope(Enum):
    """worktree の中で書いてよい範囲。"""

    NONE = "none"
    NON_TESTS = "non-tests"
    TESTS_AND_STUBS = "tests-and-stubs"
    TESTS_ONLY = "tests-only"
    #: 実行のときに渡すパスの一覧だけ（ResolveConflict の衝突したファイル）
    LISTED = "listed"
