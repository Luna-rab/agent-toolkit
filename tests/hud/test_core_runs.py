"""status の読み方と、見出しの決め方（`hud/core/runs.py`・`hud/core/headline.py`）。"""

from __future__ import annotations

import datetime as dt

import pytest
from hud_samples import execution, judge, quiet, stamp, status

from hud.core import headline, runs


def now() -> dt.datetime:
    # 見本は呼んだ時点の時刻で組むので、モジュールを読んだ時刻ではなく今の時刻と比べる
    return dt.datetime.now().astimezone()


def ago(**kw) -> str:
    return stamp(now() - dt.timedelta(**kw))


def test_一覧は終了コード0の配列だけを受け取る():
    assert runs.listing(0, [status()], "").runs[0]["name"] == "add-cache"
    assert runs.listing(1, None, "autodev: 動かない") == runs.Listing([], "autodev: 動かない")
    assert runs.listing(None, None, "").error == "終了コード None"


def test_終了コード0でも中身が違えばそう理由を出す():
    assert runs.listing(0, None, "").error == "JSON の配列が返らなかった"
    assert runs.listing(0, {"name": "a"}, "警告").error == "JSON の配列が返らなかった · 警告"
    got = runs.single(0, [], "")
    assert (got.status, got.error, got.gone) == (None, "JSON のオブジェクトが返らなかった", False)


@pytest.mark.parametrize("version", [1, 3, None])
def test_一覧に形の版が2でないランがあれば読まずに形の版が違うと出す(version):
    old = status(name="old")
    if version is None:
        del old["format"]
    else:
        old["format"] = version
    got = runs.listing(0, [status(), old], "")
    assert got.runs == []
    assert got.error is not None and "形の版が違う" in got.error


def test_errorだけを持つ読めないランは形の版が無くても形の版の違いにしない():
    broken = {"name": "old", "error": "ValueError: x"}
    got = runs.listing(0, [status(), broken], "")
    assert got.error is None
    assert [runs.name_of(st) for st in got.runs] == ["add-cache", "old"]
    assert runs.error_of(got.runs[1]) == "ValueError: x"


def test_1つのランは終了コード0のオブジェクトを読む():
    st = status()
    got = runs.single(0, st, "")
    assert (got.status, got.error, got.gone) == (st, None, False)


def test_1つのランの形の版が2でなければ読まずに形の版が違うと出す():
    got = runs.single(0, status(format=1), "")
    assert got.status is None and not got.gone
    assert got.error is not None and "形の版が違う" in got.error


def test_1つのランは終了コード5のときだけ消えたとみなす():
    gone = runs.single(5, None, "autodev: そのランが無い")
    assert (gone.status, gone.gone) == (None, True)
    crashed = runs.single(1, None, "Traceback")
    assert (crashed.status, crashed.error, crashed.gone) == (None, "Traceback", False)


@pytest.mark.parametrize("progress", ["broken", {"turns": "many", "lastTool": 3}, None, []])
def test_崩れたprogressはターン数0として読む(progress):
    """`progress` は実行器が書いたファイルの中身そのままなので、崩れていることがある。"""
    st = status()
    judge(st)["progress"] = progress
    got = runs.running(st, now())
    assert [(r.task, r.stage, r.round, r.turns) for r in got] == [("task1", "Judge", 2, 0)]
    assert isinstance(got[0].tool, str)


def test_Zで終わる時刻を読む():
    at = dt.datetime(2026, 10, 3, 1, 2, 3, tzinfo=dt.timezone.utc)
    assert runs.age("2026-10-03T01:02:03.000000Z", at + dt.timedelta(seconds=5)) == 5
    assert runs.age("…", at) is None
    assert runs.age(None, at) is None


def test_走っている実行を経過秒数と進み具合つきで取り出す():
    got = runs.running(status(), now())
    assert [(r.task, r.stage, r.round, r.turns, r.tool) for r in got] == [
        ("task1", "Judge", 2, 7, "Read")
    ]
    assert got[0].seconds is not None and 250 <= got[0].seconds <= 254


def test_走っている実行は段の下と前の版と段の外から集め走っていないものは除く():
    st = quiet()
    task1 = st["tasks"][1]
    task1["flow"]["steps"][0]["executions"] = [
        execution(id="task1-TestGen-r0-a1", stage="TestGen", round=0, status="completed")
    ]
    task1["flow"]["steps"][2]["executions"] = [
        execution(id="task1-Gate-r0-a1", stage="Gate", round=0, status="running", progress=None)
    ]
    task1["earlier_executions"] = [
        execution(id="task1-Impl-r0-a1", stage="Impl", round=0, flow_version=0, step=2)
    ]
    task1["unplaced_executions"] = [
        execution(id="task1-Fix-r1-a1", stage="Fix", round=1, step=None, progress=None)
    ]
    got = runs.running(st, now())
    assert sorted(r.stage for r in got) == ["Fix", "Gate", "Impl"]
    assert {r.task for r in got} == {"task1"}


def test_statuslineに出すのは終えていないラン():
    assert runs.shown(status(), now())
    assert not runs.shown(status(run={**status()["run"], "phase": "finished"}), now())
    assert not runs.shown({"name": "old", "error": "ValueError: x"}, now())


