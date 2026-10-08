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
from rich.text import Text

from hud.core import credit, git, headline, pipeline, runs, session, tasklist
from hud.ports import autodev, usage
from hud.ports import git as git_port
from hud.render import layout
from hud.render import session as session_view
from hud.render import tasklist as tasklist_view

DEFAULT_COLUMNS = 120
#: 各行の頭に置く色のリセット。Claude Code は行ごとに前後の空白を削ってから表示するので、
#: 空白で始まる行（左の行が無い段のタスクリスト、タスクの字下げ）は左に詰まってしまう
LINE_HEAD = "\x1b[0m"


def run_block(st: dict, now: dt.datetime) -> list[Text]:
    """ラン 1 つの見出しとタスクリスト。"""
    lines = [tasklist_view.headline(headline.build(st, now))]
    for row in tasklist.visible(tasklist.listed(st)):
        if isinstance(row, tasklist.Summary):
            lines.append(tasklist_view.summary_row(row))
        else:
            lines.append(tasklist_view.task_row(row, pipeline.steps(row)))
    return lines


def autodev_block(now: dt.datetime) -> list[Text]:
    """`status --json` を 1 回だけ呼んで、出すランのタスクリストを並べる。

    欄の形が想定と違って落ちても、statusline 全体を traceback で消さず、理由を 1 行で出す。
    """
    try:
        reply = autodev.statuses()
        found = runs.listing(reply.code, reply.data, reply.message)
        if found.error is not None:
            return [tasklist_view.failure_row(found.error)]
        return [line for st in found.runs if runs.shown(st, now) for line in run_block(st, now)]
    except Exception as error:
        return [tasklist_view.failure_row(f"{type(error).__name__}: {error}")]


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
    block = autodev_block(now)
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
    for line in layout.layout(rows, block, columns()):
        console.file.write(LINE_HEAD)
        console.print(line)
    return 0
