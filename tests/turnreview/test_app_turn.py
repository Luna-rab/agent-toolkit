from __future__ import annotations

import pytest

from turnreview.app import turn
from turnreview.core.review import Review, Row, Section


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        ("130", 116),  # 上限 120 で切ってから、`Stop hook feedback:` の字下げの 4 桁を引く
        ("80", 76),
        ("", 96),
        ("wide", 96),
        ("20", 40),
    ],
)
def test_COLUMNSから描く幅を決める(monkeypatch, columns: str, expected: int):
    monkeypatch.setenv("COLUMNS", columns)
    assert turn.width() == expected


def test_上限に収まるなら色を落とさずに描く():
    small = Review(
        title="コメントを 1 件足しています。終える前に見直してください",
        sections=(Section("コードのコメント", (Row("a.py:1", "見出し"),), action="消すのが基本"),),
        common_title="どのコメントにも言えること",
        common_rules=("経緯はコミットメッセージか PR に書いてください。",),
        reply_format="`path:line` — 残す／消す：理由",
    )
    message = turn.fitted(small)
    # 色を落とすのは上限を超えるときだけ。落とすと画面で一覧と基準の見分けがつかなくなる
    assert "\x1b[" in message
    assert "見出し" in message
