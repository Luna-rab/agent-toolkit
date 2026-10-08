"""雛形と本文のマーカー（`<!-- autodev:<名前> -->`）を埋める。

`string.Template` は使わない。通すと `$$` が `$` になり、`${tasks}` のような文字列が消える。マーカーの規則:

- 1 回の走査で置き換え、差した中身は読み直さない。中身に入ったマーカーの文字列・`\\1`・`$$` は
  そのまま残る
- 置き換えるのは、1 行に単独で置いたマーカーだけ（行の前後の空白は許す）。文の中やインラインコードで
  マーカーを説明している所は置き換えない。同じマーカーが 2 回あれば両方
- 渡していないマーカーは残す。マーカーが無い本文には足さない
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from .prompts.assets import package_root

_MARKER = re.compile(r"^[ \t]*<!-- autodev:([a-z][a-z-]*) -->[ \t]*$", re.MULTILINE)


def fill(body: str, values: Mapping[str, str]) -> str:
    def replace(found: re.Match[str]) -> str:
        return values.get(found.group(1), found.group(0))

    return _MARKER.sub(replace, body)


def templates_dir() -> Path:
    """パッケージの `templates/`。"""
    return package_root() / "templates"


def template(name: str) -> str:
    return (templates_dir() / f"{name}.md").read_text(encoding="utf-8")
