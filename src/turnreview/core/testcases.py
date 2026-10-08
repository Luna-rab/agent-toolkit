"""どのファイルのどのテストを見直させ、何を答えさせるか。

挙動を確かめないテスト（ドキュメントの文言を読む、定数や `method_exists` を確かめるだけ、
ソースを文字列として grep する）を消すか書き直させるのが狙い。

**形で絞らず、宣言の行（テスト名がある行）を足したテストを全部並べる。** 無意味なテストの形を
列挙して照合すると、列挙から漏れた形を拾えない。代わりに 1 本ごとに次の 2 つを答えさせる。

1. このテストを落とす本番コードの変更は何か
   （obra/superpowers の "Name the production change that would make this test fail."）
2. 同じ変更で落ちる既存のテストが無いのはなぜか（dashed/claude-marketplace の test-audit）

答えを 1 本 1 行で書かせる。件数だけを報告させると、見ずに「0 件」と返して終えられる。

**本体だけを変えたテストは並べない。** 既存のテストの assert を 1 行直すたびに上がると、
件数が膨らんで読まれなくなる。
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from turnreview.core.report import report_key
from turnreview.core.review import Review, Row, Section
from turnreview.core.turn import is_generated

JS_GRAMMARS = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
}
TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__", "spec"})


class Suite(NamedTuple):
    """テストファイルの読み方。`syntax` がこれを見て取り出し方を選ぶ。"""

    language: str  # "python" | "php" | "js"
    grammar: str  # tree-sitter の文法名。Python は ast で読むので "python"
    phpunit: bool = False  # `*Test.php`。クラスのメソッドをテストとして読む


class Declared(NamedTuple):
    """`syntax` が取り出したテスト 1 本。"""

    line: int  # 宣言の行。1 始まり
    name: str
    text: str  # テスト全体の本文。報告済みの鍵に使う


class FileTests(NamedTuple):
    """1 ファイル分の報告の候補。"""

    name: str  # 報告に出すパス
    lines: list[str]
    added: set[str]
    tests: list[Declared]


def under_test_dir(path: Path) -> bool:
    return bool(TEST_DIR_NAMES.intersection(path.parts[:-1]))


def suite_of(path: Path) -> Suite | None:
    """テストファイルなら読み方を返す。テストファイルでなければ None。"""
    if is_generated(path):
        return None
    name = path.name
    suffix = path.suffix.lower()
    if suffix == ".py":
        named = name.startswith("test_") or name.endswith("_test.py")
        suite = Suite("python", "python")
    elif suffix == ".php":
        named = name.endswith("Test.php")
        suite = Suite("php", "php", phpunit=named)
    elif suffix in JS_GRAMMARS:
        named = ".test." in name or ".spec." in name
        suite = Suite("js", JS_GRAMMARS[suffix])
    else:
        return None
    return suite if named or under_test_dir(path) else None


QUESTION_TITLE = "見直すときに考えること"
QUESTION_RULES = (
    "1. 本番コードをどう変えたら、このテストは落ちるか",
    "2. その変更で落ちるテストが、ほかにないか",
    "1 が「文言や定数を変えたとき」だけなら、動きを見ていないテストです。"
    "2 でほかに見つかるなら重複です。どちらも消すか、書き直してください。",
    "ドキュメントやソースをファイルとして読むテストは、答えにかかわらず消してください。",
    "書き直すときは、関数や CLI を呼んで、返り値や出力を確かめてください。",
)

REPLY_FORMAT = (
    "`path:line` — 残す／消す／書き直す：落ちるのは〜したとき。ほかで拾えないのは〜だから。"
)
REPLY_RULES = ("一覧のテストそれぞれを、この形で 1 行ずつ書いて終えてください。",)


def review_of(files: list[FileTests], reported: set[str]) -> tuple[Review, set[str]]:
    """宣言の行を足したテストのうち未報告のものを並べる。2 つ目は今回はじめて報告する鍵。"""
    rows: list[Row] = []
    fresh: set[str] = set()
    for file in files:
        for test in sorted(file.tests):
            if test.line > len(file.lines) or file.lines[test.line - 1].strip() not in file.added:
                continue
            key = report_key("test", file.name, test.text)
            if key in reported:
                continue
            fresh.add(key)
            rows.append(Row(f"{file.name}:{test.line}", test.name))
    section = Section("足したテスト", tuple(rows), action="1 本ずつ", unit="本")
    review = Review(
        title=f"テストを {len(rows)} 本足しています。終える前に 1 本ずつ見直してください",
        sections=(section,) if rows else (),
        common_title=QUESTION_TITLE,
        common_rules=QUESTION_RULES,
        reply_format=REPLY_FORMAT,
        reply_rules=REPLY_RULES,
    )
    return review, fresh
