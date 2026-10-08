"""構文木から取り出したコメントのうち、どれを見直しの対象として報告するか。

足した行に重なるコメントだけを残し、1 行コメントの連なりを 1 つの塊にまとめる。
消すと動作が変わるもの（pragma・shebang・PEP 723・アノテーション）と、区切り行は外す。
"""

from __future__ import annotations

from typing import NamedTuple


class RawComment(NamedTuple):
    """`syntax` が取り出したコメント 1 つ。"""

    start: int  # 1 始まり
    end: int  # 1 始まり、この行を含む
    text: str


class Span(NamedTuple):
    start: int  # 1 始まり
    end: int  # 1 始まり、この行を含む
    heading: str  # 報告に出す 1 行（記号を落とした本文）
    kind: str  # "line" | "doc" | "block" | "docstring"
    text: str  # コメント全体の本文。報告済みの鍵に使う


# コメントの形をしているが、消すと動作が変わるもの。
# TODO と FIXME も外す——未完の作業を指しており、コードからは読み取れない
SKIP_BODY_PREFIXES = (
    "noqa",
    "type:",
    "pyright:",
    "mypy:",
    "ruff:",
    "flake8:",
    "pylint:",
    "fmt:",
    "pragma:",
    "coding:",
    "coding=",
    "-*-",
    "shellcheck",
    "eslint",
    "prettier-",
    "biome-",
    "@ts-",
    "tslint:",
    "istanbul",
    "deno-lint",
    "nolint",
    "noinspection",
    "go:",
    "keep-sorted",
    "region",
    "endregion",
    "todo",
    "fixme",
    "spdx-",
    "copyright",
    "license",
)

# `strip_markers` が先頭から順に当てるので、長い記号を先に置く（`///` は `//` より前）
OPENERS = (
    "{/**",
    "{/*",
    "///",
    "//!",
    "/**",
    "/*",
    "<!--",
    "--[[",
    '"""',
    "'''",
    "{-",
    "//",
    "--",
    ";;;",
    ";;",
    "#",
    ";",
    "%",
    "*",
)
CLOSERS = ("*/}", "*/", "-->", "]]", "-}", '"""', "'''")


def has_words(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def strip_markers(text: str) -> str:
    body = text.strip()
    for opener in OPENERS:
        if body.startswith(opener):
            body = body[len(opener) :].strip()
            break
    for closer in CLOSERS:
        if body.endswith(closer):
            body = body[: -len(closer)].strip()
            break
    return body


def heading_of(raw: str) -> str:
    """コメントの中から、報告に出す 1 行を取り出す。

    ファイルの行ではなくコメントの文字列から取る。`x = 1  # 1 を入れる` のような
    行末のコメントで、見出しにコードまで入らないようにするため。
    """
    for line in raw.splitlines():
        body = strip_markers(line)
        if has_words(body):
            return body
    return "（説明なし）"


def pep723_lines(lines: list[str]) -> set[int]:
    """PEP 723 の `# /// script` 〜 `# ///` に挟まれた行番号。依存の宣言なので判断対象にしない。"""
    inside = False
    result: set[int] = set()
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not inside and stripped.startswith("# /// "):
            inside = True
        if inside:
            result.add(number)
            if stripped == "# ///":
                inside = False
    return result


def kind_of(raw: str, start: int, end: int) -> str:
    stripped = raw.strip()
    # JSDoc は 1 行で書かれていても関数の説明を置く場所なので、docstring と同じ扱いにする
    if stripped.startswith(("/**", "{/**")):
        return "docstring"
    if end > start:
        return "block"
    return "doc" if stripped.startswith(("///", "//!")) else "line"


def is_noise(raw: str, start: int, skipped: set[int]) -> bool:
    """消す・縮めるの判断対象にならないコメントか。"""
    if start in skipped:
        return True
    stripped = raw.strip()
    if start == 1 and stripped.startswith("#!"):
        return True
    body = strip_markers(raw)
    if body.lower().startswith(SKIP_BODY_PREFIXES):
        return True
    # `/** @test */` や `/** @var Foo $x */` はツールが読む印で、消すと動作が変わる
    tags = [line for line in (strip_markers(part) for part in raw.splitlines()) if has_words(line)]
    if tags and all(line.startswith("@") for line in tags):
        return True
    # `# ----` のような区切り行。コメントではなく体裁なので数えない
    return not has_words(body)


def spans_in_file(
    lines: list[str],
    added: set[str],
    comments: list[RawComment],
    docstrings: list[tuple[int, int]],
    *,
    python: bool,
) -> list[Span]:
    """足された行に重なるコメントを、連なりでまとめて返す。

    1 行コメントの連なりをまとめるのは、4 行の Why コメントを 4 件として見せないため。
    `///` の連なりも 1 つの塊として扱う。
    """
    skipped = pep723_lines(lines) if python else set()

    hits: list[tuple[int, int, str, str]] = []  # (開始行, 終了行, 種類, 生の文字列)
    for start, end, raw in comments:
        if start > len(lines) or is_noise(raw, start, skipped):
            continue
        hits.append((start, end, kind_of(raw, start, end), raw))
    hits.extend(
        (start, end, "docstring", "\n".join(lines[start - 1 : end])) for start, end in docstrings
    )

    # `"""` や `*/` だけの行は数えない。どの docstring の閉じにも当たり、触っていない塊まで拾う
    added = {line for line in added if has_words(line)}
    kept = [
        (start, end, kind, raw)
        for start, end, kind, raw in sorted(set(hits))
        if any(line.strip() in added for line in lines[start - 1 : end])
    ]

    spans: list[Span] = []
    for start, end, kind, raw in kept:
        merged = (
            spans
            and kind in ("line", "doc")
            and spans[-1].kind == kind
            and spans[-1].end + 1 == start
        )
        if merged:
            previous = spans.pop()
            spans.append(
                Span(previous.start, end, previous.heading, kind, f"{previous.text}\n{raw}")
            )
        else:
            spans.append(Span(start, end, heading_of(raw), kind, raw))
    return spans
