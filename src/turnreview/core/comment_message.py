"""報告するコメントの一覧と、見直しの基準を 1 つの `Review` にまとめる。

**書いた経緯に寄りかかったコメントを直させる指示は、セクションごとではなく共通で 1 回出す。**
どの記法でも同じ基準で裁けるうえ、3 つに分けて書くと同じ文が 3 回並ぶ（`HISTORY_RULES`）。

**3 つのセクションに分けて、指示を別にする。** 性質が違うので同じ基準では裁けない。

| セクション           | 中身                                                 | 指示           |
| -------------------- | ---------------------------------------------------- | -------------- |
| コードのコメント     | 1 行コメントの連なり                                 | 消す           |
| ブロックと docstring | 複数行のコメント、docstring、`///` `//!` `/**`        | 縮める         |
| 設定ファイル         | TOML・YAML・Dockerfile・Makefile などのコメント       | 緩めに見て消す |

**見つけたコメントは全件並べ、1 件 1 行で判断と理由を書かせる。** 件数だけを報告させると、
見ずに「0 件」と返して終えられる。一覧を途中で切ると、切った分を見ないまま終える。
残す理由には「消したら読者が何を間違えるか」を書かせ、挙げられないものは消させる。
"""

from __future__ import annotations

from typing import NamedTuple

from turnreview.core.comments import Span
from turnreview.core.report import report_key
from turnreview.core.review import Review, Row, Section

MAX_HEADING_CHARS = 60

# "line" 以外は「消す」ではなく「縮める」セクションへ入る
KIND_LABELS = {"docstring": "docstring", "doc": "doc コメント", "block": "ブロック"}

HISTORY_TITLE = "どのコメントにも言えること"
HISTORY_RULES = (
    "コメントには、いまのコードのことだけを書いてください。",
    "「以前は〜だった」「レビューで言われたので」のような経緯、採らなかった案、自分の TODO は、"
    "コードだけ読む人には確かめられません。",
    "残したい中身は「B でないと C が壊れる」のように書き直し、経緯はコミットメッセージに書いてください。",
)

CODE_RULES = (
    "処理を言い直しただけの行や、変数名の和訳は消してください。",
    "残すのは、知らずに触ると間違えることです。API の上限のような外の事情、そう書いた理由、"
    "守らないと壊れる約束、測って決めた値がそれに当たります。",
)

BLOCK_RULES = (
    "docstring は消さずに短くしてください。1 文で足りるなら 1 文で。",
    "Args や Returns の列挙、処理の言い直しは要りません。"
    "コードで分かることしか書いていないなら、丸ごと消してください。",
)

CONFIG_RULES = (
    "設定のコメントは、コードより多めに残して構いません。",
    "キー名の和訳や既定値の書き写しは消し、値を選んだ理由や出どころは残してください。",
)

REPLY_FORMAT = "`path:line` — 残す／消す／縮める／書き換える：理由"
REPLY_RULES = (
    "一覧のコメントそれぞれを、この形で 1 行ずつ書いて終えてください。",
    "残す理由には「消したら読む人が何を間違えるか」を書きます。「読みやすいから」は理由になりません。",
)


class FileSpans(NamedTuple):
    """1 ファイル分の報告の候補。"""

    name: str  # 報告に出すパス
    config: bool  # 設定ファイルか
    spans: list[Span]


def clip(text: str) -> str:
    return text if len(text) <= MAX_HEADING_CHARS else text[:MAX_HEADING_CHARS] + "…"


def row_of(name: str, span: Span) -> Row:
    length = span.end - span.start + 1
    if span.kind == "line":
        note = f"{length} 行" if length > 1 else ""
    else:
        note = f"{KIND_LABELS[span.kind]}・{length} 行"
    return Row(f"{name}:{span.start}", clip(span.heading), note)


def review_of(files: list[FileSpans], reported: set[str]) -> tuple[Review, set[str]]:
    """報告済みを除いて 3 つのセクションに振り分ける。2 つ目は今回はじめて報告する鍵。"""
    code: list[Row] = []
    block: list[Row] = []
    config: list[Row] = []
    fresh: set[str] = set()
    for file in files:
        for span in file.spans:
            key = report_key(span.kind, file.name, span.text)
            if key in reported:
                continue
            fresh.add(key)
            row = row_of(file.name, span)
            if span.kind != "line":
                block.append(row)
            elif file.config:
                config.append(row)
            else:
                code.append(row)

    candidates = (
        Section("コードのコメント", tuple(code), CODE_RULES, "消すのが基本"),
        Section("ブロックコメントと docstring", tuple(block), BLOCK_RULES, "縮めるのが基本"),
        Section("設定ファイルのコメント", tuple(config), CONFIG_RULES, "多めに残してよい"),
    )
    sections = tuple(section for section in candidates if section.rows)
    total = sum(len(section.rows) for section in sections)
    review = Review(
        title=f"コメントを {total} 件足しています。終える前に見直してください",
        sections=sections,
        common_title=HISTORY_TITLE,
        common_rules=HISTORY_RULES,
        reply_format=REPLY_FORMAT,
        reply_rules=REPLY_RULES,
    )
    return review, fresh
