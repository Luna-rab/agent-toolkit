"""Claude に返す見直しの指示の中身。2 つの hook が同じ形で返し、`render` が 1 か所で描く。

**`additionalContext` はユーザーの画面にも全文出る**（`Stop hook feedback:` の後ろ）。
`suppressOutput` はこの表示を隠さないので、見せ方は描き方で整える。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple


class Row(NamedTuple):
    place: str  # `path:line`
    label: str  # コメントの見出しやテスト名
    note: str = ""  # 行数や種類の添え書き


class Section(NamedTuple):
    title: str
    rows: tuple[Row, ...]
    #: この一覧だけに当てる基準。1 要素が 1 段落
    rules: tuple[str, ...] = ()
    #: 一覧に対して Claude がすること（「基本は消す」「基本は縮める」）
    action: str = ""
    #: 件数の数え方。テストは「本」
    unit: str = "件"


class Review(NamedTuple):
    #: 1 行目。画面では `Stop hook feedback:` の直後に来る
    title: str
    sections: tuple[Section, ...]
    #: どの一覧にも当てる基準の見出しと段落
    common_title: str
    common_rules: tuple[str, ...]
    #: 見直しを終えたときに Claude が書く 1 行の形と、その書き方
    reply_format: str
    reply_rules: tuple[str, ...] = ()
    #: 呼び出しの場面で足す注意（SubagentStop の報告の書き直しなど）
    notes: tuple[str, ...] = ()

    def total(self) -> int:
        return sum(len(section.rows) for section in self.sections)


#: 見つけた見直しと、出力を書き出してから呼ぶ「報告済みにする」処理。
#: 出す前に保存すると、後の失敗で見直しが出ないまま二度と上がらなくなる
Pending = tuple[Review, Callable[[], None]]
