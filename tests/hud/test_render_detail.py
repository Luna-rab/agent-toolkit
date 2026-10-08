"""autodev-watch の右ペイン（`hud/render/detail.py`）に出す指摘・Gate の落ち・spec・実行の終わり・エスカレーション。"""

from __future__ import annotations

import copy
import datetime as dt
import io
import re
import time

import pytest
from hud_samples import SAMPLE_TASK, execution, finding, status
from rich.console import Console

from hud.core import headline, pipeline
from hud.core.pipeline import Mark
from hud.core.stagelist import StageItem
from hud.render import detail

NOW = dt.datetime(2026, 10, 4, 10, 30, tzinfo=dt.timezone.utc)


def text_of(renderable) -> str:
    out = io.StringIO()
    Console(file=out, width=300, color_system=None).print(renderable)
    return out.getvalue()


def task_text(task: dict) -> str:
    return text_of(detail.task_detail(task, pipeline.steps(task), NOW))


def blocks(text: str) -> int:
    """空行で区切られた塊の数。"""
    return len([b for b in re.split(r"\n\s*\n", text) if b.strip()])


def nonblank(text: str) -> set[str]:
    return {line.rstrip() for line in text.splitlines() if line.strip()}


def running_task(**over) -> dict:
    task = copy.deepcopy(SAMPLE_TASK)
    task["flow"]["steps"][1]["executions"][0]["started_at"] = "2026-10-04T10:20:00.000000Z"
    task.update(over)
    return task


def gate_run(attempt: int, started: str, failures: list[dict]) -> dict:
    return execution(
        id=f"task1-Gate-r2-a{attempt}",
        stage="Gate",
        attempt=attempt,
        step=2,
        status="completed",
        started_at=started,
        ended_at=started,
        progress=None,
        gate_failures=failures,
    )


def with_gate(runs: list[dict]) -> dict:
    task = running_task()
    task["flow"]["steps"][1]["state"] = "done"
    task["flow"]["steps"][2].update(state="current", executions=runs)
    return task


# --- タスクの詳細: 指摘 ---


def test_開いている指摘は本文と場所を評価の重い順に出し閉じたものは件数だけ出す():
    task = running_task(
        findings=[
            finding(id="R1", rating="nit", body="名前が長い", location="c.py:3"),
            finding(id="R2", rating="must-fix", body="空の入力で落ちる", location="a.py:1"),
            finding(id="R3", rating="should-fix", body="例外を握りつぶす", location="b.py:2"),
            finding(id="R4", rating="must-fix", status="closed", body="閉じた指摘その一"),
            finding(id="R5", rating="nit", status="closed", body="閉じた指摘その二"),
            finding(id="R6", rating="should-fix", status="rejected", body="退けた指摘"),
        ]
    )
    text = task_text(task)
    bodies = ("空の入力で落ちる", "例外を握りつぶす", "名前が長い")
    assert all(b in text for b in bodies)
    must, should, nit = (text.index(b) for b in bodies)
    assert must < should < nit
    assert all(loc in text for loc in ("a.py:1", "b.py:2", "c.py:3"))
    assert "閉じた指摘" not in text and "退けた指摘" not in text
    assert "closed 2 · rejected 1" in text
    assert "carried" not in text


# --- タスクの詳細: Gate の落ち ---


def test_最後に始めたGateの実行の落ちた項目と理由を出す():
    task = with_gate(
        [
            gate_run(
                1, "2026-10-04T10:00:00.000000Z", [{"item": "lint", "reason": "ruff が 2 件"}]
            ),
            gate_run(
                2,
                "2026-10-04T10:10:00.000000Z",
                [{"item": "verify", "reason": "pytest が 1 で終えた"}],
            ),
        ]
    )
    text = task_text(task)
    assert "verify" in text and "pytest が 1 で終えた" in text
    assert "ruff が 2 件" not in text


def test_最後のGateが通っていればGateの落ちを出さない():
    task = with_gate(
        [
            gate_run(
                1,
                "2026-10-04T10:00:00.000000Z",
                [{"item": "verify", "reason": "pytest が 1 で終えた"}],
            ),
            gate_run(2, "2026-10-04T10:10:00.000000Z", []),
        ]
    )
    assert "pytest が 1 で終えた" not in task_text(task)


# --- タスクの詳細: spec と判断の履歴 ---

NOTES = [
    {"text": "空の入力は弾く", "origin": "user", "question": "q1"},
    {"text": "エラーは呼び出し元に返す", "origin": "run-supervisor", "question": None},
]


