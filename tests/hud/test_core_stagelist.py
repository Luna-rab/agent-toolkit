"""autodev-watch の段のリスト（`hud/core/stagelist.py`）。"""

from __future__ import annotations

import copy

from hud_samples import SAMPLE_TASK, execution

from hud.core import stagelist
from hud.core.pipeline import Mark


def rows(items: list[stagelist.StageItem]) -> list[tuple[str, str, int, str]]:
    return [(i.label, i.mark.value, len(i.runs), i.note) for i in items]


def test_今のフローの段ごとに実行を持たせる():
    got = stagelist.for_task(SAMPLE_TASK)
    assert rows(got) == [
        ("テスト作成", "done", 0, ""),
        ("ジャッジ r2", "current", 1, ""),
        ("完了チェック", "next", 0, ""),
    ]
    assert got[1].runs[0]["id"] == "task1-Judge-r2-a1"


def test_段の実行はflow_versionとstepを見ずに段の下の欄のまま持たせる():
    """段に入れるかは status が決める。HUD は `flow_version` と `step` で振り分け直さない。"""
    task = copy.deepcopy(SAMPLE_TASK)
    task["flow"]["version"] = 2
    task["flow"]["steps"][0]["executions"] = [
        execution(id="a", stage="TestGen", round=0, flow_version=0, step=7, status="completed")
    ]
    got = stagelist.for_task(task)
    assert [[r["id"] for r in i.runs] for i in got] == [["a"], ["task1-Judge-r2-a1"], []]
    assert all(i.note == "" for i in got)


def test_前の版と段の外の実行は段の後ろに1つずつ並べる():
    task = copy.deepcopy(SAMPLE_TASK)
    # 前の版の実行の flow_version と step が今の段に当たっても、段に重ねない
    task["earlier_executions"] = [execution(id="old", stage="Impl", round=0, step=1)]
    task["unplaced_executions"] = [
        execution(id="loose", stage="Fix", round=1, step=None, status="failed")
    ]
    got = stagelist.for_task(task)
    assert [len(i.runs) for i in got[:3]] == [0, 1, 0]
    assert rows(got[3:]) == [
        ("実装 r0", "current", 1, stagelist.OLDER),
        ("修正 r1", "failed", 1, stagelist.OUTSIDE),
    ]
    assert [i.key for i in got[3:]] == ["exec:old", "exec:loose"]
    assert got[3].runs[0]["id"] == "old" and got[4].runs[0]["id"] == "loose"


def test_フローが無くても前の版の実行は見せる():
    task = copy.deepcopy(SAMPLE_TASK)
    task["flow"] = None
    task["earlier_executions"] = [execution(status="interrupted")]
    got = stagelist.for_task(task)
    assert [(i.note, i.mark) for i in got] == [(stagelist.OLDER, Mark.SKIPPED)]


def test_段のキーは添字で決まり読み直しても変わらない():
    assert [i.key for i in stagelist.for_task(SAMPLE_TASK)] == ["step:0", "step:1", "step:2"]
