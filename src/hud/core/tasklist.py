"""statusline に出すタスクの行。多いときは、今のタスクの前後だけに窓を切る。

積んだタスクは 1 行にまとめ、未着手は次の `PENDING_ROWS` 本だけ出す。ほかの状態は必ず出す。
高さを一定に保つためで、全部を見るのは autodev-watch の役目である。
"""

from __future__ import annotations

from dataclasses import dataclass

from hud.core.runs import tasks

#: タスクがこれより多いと、今のタスクの前後だけに窓を切る
MAX_TASK_ROWS = 5
#: 窓を切ったときに出す未着手の本数
PENDING_ROWS = 2
#: 計画タスクと git 管理タスクは、この状態のときだけ statusline に出す
SIDE_TASK_SHOWN = ("running", "escalated")


@dataclass(frozen=True)
class Summary:
    """まとめた行。`status` が stacked なら「N 件完了」、pending なら「他 N 件」。"""

    status: str
    count: int


def listed(st: dict) -> list[dict]:
    """statusline に並べるタスク。実装タスクはすべて、計画タスクと git 管理タスクは動いているときだけ。"""
    return [
        t
        for t in tasks(st)
        if t.get("kind") == "implementation" or t.get("status") in SIDE_TASK_SHOWN
    ]


def visible(items: list[dict]) -> list[dict | Summary]:
    if len(items) <= MAX_TASK_ROWS:
        return list(items)
    rows: list[dict | Summary] = []
    stacked = sum(1 for t in items if t.get("status") == "stacked")
    if stacked:
        rows.append(Summary("stacked", stacked))
    shown = hidden = 0
    for task in items:
        status = task.get("status")
        if status == "stacked":
            continue
        if status == "pending":
            if shown >= PENDING_ROWS:
                hidden += 1
                continue
            shown += 1
        rows.append(task)
    if hidden:
        rows.append(Summary("pending", hidden))
    return rows
