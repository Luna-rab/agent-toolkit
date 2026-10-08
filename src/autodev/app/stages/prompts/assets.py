"""指示書（`contracts/`）とスキーマ（`schemas/`）の置き場を引き、指示書の「入力」の表を読む。"""

from __future__ import annotations

import re
import shutil
import sys
from functools import cache
from pathlib import Path

import autodev

from ....domain.value_objects.stage_kind import StageKind


def package_root() -> Path:
    """`autodev` パッケージのディレクトリ。`contracts/`・`schemas/`・`templates/` がここにある。"""
    return Path(autodev.__file__).resolve().parent


def launcher() -> str:
    """`autodev` コマンドの絶対パス。`sys.executable` の隣を先に見て、無ければ PATH から探す。

    指示書の `<autodev>` は絶対パスなので、見つからないときは名前だけを返さずに落とす。
    """
    beside = Path(sys.executable).parent / "autodev"
    if beside.is_file():
        return str(beside)
    found = shutil.which("autodev")
    if found is None:
        raise FileNotFoundError("autodev コマンドが見つからない（`uv tool install` で入れる）")
    return str(Path(found).absolute())


def asset_name(kind: StageKind) -> str:
    """ステージの種類の名前から、指示書とスキーマのファイル名（拡張子を除く）。`TestGen` → `test-gen`。"""
    return re.sub(r"(?<!^)(?=[A-Z])", "-", kind.value).lower()


def contract_path(name: str) -> Path:
    return package_root() / "contracts" / f"{name}.md"


def schema_text(name: str) -> str:
    """`claude --json-schema` に渡す本文（パスではない）。"""
    return (package_root() / "schemas" / f"{name}.json").read_text(encoding="utf-8")


#: 指示書のプレースホルダ。`<!--`・`<<<<<<<` は含めない（test_contracts と同じ形）
_PLACEHOLDER = re.compile(r"(?<![A-Za-z0-9])<([^<>\s!/`\-=][^<>\n`]*)>")


@cache
def contract_inputs(name: str) -> tuple[str, ...]:
    """指示書の `## 入力` の表の 1 列目に書いたプレースホルダ（表の順）。"""
    text = contract_path(name).read_text(encoding="utf-8")
    section = text.split("## 入力", 1)[1].split("\n## ", 1)[0]
    found: list[str] = []
    for line in section.splitlines():
        if line.startswith("| `<"):
            found += [m for m in _PLACEHOLDER.findall(line.split("|")[1]) if m not in found]
    return tuple(found)
