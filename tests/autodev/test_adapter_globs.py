"""glob の照合。フックと完了チェックが同じ照合を使う。"""

from __future__ import annotations

import pytest

from autodev.adapters.process import globs
from autodev.domain.value_objects.glob_pattern import DEFAULT_TEST_GLOBS


@pytest.mark.parametrize(
    "path",
    [
        "test_a.py",
        "src/test_a.py",
        "src/a_test.py",
        "tests/a.py",
        "a/b/tests/c.py",
        "pkg/a_test.go",
        "web/a.test.ts",
        "web/a.spec.tsx",
        "src/FooTest.java",
        "lib/a_spec.rb",
        "spec/a/b.rb",
        "tests/golden/draw.json",
    ],
)
def test_既定のテストのパスに当たる(path: str):
    assert globs.matches_any(path, DEFAULT_TEST_GLOBS)


@pytest.mark.parametrize("path", ["src/app.py", "src/testing.py", "docs/tests.md", "src/attest.py"])
def test_既定のテストのパスに当たらない(path: str):
    assert not globs.matches_any(path, DEFAULT_TEST_GLOBS)


def test_末尾がスラッシュの指定はディレクトリの名前で当てる():
    assert globs.matches("tests/a.py", "tests/")
    assert globs.matches("a/b/tests/c.py", "tests/")
    assert globs.matches("tests", "tests/")
    assert not globs.matches("testsuite/a.py", "tests/")


def test_リポジトリ直下のtestsにも当てる():
    """`**/tests/**` の `**/` は 0 段にも当たる。fnmatch だけでは直下の `tests/a.py` が漏れる。"""
    assert globs.matches("tests/a.py", "**/tests/**")


def test_末尾からの照合と根からの照合の両方で当てる():
    assert globs.matches("a/b/tests/c.py", "tests/*.py")
    assert globs.matches("spec/a/b.rb", "spec/**")


def test_星と疑問符はスラッシュをまたがない():
    """fnmatch の `*` は `/` をまたぐので、`src/*.py` が `src/a/b.py` に当たってしまう。"""
    assert globs.matches("src/a.py", "src/*.py")
    assert not globs.matches("src/a/b.py", "src/*.py")
    assert globs.matches("tests/a/x.py", "tests/?/x.py")
    assert not globs.matches("tests/ab/x.py", "tests/?/x.py")
    assert globs.matches("src/a/b/c.py", "src/**/*.py")
    assert globs.matches("src/c.py", "src/**/*.py")


def test_角括弧の文字クラスを扱う():
    assert globs.matches("tests/a1.py", "tests/a[0-9].py")
    assert not globs.matches("tests/ax.py", "tests/a[0-9].py")
    assert globs.matches("tests/ax.py", "tests/a[!0-9].py")


def test_区切りと先頭のドットスラッシュを寄せてから照合する():
    assert globs.normalize("a\\b\\test_c.py") == "a/b/test_c.py"
    assert globs.normalize("./a/b.py") == "a/b.py"
    assert globs.matches_any("./tests/test_a.py", DEFAULT_TEST_GLOBS)


def test_当たったものだけを取り出す():
    changed = ["src/a.py", "tests/test_a.py", "README.md", "src/b_test.py"]
    assert globs.pick(changed, DEFAULT_TEST_GLOBS) == ["tests/test_a.py", "src/b_test.py"]
