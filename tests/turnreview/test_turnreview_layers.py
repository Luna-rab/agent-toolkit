"""turnreview の層の規則を機械で守る。

**この分かれ方は import の向きだけで保たれている。** `core` が 1 行 `tempfile` や
`tree_sitter_language_pack` を import すると、見直しの判定を一時ディレクトリや文法無しでは
試せなくなる。そのとき他の検査は全部通るので、試せなくなったことは誰にも見えない。

    app → adapters, ports, syntax, render, core
    adapters → ports, core
    syntax → core
    render → core
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import TURNREVIEW_PACKAGE

PACKAGE = TURNREVIEW_PACKAGE
FORBIDDEN_LAYERS = {
    "core": ("adapters", "syntax", "render", "ports", "app"),
    "adapters": ("syntax", "render", "app"),
    "syntax": ("adapters", "render", "ports", "app"),
    "render": ("adapters", "syntax", "ports", "app"),
    "ports": ("adapters", "core", "syntax", "render", "app"),
    "app": (),
}
IO_MODULES = ("os", "sys", "subprocess", "shutil", "glob", "socket", "tempfile")
PARSERS = ("ast", "tree_sitter_language_pack")
#: 層ごとに import してはいけない外のモジュール。`core` は I/O も構文解析も描画もしない、
#: `syntax` と `render` は I/O をしない、rich を使うのは `render` だけ
FORBIDDEN_MODULES = {
    "core": (*IO_MODULES, *PARSERS, "rich"),
    "adapters": (*PARSERS, "rich"),
    "syntax": (*IO_MODULES, "rich"),
    "render": (*IO_MODULES, *PARSERS),
    "ports": (*PARSERS, "rich"),
    "app": (),
}
PATH_IO = frozenset(
    {"open", "read_text", "read_bytes", "write_text", "write_bytes", "exists", "is_file"}
    | {"is_dir", "mkdir", "iterdir", "glob", "rglob", "stat", "unlink", "touch", "resolve"}
)
MAY_TOUCH_FILES = ("adapters", "ports", "app")

MODULES = sorted(p for p in PACKAGE.rglob("*.py") if p.parent != PACKAGE)
ENTRIES = ("turnreview.app.comments", "turnreview.app.testcases")


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
        if top == "turnreview":
            assert not rest or rest[0] not in FORBIDDEN_LAYERS[layer], (
                f"{layer} が {name} を import している"
            )
        else:
            assert top not in FORBIDDEN_MODULES[layer], f"{layer} が {name} を import している"
    if layer not in MAY_TOUCH_FILES:
        touched = [
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr in PATH_IO)
                or (isinstance(node.func, ast.Name) and node.func.id == "open")
            )
        ]
        assert not touched, f"{layer} がファイルに触っている: {touched}"


@pytest.mark.parametrize("entry", ENTRIES)
def test_入口のモジュールを依存だけで読み込める(entry: str):
    """入口は `sys.path` を足さずに import できる。依存が pyproject.toml に宣言されていないと落ちる。"""
    done = subprocess.run(
        [sys.executable, "-I", "-c", f"import {entry}"],
        capture_output=True,
        text=True,
        check=False,
        cwd="/",
    )
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
