"""autodev-watch の左ペイン。ラン・タスク・ステージのリストの行と、今の位置。"""

from __future__ import annotations

from rich.text import Text

from hud.core.headline import Headline, State
from hud.core.pipeline import Mark
from hud.core.runs import task_label
from hud.core.stagelist import StageItem
from hud.render.theme import (
    ACCENT,
    DIM,
    GREEN,
    RED,
    STATUS_LABEL,
    YELLOW,
    status_mark,
)

#: ステージの印。statusline の段の並びと同じ記号にする
STAGE_MARK = {
    Mark.DONE: ("✔", GREEN),
    Mark.SKIPPED: ("–", DIM),
    Mark.FAILED: ("✘", RED),
    Mark.CURRENT: ("◼", ACCENT),
    Mark.NEXT: ("◻", DIM),
}
#: 行の色を決める `run.phase` の表示名
PHASE_ROW_STYLE = {"パニック": RED, "完了": GREEN}


def breadcrumb(run_name: str | None, task_id: str | None) -> Text:
    """今の位置。ランのリストに居るときは「ラン」だけ。"""
    out = Text("ラン", style=DIM)
    if run_name:
        out.append(" › ", style=DIM).append(run_name, style=ACCENT)
    if task_id:
        out.append(" › ", style=DIM).append(task_id, style=ACCENT)
    return out


def run_row(head: Headline) -> Text:
    mark = "◼" if head.state is State.RUNNING else " "
    row = Text(f"{mark} ", style=ACCENT).append(head.run_name)
    row.append(f"  {head.phase}", style=PHASE_ROW_STYLE.get(head.phase, DIM))
    if head.stopped:
        row.append(" · driver 停止", style=RED)
    if head.leftovers:
        row.append(" · 子が残っている", style=RED)
    if head.waiting:
        row.append(" · 回答待ち", style=YELLOW)
    row.append(f"  {head.stacked}/{head.total}", style=DIM)
    return row


def broken_run_row(name: str) -> Text:
    return Text("✘ ", style=RED).append(name).append("  読めない", style=RED)


def task_row(task: dict) -> Text:
    status = str(task.get("status"))
    mark, mark_style, body_style = status_mark(status)
    row = Text(f"{mark} ", style=mark_style).append(f"{task.get('id', '?')} ", style=DIM)
    row.append(task_label(task), style=body_style)
    row.append(f"  {STATUS_LABEL.get(status, status)}", style=DIM)
    return row


def stage_row(item: StageItem) -> Text:
    mark, style = STAGE_MARK.get(item.mark, ("◻", DIM))
    row = Text(f"{mark} ", style=style)
    if item.note:
        row.append(f"{item.note} ", style=DIM)
    row.append(item.label, style=DIM if item.mark in (Mark.NEXT, Mark.SKIPPED) else "")
    return row
