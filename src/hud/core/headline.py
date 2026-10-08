"""ラン 1 つの見出し。いま何が走っているか、回答を待っているか。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from enum import Enum

from hud.core.pipeline import short_name
from hud.core.runs import (
    Running,
    awaiting,
    driver_stopped,
    live_children,
    name_of,
    overview_pr,
    panic_cause,
    phase,
    phase_label,
    questions,
    run_of,
    running,
    tasks,
)


class State(Enum):
    #: 呼び直すまで進まない（`run.phase` が panicked）
    PANICKED = "panicked"
    #: 実行が走っている
    RUNNING = "running"
    #: 走っている実行が無い
    QUIET = "quiet"


@dataclass(frozen=True)
class Headline:
    run_name: str
    state: State
    #: 何をしているか（「task2 レビュー r1」「計画中」）
    doing: str
    #: `run.phase` の表示名
    phase: str
    #: 回答を待っている質問の id。ほかのタスクが走っていても入る
    waiting: tuple[str, ...] = ()
    #: いちばん新しく始めた実行からの秒数
    elapsed: float | None = None
    #: ターン数と直前のツール。実行が 1 つのときだけ入れる（並んでいるとどちらの数か分からない）
    turns: int = 0
    tool: str = ""
    overview_pr: int | None = None
    stacked: int = 0
    total: int = 0
    #: エスカレーション中のタスクの数
    escalated: int = 0
    #: driver が走っているはずのフェーズなのに走っていない（`run.driver_stopped`）
    stopped: bool = False
    #: 前の driver が残した、まだ生きている子の pid
    leftovers: tuple[int, ...] = ()
    panic_cause: str = ""


def build(st: dict, now: dt.datetime) -> Headline:
    run = run_of(st)
    waiting = tuple(str(q.get("id") or "?") for q in questions(st)) if awaiting(st) else ()
    base = Headline(
        run_name=name_of(st),
        state=State.QUIET,
        doing=phase_label(st),
        phase=phase_label(st),
        waiting=waiting,
        overview_pr=overview_pr(st),
        stacked=run["stacked_tasks"],
        total=run["stack_target_tasks"],
        escalated=sum(1 for t in tasks(st) if t.get("status") == "escalated"),
        stopped=driver_stopped(st),
        leftovers=live_children(st),
        panic_cause=panic_cause(st),
    )
    if phase(st) == "panicked":
        return replace(base, state=State.PANICKED)
    live = running(st, now)
    if not live:
        return base
    seconds = [r.seconds for r in live if r.seconds is not None]
    single = len(live) == 1
    return replace(
        base,
        state=State.RUNNING,
        doing=doing(live),
        elapsed=min(seconds) if seconds else None,
        turns=live[0].turns if single else 0,
        tool=live[0].tool if single else "",
    )


def doing(live: list[Running]) -> str:
    """タスクごとに「task2 レビュー r1」。同じタスクで並んで走るステージは 1 つにまとめる。"""
    by_task: dict[str, list[Running]] = {}
    for r in live:
        by_task.setdefault(r.task, []).append(r)
    parts = []
    for task, items in by_task.items():
        label = "+".join(dict.fromkeys(short_name(r.stage) for r in items))
        parts.append(f"{task} {label} r{max(r.round for r in items)}")
    return " · ".join(parts)
