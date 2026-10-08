from __future__ import annotations

from .base import Text, non_blank


class GlobPattern(Text):
    """テストのパス・変更禁止パス・テストが要らないパス。照合の規則はアダプタとフックが持つ。"""

    def _check(self) -> None:
        non_blank("glob", self.value)


#: テストのパスの既定
DEFAULT_TEST_GLOBS: tuple[GlobPattern, ...] = tuple(
    GlobPattern(g)
    for g in (
        "**/test_*.py",
        "**/*_test.py",
        "**/tests/**",
        "**/*_test.go",
        "**/*.test.ts",
        "**/*.test.tsx",
        "**/*.spec.ts",
        "**/*.spec.tsx",
        "**/*Test.java",
        "**/*_spec.rb",
        "spec/**",
    )
)
