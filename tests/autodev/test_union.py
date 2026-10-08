"""UnionChecker（`domain/services/union.py`）。典型の衝突を例にする。"""

from __future__ import annotations

from autodev.domain.services.union import UnionChecker
from autodev.domain.value_objects.union_file_verdict import UnionFileVerdict
from autodev.domain.value_objects.union_verdict import UnionVerdict


def lines(*rows: str) -> str:
    return "\n".join(rows) + "\n"


def check(base: str, ours: str, theirs: str, resolved: str) -> UnionFileVerdict:
    # アダプタ（adapters/github/git.py）はファイルの中身をバイト列で返す
    return UnionChecker.check("f", base.encode(), ours.encode(), theirs.encode(), resolved.encode())


# --- 同じファイルの末尾に、別々に足した ---

TESTS_BASE = lines("def test_a():", "    assert a() == 1")
TESTS_OURS = lines(
    "def test_a():", "    assert a() == 1", "", "", "def test_b():", "    assert b() == 2"
)
TESTS_THEIRS = lines(
    "def test_a():", "    assert a() == 1", "", "", "def test_c():", "    assert c() == 3"
)


def test_末尾に別々に足した2つを両方残せば通る():
    resolved = lines(
        "def test_a():",
        "    assert a() == 1",
        "",
        "",
        "def test_b():",
        "    assert b() == 2",
        "",
        "",
        "def test_c():",
        "    assert c() == 3",
    )
    assert check(TESTS_BASE, TESTS_OURS, TESTS_THEIRS, resolved) == UnionFileVerdict(
        "f", kept_both=True
    )


def test_足した塊の順を入れ替えても間の空行の数が違っても通る():
    resolved = lines(
        "def test_a():",
        "    assert a() == 1",
        "",
        "def test_c():",
        "    assert c() == 3",
        "def test_b():",
        "    assert b() == 2   ",
    )
    assert check(TESTS_BASE, TESTS_OURS, TESTS_THEIRS, resolved).kept_both


def test_片方が足した行を落とすと落ちた行を挙げて通さない():
    resolved = TESTS_OURS
    verdict = check(TESTS_BASE, TESTS_OURS, TESTS_THEIRS, resolved)
    assert not verdict.kept_both
    assert verdict.missing == ("def test_c():", "    assert c() == 3")


# --- import の並び ---

IMPORT_BASE = lines("import json", "import os", "", "x = 1")


def test_両側が足したimportを並べ直して残せば通る():
    ours = lines("import json", "import os", "import re", "", "x = 1")
    theirs = lines("import collections", "import json", "import os", "", "x = 1")
    resolved = lines("import collections", "import json", "import os", "import re", "", "x = 1")
    assert check(IMPORT_BASE, ours, theirs, resolved).kept_both


def test_両側が同じimportを足したら1つにまとめてよい():
    ours = lines("import json", "import os", "import re", "", "x = 1")
    theirs = lines("import json", "import os", "import re", "", "x = 1", "y = 2")
    resolved = lines("import json", "import os", "import re", "", "x = 1", "y = 2")
    assert check(IMPORT_BASE, ours, theirs, resolved).kept_both


def test_どちらも消していないimportを消すと通さない():
    ours = lines("import json", "import os", "import re", "", "x = 1")
    theirs = lines("import collections", "import json", "import os", "", "x = 1")
    resolved = lines("import collections", "import json", "import re", "", "x = 1")
    verdict = check(IMPORT_BASE, ours, theirs, resolved)
    assert verdict.missing == ("import os",)


def test_片方が消した行は解いた結果に無くてよい():
    ours = lines("import json", "", "x = 1")
    theirs = lines("import json", "import os", "import re", "", "x = 1")
    resolved = lines("import json", "import re", "", "x = 1")
    assert check(IMPORT_BASE, ours, theirs, resolved).kept_both


# --- 片方の行を落とした（同じ行を両側が別々に書き換えた） ---


def test_同じ行を両側が書き換えて片方だけ残すと通さない():
    base = lines("TIMEOUT = 10")
    ours = lines("TIMEOUT = 30")
    theirs = lines("TIMEOUT = 60")
    verdict = check(base, ours, theirs, lines("TIMEOUT = 60"))
    assert not verdict.kept_both
    assert verdict.missing == ("TIMEOUT = 30",)


# --- 片方の書き換え・削除を戻した ---


def test_片方の書き換えを戻すと戻った行を挙げて通さない():
    base = lines("TIMEOUT = 10", "RETRIES = 1")
    ours = lines("TIMEOUT = 30", "RETRIES = 1")
    theirs = lines("TIMEOUT = 10", "RETRIES = 1", "VERBOSE = True")
    reverted = check(base, ours, theirs, lines("TIMEOUT = 10", "RETRIES = 1", "VERBOSE = True"))
    assert (reverted.kept_both, reverted.missing, reverted.revived) == (
        False,
        ("TIMEOUT = 30",),
        ("TIMEOUT = 10",),
    )
    # 新しい値を残しても、古い値まで残せば戻したのと同じ
    both = check(
        base, ours, theirs, lines("TIMEOUT = 10", "TIMEOUT = 30", "RETRIES = 1", "VERBOSE = True")
    )
    assert (both.kept_both, both.missing, both.revived) == (False, (), ("TIMEOUT = 10",))
    good = check(base, ours, theirs, lines("TIMEOUT = 30", "RETRIES = 1", "VERBOSE = True"))
    assert good.kept_both