def test_specと判断の履歴を出す():
    task = running_task(
        spec={
            "dod": "パーサが入力を読み構文木を返す",
            "acceptance": ["空の入力で空の構文木を返す", "壊れた入力で位置を返す"],
            "scope": ["触る: parser.py", "触らない: cli.py"],
        },
        notes=copy.deepcopy(NOTES),
    )
    text = task_text(task)
    for want in (
        "パーサが入力を読み構文木を返す",
        "空の入力で空の構文木を返す",
        "壊れた入力で位置を返す",
        "触る: parser.py",
        "触らない: cli.py",
        "空の入力は弾く",
        "エラーは呼び出し元に返す",
    ):
        assert want in text


def test_判断の履歴はユーザーの回答とラン統括の回答を見分けて出す():
    swapped = copy.deepcopy(NOTES)
    swapped[0]["origin"], swapped[1]["origin"] = "run-supervisor", "user"
    # 出どころだけを入れ替えると、見え方が変わる
    assert task_text(running_task(notes=copy.deepcopy(NOTES))) != task_text(
        running_task(notes=swapped)
    )


def test_specがnullならspecの節を出さない():
    with_spec = task_text(running_task(notes=[]))
    without = task_text(running_task(spec=None, notes=[]))
    assert "パーサが入力を読み、構文木を返す" in with_spec
    assert blocks(without) < blocks(with_spec)
    assert nonblank(without) <= nonblank(with_spec)


# --- 段の詳細: 実行の終わり ---


@pytest.fixture
def utc(monkeypatch):
    """終了時刻を手元の時刻帯に直しても UTC のまま出しても、同じ時刻に見えるようにする。"""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def stage_text(run: dict, now: dt.datetime = NOW) -> str:
    item = StageItem("step:2", "完了チェック", Mark.FAILED, (run,))
    return text_of(detail.stage_detail(item, now))


def ended(**over) -> dict:
    base = {
        "id": "task1-Gate-r2-a1",
        "stage": "Gate",
        "status": "failed",
        "started_at": "2026-10-04T10:00:00.000000Z",
        "ended_at": "2026-10-04T10:04:12.000000Z",
        "progress": None,
    }
    return execution(**{**base, **over})


def test_終えた実行に終了時刻と所要時間と理由を出す(utc):
    text = stage_text(ended(end_reason="落ちた"))
    assert "10:04:12" in text and "4m12s" in text and "落ちた" in text


def test_理由が無く中断した実行はinterrupted_byを理由に出す(utc):
    text = stage_text(ended(status="interrupted", end_reason=None, interrupted_by="panic"))
    assert "panic" in text


def test_走っている実行には終了時刻を出さない(utc):
    run = execution(started_at="2026-10-04T10:00:00.000000Z", progress=None)
    text = stage_text(run, now=dt.datetime(2026, 10, 4, 10, 4, 12, tzinfo=dt.timezone.utc))
    assert re.search(r"\d{1,2}:\d{2}:\d{2}", text) is None


# --- ランの詳細 ---


def run_text(st: dict) -> str:
    return text_of(detail.run_detail(headline.build(st, NOW), st, NOW))


def test_ランのエスカレーションの理由と本文を出す():
    st = status()
    st["escalations"][0].update(
        reason="空の入力の扱いが受入条件に無い", question="空の入力をエラーにするか決めてほしい"
    )
    text = run_text(st)
    assert "空の入力の扱いが受入条件に無い" in text
    assert "空の入力をエラーにするか決めてほしい" in text


def test_計画の節に設計レビューの開いている指摘を評価の重い順に出す():
    st = status()
    st["plan"]["findings"] = [
        finding(id="D1", rating="should-fix", body="終了時刻の見せ方が決まっていない", design=1),
        finding(id="D2", rating="must-fix", body="終了コードが表に無い", design=1),
        finding(id="D3", rating="nit", status="closed", body="閉じた設計の指摘", design=1),
    ]
    lines = [line.strip() for line in run_text(st).splitlines()]
    text = "\n".join(lines)
    plan = lines.index("計画")
    assert "終了コードが表に無い" in text and "終了時刻の見せ方が決まっていない" in text
    must = next(i for i, line in enumerate(lines) if "終了コードが表に無い" in line)
    should = next(i for i, line in enumerate(lines) if "終了時刻の見せ方が決まっていない" in line)
    assert plan < must < should
    assert "閉じた設計の指摘" not in text
