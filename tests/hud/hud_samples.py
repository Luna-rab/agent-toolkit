"""hud の検査で使う入力。Claude Code が渡す JSON と、`autodev status --json` の結果。"""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any


def session(now: float) -> dict:
    return {
        "model": {"display_name": "Opus 5.5"},
        "effort": {"level": "high"},
        "workspace": {"current_dir": "/nonexistent/dotfiles"},
        "context_window": {"used_percentage": 50},
        "cost": {"total_cost_usd": 3.214},
        "rate_limits": {
            # 窓の 6 割が過ぎたところで 72% 使っている → 12 ポイント使いすぎ
            "five_hour": {"used_percentage": 72, "resets_at": now + 5 * 3600 * 0.4 + 30},
            "seven_day": {"used_percentage": 31},
        },
        "pr": {"number": 22, "url": "https://example.com/pull/22", "review_state": "pending"},
    }


def usage() -> dict:
    """claude.ai の利用状況。月 $800 の上限のうち $5.64 使っている。"""
    return {
        "five_hour": None,
        "seven_day": None,
        "extra_usage": {
            "is_enabled": True,
            "monthly_limit": 80000,
            "used_credits": 564,
            "decimal_places": 2,
        },
    }


def write_usage(path, checked_at: float) -> None:
    """`hud/ports/usage.py` のキャッシュ。`checked_at` が新しければ取りに行かずにこれを使う。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"checked_at": checked_at, "data": usage()}), encoding="utf-8")


def stamp(at: dt.datetime) -> str:
    """status と同じ形の時刻（UTC で `Z` 終わり）。"""
    return at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


#: `status --json` の実行 1 つの見本（task1 の Judge）。時刻だけ `status()` が埋める
SAMPLE_EXECUTION: dict[str, Any] = {
    "id": "task1-Judge-r2-a1",
    "stage": "Judge",
    "round": 2,
    "attempt": 1,
    "flow_version": 1,
    "step": 1,
    "status": "running",
    "started_at": "…",
    "progress": {
        "stage": "Judge",
        "state": "running",
        "turns": 7,
        "lastTool": "Read",
        "hookDenials": 0,
        "events": 41,
        "updated": "…",
    },
    "ended_at": None,
    "end_reason": None,
    "interrupted_by": None,
    "gate_failures": [],
}

#: `status --json` の task1 の見本。実行は段の下（`flow.steps[].executions`）に入る
SAMPLE_TASK: dict[str, Any] = {
    "id": "task1",
    "kind": "implementation",
    "title": "パーサを足す",
    "status": "running",
    "terminal": False,
    "blocked_by": [],
    "branch": "stack/add-cache--task-1",
    "pr": None,
    "superseded_by": None,
    "takes_over": None,
    "integration_failed": False,
    "awaiting_requeue": False,
    "spec": {
        "dod": "パーサが入力を読み、構文木を返す",
        "acceptance": ["空の入力で空の構文木を返す"],
        "scope": ["触る: parser.py"],
    },
    "notes": [],
    "flow": {
        "version": 1,
        "finished": False,
        "halted": False,
        "job": None,
        "steps": [
            {"stage": "TestGen", "state": "done", "executions": []},
            {
                "stage": "ReviewLoop",
                "state": "current",
                "inner": "Judge",
                "round": 2,
                "executions": [copy.deepcopy(SAMPLE_EXECUTION)],
            },
            {"stage": "Gate", "state": "pending", "executions": []},
        ],
    },
    "earlier_executions": [],
    "unplaced_executions": [],
    "escalations": [],
    "findings": [],
}


#: `status --json` の指摘 1 つの見本（`tasks[].findings[]`・`plan.findings[]`）
SAMPLE_FINDING: dict[str, Any] = {
    "id": "R1",
    "rating": "must-fix",
    "status": "open",
    "body": "空の入力で落ちる",
    "location": "parser.py:12",
    "fixes": 0,
    "stalled": False,
    "comments": [],
    "design": None,
}


def finding(**over) -> dict[str, Any]:
    """指摘 1 つ。見本の欄を持つ。"""
    base = copy.deepcopy(SAMPLE_FINDING)
    base.update(over)
    return base


def execution(**over) -> dict[str, Any]:
    """実行 1 つ。見本の Judge の欄を持つ。"""
    base = copy.deepcopy(SAMPLE_EXECUTION)
    base.update(over)
    return base


def judge(st: dict) -> dict[str, Any]:
    """`status()` の task1 で走っている Judge の実行。"""
    return st["tasks"][1]["flow"]["steps"][1]["executions"][0]


def task(**over) -> dict[str, Any]:
    """実装タスク。見本の欄を持ち、フローも実行も無い未着手のもの。"""
    base = copy.deepcopy(SAMPLE_TASK)
    base.update(status="pending", flow=None, branch=None)
    base.update(over)
    return base


def status(**over) -> dict[str, Any]:
    """見本のラン。task1 の Judge が 4 分 12 秒走っていて、回答を待つ質問が 1 つある。

    例の task1 のほかに、終えた計画タスク・積んだ task2・未着手の task3・エスカレーション中の task4 を足す。
    """
    now = dt.datetime.now().astimezone()
    running = copy.deepcopy(SAMPLE_TASK)
    started = stamp(now - dt.timedelta(minutes=4, seconds=12))
    judging = running["flow"]["steps"][1]["executions"][0]
    judging["started_at"] = started
    judging["progress"]["updated"] = stamp(now)
    data: dict[str, Any] = {
        "format": 2,
        "name": "add-cache",
        "last_seq": 57,
        "updated_at": stamp(now),
        "rejections": [],
        "run": {
            "phase": "running",
            "awaiting_answer": True,
            "started_at": stamp(now - dt.timedelta(hours=1)),
            "repository": "/repo",
            "base": "main",
            "limit": 2,
            "resumes": 0,
            "panic_cause": None,
            "directory": "/state/autodev/add-cache",
            "driver_running": True,
            "live_children": [],
            "driver_stopped": False,
            "stacked_tasks": 1,
            "stack_target_tasks": 4,
        },
        "tasks": [
            task(id="planning", kind="planning", title=None, status="finished", terminal=True),
            running,
            task(id="task2", title="キャッシュの土台", status="stacked", terminal=True, pr=5),
            task(id="task3", title="CLI に出す"),
            task(
                id="task4",
                title="移行",
                status="escalated",
                escalations=[
                    {
                        "id": "task4#3",
                        "kind": "stall",
                        "origin": "task4-Judge-r3-a1",
                        "reason": "",
                        "question": None,
                    }
                ],
            ),
        ],
        "stack": {
            "overview": {"task": "git", "branch": "autodev/add-cache", "pr": 4, "base": "main"},
            "entries": [
                {"task": "task2", "branch": "stack/add-cache--task-2", "pr": 5, "base": "main"}
            ],
            "top": "autodev/add-cache",
            "queue": [],
            "current": None,
            "parked": None,
            "paused": False,
            "cuts_pending": 0,
        },
        "questions": [{"id": "q1", "body": "空の入力は弾くか", "escalation": "run#7"}],
        "escalations": [
            {
                "id": "run#7",
                "kind": "ask",
                "task": "planning",
                "source": "task/planning#5",
                "for_user": False,
                "answer_only": False,
                "failures": 0,
                "reason": "空の入力の扱いが受入条件に無い",
                "question": "空の入力は弾くか",
            }
        ],
        "plan": {
            "planning": False,
            "planned": True,
            "applied_design": 1,
            "replans_without_stack": 0,
            "design": {
                "versions": [1, 2],
                "settled": 1,
                "proposal": {
                    "version": 2,
                    "state": "awaiting",
                    "round": 1,
                    "awaiting": "design-ambiguous",
                },
            },
            "findings": [],
        },
    }
    data.update(over)
    return data


def quiet(**over) -> dict[str, Any]:
    """走っている実行も回答待ちも無いラン。"""
    st = status(**over)
    st["run"]["awaiting_answer"] = False
    st["questions"] = []
    st["tasks"][1]["flow"]["steps"][1]["executions"] = []
    return st


#: `autodev.py` の代わりに置く入口。`FAKE_STATUS` の JSON の配列を、本物と同じ約束で返す
#: （`--name` が無ければ配列、あればその要素で、無ければ終了コード 5）。呼ばれた引数を `FAKE_CALLS` に足す
FAKE_ENTRY = """\
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\\n")
with open(os.environ["FAKE_STATUS"], encoding="utf-8") as fh:
    data = json.load(fh)
if "--name" not in args:
    print(json.dumps(data))
    sys.exit(0)
name = args[args.index("--name") + 1]
found = [st for st in data if st.get("name") == name]
if not found:
    sys.stderr.write("autodev: そのランが無い\\n")
    sys.exit(5)
print(json.dumps(found[0]))
"""


def write_fake_entry(root: Path, statuses: list[dict]) -> dict[str, str]:
    """偽の入口と、それが返す status を `root` に置き、向けるための環境変数を返す。"""
    entry = root / "fake-autodev.py"
    entry.write_text(FAKE_ENTRY, encoding="utf-8")
    (root / "status.json").write_text(json.dumps(statuses), encoding="utf-8")
    return {
        "AUTODEV_ENTRY": str(entry),
        "FAKE_STATUS": str(root / "status.json"),
        "FAKE_CALLS": str(root / "calls.txt"),
    }


def calls(root: Path) -> list[str]:
    """偽の入口が呼ばれた引数（1 回 1 行）。"""
    path = root / "calls.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []
