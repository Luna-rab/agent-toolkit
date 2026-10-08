"""`turnreview.core.testcases` を、tree-sitter を通さずに確かめる。"""

from __future__ import annotations

from pathlib import Path

from turnreview.core.report import report_key
from turnreview.core.review import Row
from turnreview.core.testcases import Declared, FileTests, Suite, review_of, suite_of


def test_テストファイルの見分け方():
    assert suite_of(Path("tests/Feature/LoginTest.php")) == Suite("php", "php", phpunit=True)
    assert suite_of(Path("tests/Unit/login.php")) == Suite("php", "php", phpunit=False)
    assert suite_of(Path("src/a.spec.tsx")) == Suite("js", "tsx")
    assert suite_of(Path("test_a.py")) == Suite("python", "python")
    assert suite_of(Path("src/router.ts")) is None
    assert suite_of(Path("vendor/x/tests/ATest.php")) is None


def test_宣言の行を足したテストのうち未報告のものだけ並べる():
    lines = ["def test_new():", "    pass", "def test_old():", "    assert f() == 2"]
    tests = [Declared(1, "test_new", "def test_new(): ..."), Declared(3, "test_old", "...")]
    file = FileTests("test_a.py", lines, {"def test_new():", "assert f() == 2"}, tests)
    review, fresh = review_of([file], set())
    assert [section.rows for section in review.sections] == [(Row("test_a.py:1", "test_new"),)]
    assert fresh == {report_key("test", "test_a.py", "def test_new(): ...")}

    again, none = review_of([file], fresh)
    assert (again.sections, none) == ((), set())
