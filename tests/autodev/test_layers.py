"""ドメイン層が I/O をせず、ドメイン層の外に依らないこと。

`domain/` の中のファイルを構文木で読み、import と名前の参照を見る。import は許可の一覧で見る。
禁止の一覧にすると、載せ忘れた標準ライブラリ（`importlib`・`builtins` など）から I/O に届く。
ドメインの規則を I/O 無しで確かめられるのは、この線が守られている間だけである。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conftest import SRC_ROOT

DOMAIN = SRC_ROOT / "autodev" / "domain"
DOMAIN_PACKAGE = "autodev.domain"

#: ドメイン層が import してよい標準ライブラリ。どれもファイル・プロセス・ネットワーク・時刻・
#: 乱数・環境変数に触らない。datetime は今の時刻を読めるので入れない（ドメインは時刻をコマンドの
#: 中身として受け取る）
ALLOWED_MODULES = frozenset(
    {
        "__future__",
        "abc",
        "collections",
        "collections.abc",
        "dataclasses",
        "enum",
        "functools",
        "itertools",
        "json",
        "re",
        "types",
        "typing",
    }
)
#: 参照するだけで外に届く組み込みの名前（呼ばずに変数へ入れても抜け道になる）
FORBIDDEN_NAMES = frozenset(
    {
        "open",
        "input",
        "print",
        "exec",
        "eval",
        "compile",
        "breakpoint",
        "__import__",
        "__builtins__",
    }
)
#: 属性としても取らせない名前（`builtins.open`・`io.open`・`x.__import__` など）。`compile` は
#: `re.compile` と同じ名前なので、属性では見ない（組み込みの compile は名前の参照で見つける）
FORBIDDEN_ATTRIBUTES = frozenset({"open", "exec", "eval", "__import__", "__builtins__"})


def module_of(path: Path) -> str:
    return ".".join(path.relative_to(SRC_ROOT).with_suffix("").parts)


def resolve(module: str, node: ast.ImportFrom) -> str:
    """相対 import を、絶対の名前にする。パッケージの外まで上ったら空の文字列にする。"""
    if node.level == 0:
        return node.module or ""
    package = module.split(".")[:-1]
    if node.level - 1 >= len(package):
        return ""
    base = package[: len(package) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def is_allowed(name: str) -> bool:
    if name == DOMAIN_PACKAGE or name.startswith(f"{DOMAIN_PACKAGE}."):
        return True
    return name in ALLOWED_MODULES


def violations(source: str, module: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        imported: list[str] = []
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported = [resolve(module, node)]
        for name in imported:
            if not is_allowed(name):
                found.append(
                    f"{line}: 許していない {name or '（パッケージの外）'} を import している"
                )
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"{line}: 組み込みの {node.id} を使っている")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            found.append(f"{line}: 属性の {node.attr} を使っている")
        # getattr(x, "open") のように、名前を文字列で渡して取る
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            for arg in node.args[1:2]:
                if isinstance(arg, ast.Constant) and arg.value in FORBIDDEN_NAMES:
                    found.append(f"{line}: {node.func.id} で {arg.value} を取っている")
    return found


DOMAIN_FILES = sorted(DOMAIN.rglob("*.py"))


def test_ドメイン層のファイルがある():
    names = {path.relative_to(DOMAIN).as_posix() for path in DOMAIN_FILES}
    assert {
        "value_objects/base.py",
        "events/base.py",
        "commands/base.py",
        "stages/catalog.py",
        "flow/flow.py",
        "aggregates/base.py",
    } <= names


@pytest.mark.parametrize(
    "path", DOMAIN_FILES, ids=[p.relative_to(DOMAIN).as_posix() for p in DOMAIN_FILES]
)
def test_ドメイン層はIOをせず外に依らない(path: Path):
    assert violations(path.read_text(encoding="utf-8"), module_of(path)) == []


DOMAIN_MODULE = "autodev.domain.codec"


@pytest.mark.parametrize(
    "source",
    [
        # I/O の標準ライブラリ
        "import os",
        "import os.path",
        "from subprocess import run",
        "import sqlite3",
        "from pathlib import Path",
        "import time",
        "from random import choice",
        "import socket",
        "import datetime",
        "from datetime import datetime",
        # 禁止の一覧なら載せ忘れる抜け道
        "import importlib\nimportlib.import_module('os')",
        "from importlib import import_module",
        "import builtins\nbuiltins.open('x')",
        "from builtins import open",
        "import ctypes",
        "import pickle",
        "import collections.foo",
        # 組み込みから I/O に届く
        "open('x')",
        "f = open\nf('x')",
        "__import__('os')",
        "getattr(__builtins__, 'open')('x')",
        "__builtins__['open']('x')",
        "getattr(object, 'open')",
        "x.open('y')",
        "exec('import os')",
        "eval('1')",
        "compile('1', 'x', 'eval')",
        # ドメイン層の外
        "from ..app.driving import mainloop",
        "from .. import app",
        "from ..infra.store.eventstore import EventStore",
        "from .... import x",
        "from autodev.adapters.github import git",
        "from autodev import cli",
        "import autodev",
        "import autodev.domainx",
        "from autodev.domainx import y",
    ],
)
def test_検査はIOと外への依存を見つける(source: str):
    assert violations(source, DOMAIN_MODULE) != []


@pytest.mark.parametrize(
    "source",
    [
        "from . import codec",
        "from .value_objects.task_id import TaskId",
        "from autodev.domain.flow import Flow",
        "import autodev.domain",
        "import re\nimport json\nfrom dataclasses import dataclass",
        "from collections.abc import Mapping\nimport collections.abc",
        "from typing import Any\nfrom functools import cache\nimport itertools",
        "getattr(self, 'value')",
    ],
)
def test_検査はドメインの中と許したライブラリを通す(source: str):
    assert violations(source, DOMAIN_MODULE) == []


def test_サブパッケージの相対importも名前を解く():
    node = ast.parse("from ..value_objects.task_id import TaskId").body[0]
    assert isinstance(node, ast.ImportFrom)
    assert (
        resolve("autodev.domain.services.scheduler", node) == "autodev.domain.value_objects.task_id"
    )
