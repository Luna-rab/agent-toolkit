"""フローの段の並び（`hud/core/pipeline.py`）とタスクの窓切り（`hud/core/tasklist.py`）。"""

from __future__ import annotations

from hud_samples import SAMPLE_TASK, status

from hud.core import pipeline, tasklist
from hud.core.pipeline import Mark


def plain(steps: list[pipeline.Step]) -> list[tuple[str, str]]:
    return [(s.label, s.mark.value) for s in steps]


def flow(*steps: dict) -> dict:
    return {"id": "task1", "flow": {"version": 1, "steps": list(steps)}}


def test_今の合成ステージは中で走っているステージとラウンドを出す():
    assert plain(pipeline.steps(SAMPLE_TASK)) == [
        ("テスト作成", "done"),
        ("ジャッジ r2", "current"),
        ("完了チェック", "next"),
    ]


def test_1ラウンド目はラウンドを添えない():
    task = flow({"stage": "ReviewLoop", "state": "current", "inner": "Review", "round": 1})
    assert plain(pipeline.steps(task)) == [("通常レビュー", "current")]


def test_飛ばした段を印で分け古い済んだ段は省く():
    task = flow(
        {"stage": "TestGen", "state": "done"},
        {"stage": "ConfirmRed", "state": "done"},
        {"stage": "Impl", "state": "done"},
        {"stage": "Rebase", "state": "skipped"},
        {"stage": "Gate", "state": "current"},
        {"stage": "WritePrBody", "state": "pending"},
    )
    got = pipeline.steps(task)
    assert [s.label for s in got] == ["…", "実装", "リベース", "完了チェック", "PR 本文"]
    assert [s.mark for s in got][:3] == [Mark.ELIDED, Mark.DONE, Mark.SKIPPED]


def test_フローが無ければ段も無い():
    assert pipeline.steps({"id": "task3", "flow": None}) == []


def test_知らないステージはそのままの名前で出す():
    task = flow({"stage": "NewStage", "state": "pending"})
    assert plain(pipeline.steps(task)) == [("NewStage", "next")]


def test_statuslineには実装タスクと動いている計画タスク_git管理タスクだけを並べる():
    st = status()
    assert [t["id"] for t in tasklist.listed(st)] == ["task1", "task2", "task3", "task4"]
    st["tasks"][0]["status"] = "running"
    assert tasklist.listed(st)[0]["id"] == "planning"


def test_タスクが多いと今のタスクの前後に窓を切る():
    statuses = [
        "stacked",
        "stacked",
        "escalated",
        "running",
        "pending",
        "pending",
        "pending",
        "pending",
    ]
    tasks = [{"id": f"task{i}", "status": s} for i, s in enumerate(statuses, 1)]
    got = tasklist.visible(tasks)
    assert got[0] == tasklist.Summary("stacked", 2)
    assert [t["id"] for t in got[1:5] if isinstance(t, dict)] == [
        "task3",
        "task4",
        "task5",
        "task6",
    ]
    assert got[5] == tasklist.Summary("pending", 2)


def test_タスクが少なければ全部出す():
    tasks = [{"id": f"task{i}", "status": "pending"} for i in range(5)]
    assert tasklist.visible(tasks) == tasks
