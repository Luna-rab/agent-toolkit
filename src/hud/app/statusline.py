"""statusline を 1 回描く。Claude Code が標準入力に JSON を渡し、標準出力を受け取って表示する。

**Claude Code は標準出力を受け取るだけで、端末に直結しない。** 常駐・キー入力・アニメーションは
できず、端末の幅は `tput` では取れないので `COLUMNS` を読む。**端末の幅を変えても描き直されない**
ので、`settings.json` の `refreshInterval` で定期的に描き直させて幅の変化に追いつかせる。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import sys
import time

from rich.console import Console

from hud.core import credit, git, session
from hud.ports import git as git_port
from hud.ports import usage
from hud.render import session as session_view
from hud.render.parts import fit

DEFAULT_COLUMNS = 120
#: 各行の頭に置く色のリセット。Claude Code は行ごとに前後の空白を削ってから表示する
LINE_HEAD = "\x1b[0m"


def columns() -> int:
    try:
        return int(os.environ.get("COLUMNS") or 0) or DEFAULT_COLUMNS
    except ValueError:
        return DEFAULT_COLUMNS


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        data = {}
    current = session.parse(data, time.time())
    now = dt.datetime.now().astimezone()
    month = credit.monthly(usage.usage(now.timestamp()), now)
    if month is not None:
        current = dataclasses.replace(current, limits=(*current.limits, month))
    output = git_port.status(current.cwd)
    rows = session_view.rows(current, git.parse(output) if output is not None else None)
    # 標準出力は端末ではないので、色を付けるよう明示する。折り返しは Claude Code に任せない
    console = Console(
        force_terminal=True,
        color_system="truecolor",
        width=10_000,
        soft_wrap=True,
        highlight=False,
        markup=False,
        emoji=False,
    )
    width = columns()
    for row in rows:
        console.file.write(LINE_HEAD)
        console.print(fit(row, width))
    return 0
