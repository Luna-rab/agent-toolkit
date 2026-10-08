"""hud の層の規則を機械で守る。

**この分かれ方は import の向きだけで保たれている。** `core` が 1 行 `subprocess` を import すると、
ステージの並びや窓切りを git とファイル無しでは試せなくなる。そのとき他の検査は全部通るので、試せなく
なったことは誰にも見えない。

    app → ports, render, core
    render → core
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conftest import HUD_PACKAGE

PACKAGE = HUD_PACKAGE
FORBIDDEN_LAYERS = {
    "core": ("ports", "render", "app"),
    "ports": ("core", "render", "app"),
    "render": ("ports", "app"),
    "app": (),
}
#: 層ごとに import してはいけない外のモジュール。`core` は I/O も描画もしない、`ports` は描かない、
#: `render` は I/O をしない
FORBIDDEN_MODULES = {
    "core": ("os", "subprocess", "shutil", "glob", "socket", "rich", "textual"),
    "ports": ("rich", "textual"),
    "render": ("os", "subprocess", "shutil", "glob", "socket", "textual"),
    "app": (),
}
MAY_OPEN = ("ports",)

MODULES = sorted(p for p in PACKAGE.rglob("*.py") if p.parent != PACKAGE)


def layer_of(path: Path) -> str:
    return path.relative_to(PACKAGE).parts[0]


def imports(tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def test_層の表がディレクトリと一致している():
    assert {layer_of(p) for p in MODULES} == set(FORBIDDEN_LAYERS)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(PACKAGE)))
def test_層の向きを守る(path: Path):
    layer = layer_of(path)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in imports(tree):
        top, *rest = name.split(".")
        if top == "hud":
            assert not rest or rest[0] not in FORBIDDEN_LAYERS[layer], (
                f"{layer} が {name} を import している"
            )
        else:
            assert top not in FORBIDDEN_MODULES[layer], f"{layer} が {name} を import している"
    if layer not in MAY_OPEN:
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "open"
        ]
        assert not calls, f"{layer} が open() を呼んでいる"
