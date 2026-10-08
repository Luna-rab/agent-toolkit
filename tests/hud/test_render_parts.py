"""行の部品（`hud/render/parts.py`）とタスクリストの描き方（`hud/render/tasklist.py`）。"""

from __future__ import annotations

import datetime as dt

import pytest
from hud_samples import quiet, task
from rich.text import Text

from hud.core import headline
from hud.core.pipeline import Mark, Step
from hud.core.stagelist import StageItem
from hud.core.tasklist import Summary
from hud.render import detail, navigator, parts, tasklist


@pytest.mark.parametrize(
    ("pct", "mark", "want"),
    [(50, None, "━━──"), (10, None, "╾───"), (100, None, "━━━━"), (50, 75, "━━─┃"), (0, 0, "┃───")],
)
def test_棒は罫線で0_5マスまで刻む(pct, mark, want):
    assert parts.bar(pct, 4, mark).plain == want


@pytest.mark.parametrize(("pct", "rgb"), [(0, "#a6e3a1"), (50, "#f9e2af"), (100, "#f38ba8")])
def test_使用率の色は緑から黄を経て赤へ変わる(pct, rgb):
    color = parts.pct_color(pct).color
    assert color is not None and color.get_truecolor().hex == rgb


def test_幅に収まらなければkeepの小さい部品から落とす():
    row = [
        parts.Part(Text("aaaa"), keep=2),
        parts.Part(Text("bb"), keep=0),
        parts.Part(Text("cc"), keep=1),
    ]
    assert parts.fit(row, 20).plain == "aaaa   bb   cc"
    assert parts.fit(row, 9).plain == "aaaa   cc"
    assert parts.fit(row, 3).plain == "aaaa"


def test_段の並びを記号つきで描く():
    steps = [
        Step("…", None, Mark.ELIDED),
        Step("実装", None, Mark.DONE),
        Step("リベース", None, Mark.SKIPPED),
        Step("ジャッジ", 2, Mark.CURRENT),
        Step("完了チェック", None, Mark.NEXT),
    ]
    assert (
        tasklist.pipeline(steps).plain == "… › 実装 ✔ › リベース – › ジャッジ r2 ◼ › 完了チェック"
    )


def test_タスク行は右に続くものがあるときだけ件名の幅をそろえる():
    running = tasklist.task_row(
        task(id="task2", title="範囲", status="running"), [Step("実装", None, Mark.CURRENT)]
    )
    pending = tasklist.task_row(task(id="task3", title="CLI", status="pending"), [])
    assert running.plain == "  ◼ task2 範囲" + " " * 18 + "  実装 ◼"
    assert pending.plain == "  ◻ task3 CLI"


def test_エスカレーション中のタスクは種類を出す():
    escalated = task(
        id="task4",
        title="移行",
        status="escalated",
        escalations=[
            {"id": "task4#3", "kind": "stall", "origin": None, "reason": "", "question": None}
        ],
    )
    assert tasklist.task_row(escalated, []).plain.endswith("  stall")


def test_飛ばした段は飛ばしたと出す():
    item = StageItem("step:1", "リベース", Mark.SKIPPED, ())
    text = Text()
    for part in detail.stage_detail(item, dt.datetime.now().astimezone()).renderables:
        text.append_text(part if isinstance(part, Text) else Text(str(part)))
    assert "飛ばした" in text.plain and "まだ走っていない" not in text.plain


def detail_lines(st: dict, now: dt.datetime) -> list[str]:
    group = detail.run_detail(headline.build(st, now), st, now)
    return [r.plain if isinstance(r, Text) else "" for r in group.renderables]


def test_ランの詳細とリストの行にdriverの止まり方とパニックの原因を出す():
    now = dt.datetime.now().astimezone()
    stopped = quiet()
    stopped["run"].update(driver_stopped=True, live_children=[4242])
    head = headline.build(stopped, now)
    lines = detail_lines(stopped, now)
    assert lines[2] == (
        "driver が止まっている（run --name add-cache で呼び直す）"
        " · 前の driver の子が生きている pid 4242"
    )
    assert navigator.run_row(head).plain == (
        "  add-cache  実行中 · driver 停止 · 子が残っている  1/4"
    )

    panicked = quiet()
    panicked["run"].update(phase="panicked", driver_running=False, panic_cause="利用枠の上限" * 20)
    # 詳細には切らずに全文を出す
    assert detail_lines(panicked, now)[2] == f"パニックの原因: {'利用枠の上限' * 20}"


def test_まとめた行():
    assert tasklist.summary_row(Summary("stacked", 3)).plain == "  ✔ 3 件完了"
    assert tasklist.summary_row(Summary("pending", 2)).plain == "  ◻ 他 2 件"
