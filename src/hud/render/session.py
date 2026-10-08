"""statusline の左の 4 行。

    Opus 5.5 · high   ctx ━━━━━───── 47%   $3.21
    dotfiles   feature/x +2 ~1 ?3
    5h ━━━━━━━━━━━━━━━━━━━━━━━━┃━━━━╾─────────── 72% ▲12 1h47m
    7d ━━━━━━━━━━━━╾───────────────────┃─────── 31% ▼4 2d05h
    mo ━━━━━━━━┃──────────────────────────────── 12% $96.00/$800 ▼8 27d03h

1 行目は使っている量、2 行目はどこで、3 行目からは利用枠。5h・7d は Pro / Max、
mo（月次のクレジット）はクレジットが有効なアカウントでだけ出る。
"""

from __future__ import annotations

from rich.text import Text

from hud.core.git import GitStatus
from hud.core.limits import Limit, until
from hud.core.session import Session
from hud.render.parts import Part, bar, pct_color
from hud.render.theme import ACCENT, BLUE, BOLD, DIM, GREEN, MAUVE, RED, YELLOW

CONTEXT_BAR_WIDTH = 10
LIMIT_BAR_WIDTH = 40


def rows(session: Session, git: GitStatus | None) -> list[list[Part]]:
    model = Text(session.model, style=ACCENT)
    if session.effort:
        model.append(f" · {session.effort}", style=MAUVE)
    ctx = Text("ctx ", style=DIM)
    ctx.append_text(bar(session.context_pct, CONTEXT_BAR_WIDTH))
    ctx.append(f" {session.context_pct:.0f}%", style=pct_color(session.context_pct))
    usage = [
        Part(model, keep=3),
        Part(ctx, keep=2),
        Part(Text(f"${session.cost:.2f}", style=DIM), keep=1),
    ]
    where = [
        Part(Text(session.repo, style=BOLD), keep=3),
        Part(git_text(git), keep=2),
        Part(
            Text(f"worktree {session.worktree}", style=BLUE) if session.worktree else Text(), keep=1
        ),
    ]
    return [usage, where] + [[Part(limit_gauge(limit))] for limit in session.limits]


def git_text(git: GitStatus | None) -> Text:
    if git is None:
        return Text()
    text = Text(git.branch, style=GREEN)
    for count, mark, style in (
        (git.staged, "+", GREEN),
        (git.modified, "~", YELLOW),
        (git.untracked, "?", BLUE),
    ):
        if count:
            text.append(f" {mark}{count}", style=style)
    return text


def limit_gauge(limit: Limit) -> Text:
    """利用枠の棒。目盛り `┃` は窓の時間が過ぎた位置で、棒がこれを越えていれば使いすぎ。

    `▲n` は窓の経過割合より n ポイント多く使っている（このままでは窓の途中で尽きる）、`▼n` は余裕。
    """
    text = Text(f"{limit.label} ", style=DIM)
    text.append_text(bar(limit.used, LIMIT_BAR_WIDTH, limit.elapsed_pct))
    text.append(f" {limit.used:.0f}%", style=pct_color(limit.used))
    if limit.money is not None:
        spent, cap = limit.money
        text.append(f" ${spent:.2f}/${cap:.0f}", style=DIM)
    pace = limit.pace
    if pace is not None and pace >= 1:
        text.append(f" ▲{pace}", style=RED)
    elif pace is not None and pace <= -1:
        text.append(f" ▼{-pace}", style=GREEN)
    if limit.remaining is not None:
        text.append(f" {until(limit.remaining)}", style=DIM)
    return text
