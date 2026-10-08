"""`turnreview.core.comments` と `core.languages` を、tree-sitter を通さずに確かめる。

`syntax` が返す形（`RawComment` と docstring の行の組）を手で組んで渡す。
"""

from __future__ import annotations

from pathlib import Path

from turnreview.core.comments import RawComment, spans_in_file
from turnreview.core.languages import Language, language_for, wants_shebang


def test_足した行に重なる1行コメントの連なりを1つにまとめる():
    lines = ["# 1 行目", "# 2 行目", "x = 1", "# 前からある"]
    comments = [
        RawComment(1, 1, "# 1 行目"),
        RawComment(2, 2, "# 2 行目"),
        RawComment(4, 4, "# 前からある"),
    ]
    spans = spans_in_file(lines, {"# 1 行目", "# 2 行目"}, comments, [], python=False)
    assert [(s.start, s.end, s.heading, s.kind) for s in spans] == [(1, 2, "1 行目", "line")]


def test_docstringは足した行に英数字が無ければ拾わない():
    lines = ['"""説明。', "", '"""']
    spans = spans_in_file(lines, {'"""'}, [], [(1, 3)], python=True)
    assert spans == []


def test_動作に効くコメントは外す():
    lines = ["import os  # noqa: F401", "/** @test */"]
    comments = [RawComment(1, 1, "# noqa: F401"), RawComment(2, 2, "/** @test */")]
    assert spans_in_file(lines, set(lines), comments, [], python=False) == []


def test_拡張子の無いファイルだけshebangを読む():
    assert wants_shebang(Path("bin/tool"))
    assert not wants_shebang(Path("a.py"))
    assert not wants_shebang(Path("Makefile"))
    assert language_for(Path("bin/tool"), "#!/usr/bin/env python3\n") == Language(
        "python", python=True
    )
    assert language_for(Path("bin/tool"), "no shebang\n") is None
    assert language_for(Path("bin/tool")) is None
