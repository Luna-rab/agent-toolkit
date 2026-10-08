"""statusline を通しで動かす。Claude Code と同じく、標準入力に JSON を渡して標準出力を読む。"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from hud_samples import (
    calls,
    execution,
    finding,
    judge,
    quiet,
    session,
    stamp,
    status,
    write_fake_entry,
    write_usage,
)

from hud.app import statusline
from hud.ports import autodev

#: インストールされた入口。`uv run pytest` ならこの仮想環境の bin にある
AGENT_HUD = Path(sys.executable).parent / "agent-hud"


def raw(
    stdin: str, tmp_path, columns: int, statuses: list[dict] | None = None, entry: str | None = None
) -> subprocess.CompletedProcess[str]:
    # 本物の OAuth トークンで利用状況を取りに行かないよう、Claude Code の設定とキャッシュを空の場所に向ける。
    # autodev の入口は偽物に替え、`statuses` を返させる
    env = {
        **os.environ,
        **write_fake_entry(tmp_path, statuses or []),
        "COLUMNS": str(columns),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    if entry is not None:
        env["AUTODEV_ENTRY"] = entry
    return subprocess.run(
        [str(AGENT_HUD), "statusline"],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def run(
    tmp_path,
    columns: int = 100,
    data: dict | None = None,
    statuses: list[dict] | None = None,
    entry: str | None = None,
) -> list[str]:
    """Claude Code が表示する行。Claude Code は各行の前後の空白を削り、空になった行を捨てる。"""
    out = raw(json.dumps(data or session(time.time())), tmp_path, columns, statuses, entry)
    assert out.returncode == 0, out.stderr
    shown = [line.strip() for line in out.stdout.strip().split("\n") if line.strip()]
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in shown]


def test_セッションは使用量_場所_利用枠2本の4行(tmp_path):
    lines = run(tmp_path)
    assert lines[:2] == ["Opus 5.5 · high   ctx ━━━━━───── 50%   $3.21", "dotfiles"]
    assert lines[2].startswith("5h ")
    assert lines[2].endswith(" 72% ▲12 2h00m")
    # 窓の 6 割が過ぎた位置（40 マスの 23 番目）に目盛りを置く
    assert lines[2][3:].index("┃") == 23
    assert lines[3] == "7d ━━━━━━━━━━━━╾─────────────────────────── 31%"


def test_クレジットが有効なら月次の棒を利用枠の下に出す(tmp_path):
    write_usage(tmp_path / "cache" / "claude-hud" / "usage.json", time.time())
    lines = run(tmp_path)
    assert len(lines) == 5
    assert lines[4].startswith("mo ")
    assert " 1% $5.64/$800 " in lines[4]


def test_幅で埋めないので縮めても行頭は残る(tmp_path):
    """幅を変えても描き直されないので、右寄せや行末の空白で幅を埋めてはいけない。"""
    assert all(line == line.rstrip() for line in run(tmp_path, columns=200))


def test_幅が足りないと優先度の低い部品から落とす(tmp_path):
    assert run(tmp_path, columns=40)[0] == "Opus 5.5 · high   ctx ━━━━━───── 50%"


def test_入力が壊れていても落ちない(tmp_path):
    assert raw("not json", tmp_path, 100).returncode == 0


HEADLINE = (
    "autodev add-cache ▸ task1 ジャッジ r2 · 4m12s 7ターン Read"
    " · 回答待ち q1 · エスカレーション 1 · 概要 PR #4"
)


def test_status_jsonは1回の描画で1回だけ呼ぶ(tmp_path):
    run(tmp_path, statuses=[status(), quiet(name="other")])
    assert calls(tmp_path) == ["status --json"]


def test_幅が足りればタスクリストを右に置く(tmp_path):
    lines = run(tmp_path, columns=220, statuses=[status()])
    assert len(lines) == 5
    left, right = lines[0].split("  │  ")
    assert left.startswith("Opus 5.5")
    assert right == HEADLINE
    assert lines[1].split("  │  ")[1].startswith("  ◼ task1 パーサを足す")
    assert lines[4].split("  │  ")[1] == "  ◼ task4 移行" + " " * 18 + "  stall"


def test_利用枠が無くてもタスクリストの列はそろう(tmp_path):
    """Enterprise などでは rate_limits が渡されず、左の行が 2 行しかない。"""
    data = session(time.time())
    del data["rate_limits"]
    lines = run(tmp_path, columns=220, data=data, statuses=[status()])
    assert len(lines) == 5
    side = lines[0].index("  │  ")
    assert all(line.index("  │  ") == side for line in lines)


def test_幅が足りなければタスクリストを下に置く(tmp_path):
    lines = run(tmp_path, columns=120, statuses=[status()])
    assert lines[4] == HEADLINE
    assert lines[5].endswith("テスト作成 ✔ › ジャッジ r2 ◼ › 完了チェック")
    assert lines[6].startswith("  ✔ task2 キャッシュの土台")
    assert lines[6].endswith("#5")
    assert lines[7] == "  ◻ task3 CLI に出す"
    assert lines[8].endswith("stall")


def test_走っている実行が無ければフェーズと積んだ数を出す(tmp_path):
    lines = run(tmp_path, statuses=[quiet()])
    assert lines[4] == (
        "autodev add-cache ▸ 実行中 · スタック済み 1/4 · エスカレーション 1 · 概要 PR #4"
    )


def test_最後のイベントが古く回答も待たないランは出さない(tmp_path):
    stale = stamp(dt.datetime.now().astimezone() - dt.timedelta(hours=4))
    assert len(run(tmp_path, statuses=[quiet(updated_at=stale)])) == 4


def test_読めないランは出さない(tmp_path):
    """古いディレクトリ 1 つで毎回赤字が出ないように。読めないランは autodev-watch でだけ見せる。"""
    assert len(run(tmp_path, statuses=[{"name": "old", "error": "ValueError: x"}])) == 4


def test_パニックしたランは最後のイベントが古くても出す(tmp_path):
    st = quiet(updated_at=stamp(dt.datetime.now().astimezone() - dt.timedelta(days=2)))
    st["run"]["phase"] = "panicked"
    assert run(tmp_path, statuses=[st])[4].startswith("autodev add-cache ▸ パニック")


def test_パニックの原因を幅を切って添える(tmp_path):
    st = quiet()
    st["run"].update(
        phase="panicked", driver_running=False, panic_cause="利用枠の上限に当たった" * 5
    )
    lines = run(tmp_path, columns=200, statuses=[st])
    head = next(line for line in lines if "autodev add-cache ▸ " in line)
    assert "autodev add-cache ▸ パニック · 呼び直すまで進まない · 利用枠の上限に" in head
    assert "…" in head
    # パニックの後に driver がいないのは当たり前なので、止まっているとは言わない
    assert "driver 停止" not in head


def test_driverが止まっていれば回答待ちとエスカレーションの後ろに短く出す(tmp_path):
    st = quiet()
    st["run"].update(driver_stopped=True, live_children=[4242, 4343])
    assert run(tmp_path, columns=120, statuses=[st])[4] == (
        "autodev add-cache ▸ 実行中 · スタック済み 1/4 · エスカレーション 1"
        " · driver 停止 · 残った子 pid 4242 4343 · 概要 PR #4"
    )


def head_of(tmp_path, st: dict) -> str:
    """ラン `add-cache` の見出しの行。タスクリストを右に置いても下に置いても、見出しから先だけを返す。"""
    lines = run(tmp_path, columns=200, statuses=[st])
    found = [line for line in lines if "autodev add-cache ▸ " in line]
    assert len(found) == 1, lines
    return found[0][found[0].index("autodev add-cache ▸ ") :]


def test_driver停止はrunのdriver_stoppedだけで決めphaseやdriver_runningを見ない(tmp_path):
    stopped = quiet()
    # HUD が自分で決めていたなら止まっていないとした組み合わせ（driver は走っている）
    stopped["run"].update(driver_stopped=True, driver_running=True)
    assert "driver 停止" in head_of(tmp_path, stopped)
    running = quiet()
    # HUD が自分で決めていたなら止まっているとした組み合わせ
    running["run"].update(driver_stopped=False, driver_running=False, phase="planning")
    assert "driver 停止" not in head_of(tmp_path, running)


def test_積んだ数はrunの欄を読みタスクの状態を数え直さない(tmp_path):
    st = quiet()
    st["run"].update(stacked_tasks=1, stack_target_tasks=4)
    # タスクの状態から数え直すと 0/3 になる
    st["tasks"][2]["status"] = "pending"
    st["tasks"][4]["status"] = "dropped"
    st["tasks"][4]["escalations"] = []
    assert head_of(tmp_path, st).startswith("autodev add-cache ▸ 実行中 · スタック済み 1/4")


def test_段の下で走っている実装を見出しに出す(tmp_path):
    st = quiet()
    st["tasks"][1]["flow"]["steps"] = [
        {"stage": "TestGen", "state": "done", "executions": []},
        {"stage": "ConfirmRed", "state": "done", "executions": []},
        {
            "stage": "Impl",
            "state": "current",
            "executions": [execution(id="task1-Impl-r0-a1", stage="Impl", round=0, step=2)],
        },
    ]
    assert head_of(tmp_path, st).startswith("autodev add-cache ▸ task1 実装 r0")


def test_前の版で走っている実行も見出しに出す(tmp_path):
    st = quiet()
    st["tasks"][1]["earlier_executions"] = [
        execution(id="task1-Impl-r0-a1", stage="Impl", round=0, flow_version=0, step=2)
    ]
    assert head_of(tmp_path, st).startswith("autodev add-cache ▸ task1 実装 r0")


def test_形の版が2でないstatusなら形の版が違うと出して0で終わる(tmp_path):
    lines = run(tmp_path, statuses=[status(format=1)])
    assert len(lines) == 5
    assert "形の版が違う" in lines[4]
    assert not any(line.startswith("autodev add-cache ▸ ") for line in lines)


def test_崩れたprogressがあってもtracebackを出さずに描く(tmp_path):
    """`progress` は実行器が書いたファイルの中身そのままなので、崩れていることがある。"""
    st = status()
    judge(st)["progress"]["turns"] = "many"
    other = status(name="other")
    judge(other)["progress"] = "broken"
    lines = run(tmp_path, columns=120, statuses=[st, other])
    assert lines[4].startswith("autodev add-cache ▸ task1 ジャッジ r2")
    assert any(line.startswith("autodev other ▸ task1 ジャッジ r2") for line in lines)


def test_組み立ての途中で落ちても理由を1行で出す(monkeypatch):
    def broken(st, now):
        raise KeyError("x")

    monkeypatch.setattr(autodev, "statuses", lambda: autodev.Reply(0, [status()], ""))
    monkeypatch.setattr(statusline, "run_block", broken)
    got = statusline.autodev_block(dt.datetime.now().astimezone())
    assert [line.plain for line in got] == ["autodev status を読めない · KeyError: 'x'"]


def test_statusが返らなければそう出す(tmp_path):
    entry = tmp_path / "broken-autodev.py"
    entry.write_text("import sys; sys.stderr.write('autodev: まだ動かない\\n'); sys.exit(1)")
    lines = run(tmp_path, statuses=[], entry=str(entry))
    assert lines[4:] == ["autodev status を読めない · autodev: まだ動かない"]


def row_of(lines: list[str], task_id: str) -> str:
    """タスク `task_id` の行。タスクリストを右に置いても下に置いても、記号から先だけを返す。"""
    rows = [line.split("│")[-1].strip() for line in lines]
    found = [row for row in rows if row.split()[1:2] == [task_id]]
    assert len(found) == 1, lines
    return found[0]


def test_走っているタスクの行に開いている指摘の件数を評価ごとに出し行数は変えない(tmp_path):
    plain = run(tmp_path, columns=400, statuses=[status()])
    st = status()
    st["tasks"][1]["findings"] = [
        *(finding(id=f"R{i}", rating="must-fix") for i in (1, 2)),
        *(finding(id=f"R{i}", rating="should-fix") for i in (3, 4, 5)),
        # 閉じた指摘は数えない
        finding(id="R6", rating="must-fix", status="closed"),
        finding(id="R7", rating="nit", status="rejected"),
    ]
    lines = run(tmp_path, columns=400, statuses=[st])
    row = row_of(lines, "task1")
    assert "指摘 must 2 / should 3" in row
    assert "nit" not in row
    assert len(lines) == len(plain)


def test_開いている指摘が無ければ指摘を出さない(tmp_path):
    st = status()
    st["tasks"][1]["findings"] = [finding(status="closed"), finding(id="R2", status="carried")]
    assert "指摘" not in row_of(run(tmp_path, columns=400, statuses=[st]), "task1")


def escalation(**over) -> dict:
    """見本の task4 のエスカレーションを写して変えたもの。"""
    base = dict(status()["tasks"][4]["escalations"][0])
    base.update(over)
    return base


def test_エスカレーション中のタスクの行は最初の理由の頭24字を出し種類を出さない(tmp_path):
    head = "一二三四五六七八九十" * 2 + "一二三四"
    reason = head + "壱弐参肆伍陸"
    assert (len(head), len(reason)) == (24, 30)
    st = status()
    st["tasks"][4]["escalations"] = [
        escalation(kind="ask", reason=reason, question="空の入力は弾くか"),
        escalation(id="task4#4", kind="stall", reason="二つ目の理由"),
    ]
    detail = row_of(run(tmp_path, columns=400, statuses=[st]), "task4").split("移行", 1)[1]
    assert head in detail
    assert not any(c in detail for c in "壱弐参肆伍陸")
    assert "ask" not in detail and "stall" not in detail and "二つ目" not in detail
