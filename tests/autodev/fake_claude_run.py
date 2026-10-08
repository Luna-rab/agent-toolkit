"""`autodev run` の検査で PATH に置く偽の `claude`。本物は叩かない。

渡された `--json-schema` の本文を `schemas/` のファイルと照らして、どのステージか統括かを見分け、
実装タスクを 0 件にした計画でランを仕上げまで進める決め打ちの結果を返す。

- Plan は、初めて起こされたら ask で止まり（defer の形の result）、`--resume` で起こされたら
  `answers/<tool_use_id>.json` の回答を `decisions` に写して計画を返す
- ラン統括は、上がってきたエスカレーションをユーザーに聞き、届いた回答でそのまま答え、全部が
  終わったら仕上げる

`FAKE_CLAUDE_SCHEMAS` に `schemas/` の置き場、`FAKE_CLAUDE_LOG` に呼ばれ方を書き足すファイルを渡す。
`FAKE_CLAUDE_HANG` に役の名前を渡すと、その役は result を返さずに待ち続ける（`hang`）。
`FAKE_CLAUDE_STUBBORN=1` なら、interrupt を受けても打ち切らない。
`FAKE_CLAUDE_TASK=1` なら、計画が実装タスクを 1 つ返し、タスク統括は TestGen から始まるフローを組む。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

#: 止めた ask の呼び出しの id（回答のファイルの名前になる）
ASK_ID = "toolu_ask1"
QUESTION = "q-ttl"
#: `FAKE_CLAUDE_TASK=1` のとき、計画が返す実装タスク 1 つ
TASK = {
    "id": "task1",
    "title": "TTL を足す",
    "dod": "TTL が効く",
    "acceptance": ["TTL が 300 秒で切れる"],
    "scope": ["a.txt"],
    "entryPoints": ["a.txt:1"],
    "boundary": "a.txt の中",
    "taskTests": ["true"],
    "blockedBy": [],
}
_NOTICE = re.compile(r"## `<通知>`\s*```json\n(.*?)\n```", re.DOTALL)


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(**fields: object) -> None:
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 1,
            "result": "done",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "total_cost_usd": 0.0,
            "permission_denials": [],
            **fields,
        }
    )


def option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def role(schema: str | None) -> str:
    if schema is None:
        return "none"
    wanted = json.loads(schema)
    for path in Path(os.environ["FAKE_CLAUDE_SCHEMAS"]).glob("*.json"):
        if json.loads(path.read_text(encoding="utf-8")) == wanted:
            return path.stem
    return "unknown"


def prompt_of(line: str) -> str:
    if not line:
        return ""
    return str(json.loads(line)["message"]["content"])


def supervise(prompt: str) -> dict:
    found = _NOTICE.search(prompt)
    notice = json.loads(found.group(1)) if found else {}
    kind = notice.get("notice")
    if kind == "escalation":
        return {
            "decision": "ask-user",
            "askUser": {
                "question": QUESTION,
                "body": str(notice.get("reason") or "どちらにするか"),
                "escalation": notice["id"],
            },
        }
    if kind == "answer":
        return {
            "decision": "answer",
            "answer": {
                "escalation": notice["escalation"],
                "answer": None,
                "question": notice["question"],
            },
        }
    return {"decision": "finish", "finish": {"readyOverview": False}}


def supervise_task() -> dict:
    """どの通知にも、TestGen から始まる実装のフローを返す（`started` の 1 回だけを想定する）。"""
    stages = ["TestGen", "ConfirmRed", "Impl", "ReviewLoop", "Gate", "WritePrBody"]
    steps: list[dict] = [{"stage": stage} for stage in stages]
    steps[3]["reviewers"] = {"first": ["Review"]}
    return {"decision": "run-flow", "runFlow": {"steps": steps, "respondsTo": None}}


def plan(resumed: bool) -> None:
    if not resumed:
        launcher = "/skill/bin/autodev"
        result(
            stop_reason="tool_deferred",
            deferred_tool_use={
                "id": ASK_ID,
                "name": "Bash",
                "input": {"command": f"{launcher} ask --question 'TTL は 60 秒か 300 秒か'"},
            },
        )
        return
    # cwd は trees/overview。回答はランディレクトリの answers/ にある
    answer_file = Path.cwd().parent.parent / "answers" / f"{ASK_ID}.json"
    answer = json.loads(answer_file.read_text(encoding="utf-8"))["answer"]
    result(
        structured_output={
            "design": "# 設計\n\n変えるものは無い。\n",
            "codemap": "# コードマップ\n\n- a.txt\n",
            "tasks": [TASK] if os.environ.get("FAKE_CLAUDE_TASK") == "1" else [],
            "quickChecks": [],
            "regressionTests": [],
            "decisions": [f"TTL: {answer}"],
            "deferrals": [],
        }
    )


OUTPUTS = {
    # design-review.json は review.json と同じ形なので、どちらの名前で見分けても同じ結果を返す
    "design-review": {"findings": []},
    "review": {"findings": []},
    "design-judge": {"verdicts": [], "comments": [], "designCause": None},
    "write-overview": {"title": "何も変えない", "body": "# 概要\n\n変えるものは無かった。\n"},
    "write-pr-body": {"body": "# 概要\n\n変えるものは無かった。\n"},
}


def hang() -> int:
    """result を返さずに待つ。interrupt を受けたら打ち切って終わる。driver が落ちて標準入力が
    閉じても、ステージの途中の本物の claude のように走り続ける（検査が pid で止める）。"""
    stubborn = os.environ.get("FAKE_CLAUDE_STUBBORN") == "1"
    while line := sys.stdin.readline():
        if json.loads(line).get("type") == "control_request" and not stubborn:
            result(subtype="error_during_execution", is_error=True)
            while sys.stdin.readline():
                pass
            return 0
    time.sleep(60)
    return 1


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        sys.stdout.write("0.0.0 (fake)\n")
        return 0
    name = role(option(argv, "--json-schema"))
    resumed = "--resume" in argv
    # defer から続けるときはプロンプトが来ず、標準入力はすぐ閉じる
    prompt = prompt_of(sys.stdin.readline())
    found = _NOTICE.search(prompt)
    called = {
        "role": name,
        "resumed": resumed,
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "notice": json.loads(found.group(1)) if found else None,
    }
    with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(called, ensure_ascii=False) + "\n")
    emit({"type": "system", "subtype": "init", "session_id": "x", "capabilities": []})
    if os.environ.get("FAKE_CLAUDE_HANG") == name:
        return hang()
    if name == "plan":
        plan(resumed)
    elif name == "supervisor-run":
        result(structured_output=supervise(prompt))
    elif name == "supervisor-task":
        result(structured_output=supervise_task())
    elif name in OUTPUTS:
        result(structured_output=OUTPUTS[name])
    else:
        result(is_error=True, subtype="error_during_execution", result=f"知らない役: {name}")
    # result を見た側が標準入力を閉じるまで待つ（本物と同じく、閉じられてから終わる）
    while sys.stdin.readline():
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