def test_片方の削除を戻すと通さない():
    ours = lines("import json", "", "x = 1")  # 使わなくなった import os を消した
    theirs = lines("import json", "import os", "import re", "", "x = 1")
    resolved = lines("import json", "import os", "import re", "", "x = 1")
    verdict = check(IMPORT_BASE, ours, theirs, resolved)
    assert (verdict.kept_both, verdict.missing, verdict.revived) == (False, (), ("import os",))


def test_もう片方が足し直した行は残してよい():
    base = lines("a = 1", "b = 2")
    ours = lines("b = 2")  # a = 1 を消した
    theirs = lines("a = 1", "b = 2", "a = 1")  # a = 1 をもう 1 つ足した
    assert check(base, ours, theirs, lines("b = 2", "a = 1")).kept_both
    revived = check(base, ours, theirs, lines("a = 1", "b = 2", "a = 1"))
    assert revived.revived == ("a = 1",)


# --- 両方の行を残して、構文が壊れた ---


def test_両方の行を残せば構文が壊れていても通す_構文はVerifyが見る():
    base = lines("CONFIG = {", '    "a": 1,', "}")
    ours = lines("CONFIG = {", '    "a": 1,', '    "b": 2', "}")
    theirs = lines("CONFIG = {", '    "a": 1,', '    "c": 3', "}")
    # "b": 2 の後のカンマが無いので、Python としては読めない
    resolved = lines("CONFIG = {", '    "a": 1,', '    "b": 2', '    "c": 3', "}")
    assert check(base, ours, theirs, resolved).kept_both


def test_衝突の印が残っていれば両側の行があっても通さない():
    base = lines("x = 1")
    ours = lines("x = 1", "y = 2")
    theirs = lines("x = 1", "z = 3")
    resolved = lines("x = 1", "<<<<<<< ours", "y = 2", "=======", "z = 3", ">>>>>>> theirs")
    verdict = check(base, ours, theirs, resolved)
    assert not verdict.kept_both
    assert verdict.missing == ()
    assert verdict.markers == ("<<<<<<< ours", "=======", ">>>>>>> theirs")


SETEXT = lines("Title", "=======", "", "body")


def test_元から区切りの行を持つファイルは増えた分だけを印に数える():
    ours = lines("Title", "=======", "", "body", "ours")
    theirs = lines("Title", "=======", "", "body", "theirs")
    resolved = lines("Title", "=======", "", "body", "ours", "theirs")
    assert check(SETEXT, ours, theirs, resolved).kept_both
    left = lines(
        "Title", "=======", "", "body", "<<<<<<< ours", "ours", "=======", "theirs", ">>>>>>> b"
    )
    verdict = check(SETEXT, ours, theirs, left)
    # 元からある 1 つの `=======` を超えた分だけ。並びは解いた結果に初めて現れた順
    assert verdict.markers == ("=======", "<<<<<<< ours", ">>>>>>> b")


def test_片方が区切りの行を足したならその数までは印に数えない():
    ours = lines("Title", "=======", "", "body", "Next", "=======")
    theirs = lines("Title", "=======", "", "body", "more")
    resolved = lines("Title", "=======", "", "body", "more", "Next", "=======")
    verdict = check(SETEXT, ours, theirs, resolved)
    assert (verdict.kept_both, verdict.markers) == (True, ())


# --- 新しく足したファイル ---


def test_両側が同じ名前で足したファイルは祖先を空にして見る():
    ours = lines("a")
    theirs = lines("b")
    assert check("", ours, theirs, lines("b", "a")).kept_both
    assert not check("", ours, theirs, lines("a")).kept_both


def test_無いファイルはNoneで受けて空として見る():
    assert UnionChecker.check("f", None, b"a\n", b"b\n", b"a\nb\n").kept_both


def test_衝突の段が無ければ両側が分からないので通さない():
    # 解きかけを git add した（index の段 1・2・3 がどれも無い）
    verdict = UnionChecker.check("f", None, None, None, b"a\nb\n")
    assert verdict == UnionFileVerdict("f", kept_both=False, unreadable=True)
    assert UnionChecker.still_conflicted("g") == UnionFileVerdict("g", kept_both=False)


def test_UTF8として読めないかバイナリのファイルの衝突は意味が変わる統合として通さない():
    latin1 = "caf\xe9\n".encode("latin-1")
    for sides in (
        (b"a\n", latin1, b"a\nb\n", b"a\nb\n"),
        (b"a\n", b"a\x00\x01\n", b"a\nb\n", b"a\nb\n"),
        (None, b"a\n", b"b\n", b"\xff\xfe"),
    ):
        verdict = UnionChecker.check("f", *sides)
        assert verdict == UnionFileVerdict("f", kept_both=False, unreadable=True)


def test_ファイルごとの判定を集めて全部通ったときだけ通る():
    good = check("", lines("a"), lines("b"), lines("a", "b"))
    bad = UnionChecker.check("g", b"", lines("a").encode(), lines("b").encode(), b"a\n")
    assert UnionVerdict((good,)).passed
    assert not UnionVerdict((good, bad)).passed
