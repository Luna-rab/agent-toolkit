"""Stop・SubagentStop の流れ。この turn に足した行をアダプタから受け取り、コメントとテストの
見直しを 1 回の出力にまとめる。

**2 周目（`stop_hook_active`）は黙って通す。** これを見ないと、残す判断をしたもので永久に止まる。
**何が起きてもエージェントの停止は妨げない。** 例外は標準エラーに 1 行出して終了コード 0 で抜ける。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

from turnreview.adapters.claude import transcript as claude
from turnreview.adapters.codex import transcript as codex
from turnreview.app import comments, testcases
from turnreview.core.report import notes_for
from turnreview.core.review import Pending, Review
from turnreview.render.review import render

#: `additionalContext` がこれを超えると Claude には先頭の 2KB しか渡らない（上限は 10KB、実測）。
#: 超えそうなら色を落とし、それでも超えるなら一覧を `path:line` だけにする。一覧を途中で切ると、
#: 切った分は見直されないまま報告済みになる
MAX_CONTEXT_CHARS = 9_000
FALLBACKS = ({"color": True}, {"color": False}, {"color": False, "compact": True})
DEFAULT_COLUMNS = 100
MAX_WIDTH = 120
#: `Stop hook feedback:` の欄は 2 桁下げて描かれ、右端で折り返されると枠が崩れる
INDENT = 4


def width() -> int:
    """描く幅。hook の子プロセスは端末に直結しないので `COLUMNS` を読む。"""
    try:
        columns = int(os.environ.get("COLUMNS") or 0) or DEFAULT_COLUMNS
    except ValueError:
        columns = DEFAULT_COLUMNS
    return max(40, min(columns, MAX_WIDTH) - INDENT)


def fitted(*reviews: Review, color: bool = True) -> str:
    """上限に収まる描き方のうち、いちばん見やすいもの。どれも収まらなければ最後の形で返す。

    見直しは空行 1 つで連結する。上限は連結した全体に当てる。
    """
    message = ""
    for options in FALLBACKS if color else FALLBACKS[1:]:
        message = "\n\n".join(render(review, width(), **options) for review in reviews)
        if len(message) <= MAX_CONTEXT_CHARS:
            break
    return message


#: エージェントごとの transcript の読み方と出力の形。どちらも `added_in_turn`・`hook_output`・`COLOR`（描画に色を使うか）を持つ
ADAPTERS = {"claude": claude, "codex": codex}
COMMENT_SKIP_ENV = "CLAUDE_SKIP_COMMENT_REVIEW"
TEST_SKIP_ENV = "CLAUDE_SKIP_TEST_REVIEW"


def reviews_of(agent: str, payload: dict) -> list[Pending]:
    """環境変数で止められていない見直しのうち、出すものがあるもの。"""
    wanted = [
        review
        for review, skip in ((comments.review, COMMENT_SKIP_ENV), (testcases.review, TEST_SKIP_ENV))
        if os.environ.get(skip) != "1"
    ]
    if not wanted:
        return []
    added, cwd = ADAPTERS[agent].added_in_turn(payload)
    session_id = str(payload.get("session_id") or "")
    found = (review(added, cwd, agent, session_id) for review in wanted)
    return [review for review in found if review is not None]


def run(agent: str, payload: dict, emit: Callable[[dict], None] | None = None) -> dict | None:
    """見直しがあれば、エージェントが受け取る形の出力を返す。

    `emit` を渡すと、出力を書き出してから報告済みにする。書き出しに失敗したら保存しない。

    例外は標準エラーに 1 行出して None にする。
    """
    try:
        if payload.get("stop_hook_active"):
            return None
        found = reviews_of(agent, payload)
        if not found:
            return None
        adapter = ADAPTERS[agent]
        reviews = [review for review, _ in found]
        reviews[-1] = reviews[-1]._replace(notes=notes_for(payload))
        output = adapter.hook_output(payload, fitted(*reviews, color=adapter.COLOR))
        if emit is not None:
            emit(output)
        for _, save in found:
            save()
        return output
    except Exception as exc:
        print(f"turnreview: skipped ({exc!r})", file=sys.stderr)
        return None
