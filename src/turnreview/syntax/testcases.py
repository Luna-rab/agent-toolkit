"""ソースの文字列から、テストの宣言の行・名前・本文を取り出す。

見るのは Python（`test` で始まる関数）、PHP（`*Test.php` の public メソッドのうち `test` で
始まるか `#[Test]` / `@test` の付いたもの、Pest の `it()` / `test()`）、JS/TS（`it()` / `test()`）。
`describe()` はテストをまとめる枠なので数えない。
"""

from __future__ import annotations

import ast

from tree_sitter_language_pack import get_parser

from turnreview.core.testcases import Declared, Suite

# `it.only(...)` や `test.skip(...)` も数える
TEST_CALLEES = frozenset({"it", "test"})
GRAMMARS = ("php", "javascript", "typescript", "tsx")


def declared(suite: Suite, source: str) -> list[Declared]:
    if suite.language == "python":
        return python_tests(source)
    tree = get_parser(suite.grammar).parse(source.encode("utf-8"))  # type: ignore[arg-type]
    if suite.language == "php":
        return php_tests(tree.root_node, phpunit=suite.phpunit)
    return js_tests(tree.root_node)


def python_tests(source: str) -> list[Declared]:
    """`test` で始まる関数。`Test*` クラスのメソッドも含む。"""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []
    lines = source.splitlines()
    return [
        Declared(node.lineno, node.name, "\n".join(lines[node.lineno - 1 : node.end_lineno]))
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ]


def node_text(node) -> str:
    return (node.text or b"").decode("utf-8", errors="replace")


def string_value(node) -> str | None:
    if node is None or node.type not in ("string", "template_string", "encapsed_string"):
        return None
    return node_text(node)[1:-1]


def first_argument(call):
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return None
    for child in arguments.named_children:
        # PHP は引数を `argument` ノードで包む
        return (
            child.named_children[0] if child.type == "argument" and child.named_children else child
        )
    return None


def walk(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def labelled(call) -> list[Declared]:
    """第 1 引数が文字列なら、それをテスト名にして返す。宣言の行はテスト名の行とする。"""
    argument = first_argument(call)
    label = string_value(argument)
    if argument is None or label is None:
        return []
    return [Declared(argument.start_point[0] + 1, label, node_text(call))]


def is_php_test_method(method) -> bool:
    if any(
        child.type == "visibility_modifier" and node_text(child) != "public"
        for child in method.children
    ):
        return False
    name = method.child_by_field_name("name")
    if name is not None and node_text(name).startswith("test"):
        return True
    if any(
        child.type == "attribute_list" and "Test" in node_text(child) for child in method.children
    ):
        return True
    previous = method.prev_named_sibling
    return previous is not None and previous.type == "comment" and "@test" in node_text(previous)


def php_tests(root, *, phpunit: bool) -> list[Declared]:
    found: list[Declared] = []
    for node in walk(root):
        if phpunit and node.type == "method_declaration" and is_php_test_method(node):
            name = node.child_by_field_name("name")
            if name is not None:
                found.append(Declared(name.start_point[0] + 1, node_text(name), node_text(node)))
        elif node.type == "function_call_expression":
            callee = node.child_by_field_name("function")
            if callee is not None and node_text(callee) in TEST_CALLEES:
                found.extend(labelled(node))
    return found


def js_callee_is_test(callee) -> bool:
    """`it` / `test`、`it.only` / `test.skip`、`it.each([...])` の形か。"""
    if callee is None:
        return False
    if callee.type == "identifier":
        return node_text(callee) in TEST_CALLEES
    if callee.type == "member_expression":
        target = callee.child_by_field_name("object")
        return (
            target is not None and target.type == "identifier" and node_text(target) in TEST_CALLEES
        )
    if callee.type == "call_expression":
        return js_callee_is_test(callee.child_by_field_name("function"))
    return False


def js_tests(root) -> list[Declared]:
    found: list[Declared] = []
    for node in walk(root):
        if node.type == "call_expression" and js_callee_is_test(
            node.child_by_field_name("function")
        ):
            found.extend(labelled(node))
    return found
