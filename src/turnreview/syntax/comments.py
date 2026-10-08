"""ソースの文字列から、コメントと Python の docstring の位置と本文を取り出す。

**コメントの切り出しは tree-sitter に任せる。** 行頭の記号で照合する方式だと、
`printf("/* x")` のように文字列リテラルの中に記号がある行をコメントと誤って拾い、
逆に行の途中から始まるコメントを取りこぼす。構文木なら文字列とコメントを取り違えない。

**Python の docstring だけは `ast` で拾う。** tree-sitter から見ると docstring は
ただの文字列ノードなので、構文木では普通の複数行文字列と区別できない。モジュール・
クラス・関数の本体の先頭にある文字列だけが docstring である。
"""

from __future__ import annotations

import ast

from tree_sitter_language_pack import get_parser

from turnreview.core.comments import RawComment


def comments(grammar: str, source: str) -> list[RawComment]:
    """構文木からコメントノードを集める。

    Rust の `///` や Lua の `--[[ ]]` はコメントノードの中に子ノード（doc_comment、
    comment_content）を持つので、コメントを見つけたらその下には潜らない。
    """
    tree = get_parser(grammar).parse(source.encode("utf-8"))  # type: ignore[arg-type]
    found: list[RawComment] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if "comment" in node.type:
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            # 末尾の改行を含むノード（Rust の line_comment）は 1 行はみ出すので戻す
            if node.end_point[1] == 0 and end > start:
                end -= 1
            found.append(
                RawComment(start, end, (node.text or b"").decode("utf-8", errors="replace"))
            )
            continue
        stack.extend(reversed(node.children))
    return found


def python_docstrings(source: str) -> list[tuple[int, int]]:
    """モジュール・クラス・関数の docstring の (開始行, 終了行)。

    編集途中で構文が壊れているファイルは諦めて空を返す。
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, holders) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            spans.append((first.lineno, first.end_lineno or first.lineno))
    return spans