def test_最後のイベントが古いランは回答待ちかパニックのときだけ出す():
    assert not runs.shown(quiet(updated_at=ago(hours=4)), now())
    assert runs.shown(status(updated_at=ago(hours=4)), now())
    panicked = quiet(updated_at=ago(days=2))
    panicked["run"]["phase"] = "panicked"
    assert runs.shown(panicked, now())


def test_終えていないランを先に並べ読めないランは最後():
    done = status(name="aaa", run={**status()["run"], "phase": "finished"})
    broken = {"name": "bbb", "error": "ValueError: x"}
    older = status(name="ccc", updated_at=ago(hours=1))
    got = runs.order([broken, done, older, status(name="zzz")], now())
    assert [runs.name_of(st) for st in got] == ["zzz", "ccc", "aaa", "bbb"]


def test_見出しは走っている実行を短い名前とラウンドで出す():
    head = headline.build(status(), now())
    assert (head.state, head.doing, head.turns, head.tool, head.overview_pr) == (
        headline.State.RUNNING,
        "task1 ジャッジ r2",
        7,
        "Read",
        4,
    )
    assert head.waiting == ("q1",)


def test_見出しは段の下で走っている実装を出す():
    st = quiet()
    task1 = st["tasks"][1]
    task1["flow"]["steps"] = [
        {"stage": "TestGen", "state": "done", "executions": []},
        {"stage": "ConfirmRed", "state": "done", "executions": []},
        {
            "stage": "Impl",
            "state": "current",
            "executions": [execution(id="task1-Impl-r0-a1", stage="Impl", round=0, step=2)],
        },
    ]
    assert headline.build(st, now()).doing == "task1 実装 r0"


def test_見出しは前の版で走っている実行も出す():
    st = quiet()
    st["tasks"][1]["earlier_executions"] = [
        execution(id="task1-Impl-r0-a1", stage="Impl", round=0, flow_version=0, step=2)
    ]
    head = headline.build(st, now())
    assert (head.state, head.doing) == (headline.State.RUNNING, "task1 実装 r0")


def test_同じタスクで並んで走るレビューは1つにまとめターン数は出さない():
    st = status()
    review = execution(stage="Review", round=1)
    st["tasks"][1]["flow"]["steps"][1]["executions"] = [
        review,
        execution(id="task1-AdversarialReview-r1-a1", stage="AdversarialReview", round=1),
    ]
    head = headline.build(st, now())
    assert (head.doing, head.turns) == ("task1 レビュー r1", 0)


def test_走っている実行が無ければフェーズだけを出し止まっているとは言わない():
    head = headline.build(quiet(), now())
    assert (head.state, head.doing, head.waiting, head.stopped) == (
        headline.State.QUIET,
        "実行中",
        (),
        False,
    )


def test_パニックは走っている実行より先に見せ原因を1行にして添える():
    st = status(run={**status()["run"], "phase": "panicked", "panic_cause": "利用枠\n  上限"})
    head = headline.build(st, now())
    assert (head.state, head.panic_cause) == (headline.State.PANICKED, "利用枠 上限")


@pytest.mark.parametrize(
    ("stopped", "phase", "running", "awaiting"),
    [
        (True, "running", False, False),
        # 次の 3 つは、HUD が自分で決めていたなら止まっていないとした組み合わせ
        (True, "panicked", False, False),
        (True, "running", True, False),
        (True, "running", False, True),
        (False, "running", False, False),
        (False, "finishing", False, False),
        (False, "panicked", True, True),
    ],
)
def test_driverが止まっているかはrunのdriver_stoppedだけで決める(stopped, phase, running, awaiting):
    st = quiet()
    st["run"].update(
        phase=phase, driver_running=running, awaiting_answer=awaiting, driver_stopped=stopped
    )
    assert headline.build(st, now()).stopped is stopped


def test_前のdriverの子のpidを拾う():
    st = status(run={**status()["run"], "live_children": [41, 42]})
    assert headline.build(st, now()).leftovers == (41, 42)


def test_積んだ数と分母はrunの欄を読みタスクの状態を数え直さない():
    st = quiet()
    st["run"].update(stacked_tasks=1, stack_target_tasks=4)
    for task in st["tasks"]:
        task["status"] = "dropped"
    st["tasks"][2]["status"] = "pending"
    head = headline.build(st, now())
    assert (head.stacked, head.total) == (1, 4)
    st["run"].update(stacked_tasks=3, stack_target_tasks=7)
    head = headline.build(st, now())
    assert (head.stacked, head.total) == (3, 7)


def test_エスカレーション中のタスクを数える():
    assert headline.build(status(), now()).escalated == 1


@pytest.mark.parametrize(
    ("task", "want"),
    [
        ({"title": "パーサ", "kind": "implementation"}, "パーサ"),
        ({"title": None, "kind": "planning"}, "計画"),
        ({"title": None, "kind": "git"}, "git 管理"),
    ],
)
def test_件名の無いタスクは種類の名前で呼ぶ(task, want):
    assert runs.task_label(task) == want
