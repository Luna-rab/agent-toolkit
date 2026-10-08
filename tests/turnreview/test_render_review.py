"""`turnreview.render.review` の描き方の約束を確かめる。崩れると `Stop hook feedback:` の欄で枠がずれる。"""

from __future__ import annotations

import re

import pytest
from rich.cells import cell_len

from turnreview.core import comment_message
from turnreview.core.comment_message import FileSpans
from turnreview.core.comments import Span
from turnreview.core.review import Review, Row, Section
from turnreview.render.review import fold, render

ANSI = re.compile(r"\x1b\[[0-9;]*m")

SAMPLE = Review(
    title="このターンで足したコメント 1 件を、応答を終える前に見直す",
    sections=(
        Section(
            "コードのコメント",
            (Row("src/a.py:12", "入力を検証する", "2 行"),),
            (
                "残すのは、知らないと間違えることだけ。外部の制約（API の上限、OS の違い、既知のバグ）。"
                * 3,
            ),
            "消す",
        ),
    ),
    common_title="どのセクションでも",
    common_rules=("経緯そのものはコミットメッセージと PR 本文に書く。",),
    reply_format="`path:line` — 残す｜消す: 理由",
    notes=("最後に、見直す前に書いた最終報告を省かずにもう一度書く。",),
)


def test_句読点の後ろで折り返し_行頭に空白と約物を置かない():
    # 全角 1 文字は 2 桁。「、」が行頭に来るときは、前の行の「う」と一緒に送る
    assert fold("あいう、えお", 6).splitlines() == ["あい", "う、", "えお"]
    assert fold("あいう abc", 6).splitlines() == ["あいう", "abc"]
    # 句読点があれば空白より優先する。「2 で」の間では切らない
    assert fold("あいう。2 でえお", 14).splitlines() == ["あいう。", "2 でえお"]
    # 送る文字（「）」）も行頭に置けないので、その前の「い」から送る
    assert fold("あい）、う", 6).splitlines() == ["あ", "い）、", "う"]


def test_1行目は枠ではなく見出しで_どの行も幅に収まる():
    lines = [ANSI.sub("", line) for line in render(SAMPLE, 60).splitlines()]
    assert lines[0].startswith("● turnreview ▸ ")
    assert [line for line in lines if cell_len(line) > 60] == []
    assert any(line.startswith("※ 最後に") for line in lines)


def test_色の付いた文字列を返す():
    assert "\x1b[" in render(SAMPLE, 60)


def test_コメントのセクションごとに_することを決まった色で描く(monkeypatch: pytest.MonkeyPatch):
    # core の語を変えて theme の表を直し忘れると、色は黙って既定の青に戻る
    monkeypatch.delenv("NO_COLOR", raising=False)
    files = [
        FileSpans("a.py", False, [Span(1, 1, "a", "line", "a"), Span(3, 5, "b", "block", "b")]),
        FileSpans("c.toml", True, [Span(1, 1, "c", "line", "c")]),
    ]
    rendered = render(comment_message.review_of(files, set())[0], 80)
    assert "\x1b[38;5;211m消すのが基本" in rendered  # 赤
    assert "\x1b[38;5;223m縮めるのが基本" in rendered  # 黄
    assert "\x1b[38;5;151m多めに残してよい" in rendered  # 緑
