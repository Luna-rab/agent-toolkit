"""autodev の `status --json` を呼ぶ。**ランディレクトリの中は読まない**。

ここは呼んだ結果を返すだけで、終了コードの
意味も JSON の中身も解釈しない（`core/runs.py` が読む）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

#: statusline は 2 秒ごとに描き直すので、それより長く待たない
TIMEOUT = 2
#: 入口のスクリプトを差し替える。検査と、手で偽の status を渡して確かめるときに使う
ENTRY_ENV = "AUTODEV_ENTRY"


@dataclass(frozen=True)
class Reply:
    #: 終了コード。起動できなかった・時間切れなら None
    code: int | None
    #: 標準出力を JSON として読んだもの。読めなければ None
    data: Any
    #: 標準エラーの最後の行か、起動できなかった理由
    message: str


def command() -> list[str]:
    """autodev を起こすコマンド。`AUTODEV_ENTRY` があればそのスクリプト、無ければ `python -I -m autodev`。"""
    override = os.environ.get(ENTRY_ENV)
    if override:
        return [sys.executable, os.path.abspath(override)]
    return [sys.executable, "-I", "-m", "autodev"]


def call(args: list[str]) -> Reply:
    # 今の Python で起こす（PATH の python3 を探さない）
    try:
        out = subprocess.run(
            [*command(), *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Reply(None, None, f"{TIMEOUT} 秒で返らなかった")
    except OSError as error:
        return Reply(None, None, str(error))
    try:
        data = json.loads(out.stdout) if out.stdout.strip() else None
    except json.JSONDecodeError:
        data = None
    lines = out.stderr.strip().splitlines()
    return Reply(out.returncode, data, lines[-1] if lines else "")


def statuses() -> Reply:
    """全ランの status（配列）。statusline はこれを 1 回だけ呼んで描く。"""
    return call(["status", "--json"])


def status(name: str) -> Reply:
    """1 つのランの status。ランが無ければ終了コード 5。"""
    return call(["status", "--json", "--name", name])
