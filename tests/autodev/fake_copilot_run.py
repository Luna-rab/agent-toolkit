"""`autodev run` の検査で PATH に置く偽の `copilot`。本物は叩かない。

`fake_claude_run.py` の偽の claude と同じ決め打ちの結果を、`copilot -p --output-format json` の JSONL で返す。
copilot には出力スキーマのオプションが無いので、`-p` のプロンプトに入っているスキーマの本文を、`schemas/` の
各ファイルと照らして役を見分ける。

- Plan は、初めて起こされたら ask を呼び、パーク（フック）が拒んだ記録を足して SIGINT を待つ。
  `--resume` で起こされたら、`answers/<tool_use_id>.json` の回答を `decisions` に写して計画を返す
- ラン統括は、`fake_claude_run.supervise` と同じ答えを返す

`FAKE_COPILOT_SCHEMAS` に `schemas/` の置き場、`FAKE_COPILOT_LOG` に呼ばれ方を書き足すファイル、
`FAKE_COPILOT_QUOTA=1` なら、どの役も利用枠の上限の `session.error` を流して `result` を出さずに終わる。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from fake_claude_run import ASK_ID, OUTPUTS, supervise

ASK_COMMAND = "/skill/bin/autodev ask --question 'TTL は 60 秒か 300 秒か'"


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def message(body: object) -> None:
    emit(
        {
            "type": "assistant.message",
            "data": {"messageId": "m1", "content": json.dumps(body, ensure_ascii=False)},
        }
    )


def finished(session: str) -> None:
    emit(
        {
            "type": "result",
            "sessionId": session,
            "exitCode": 0,
            "usage": {"premiumRequests": 1},
        }
    )


def option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def role(prompt: str) -> str:
    """プロンプトの中の JSON の object のうち、`schemas/` のどれかと等しいものの名前。"""
    schemas = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(Path(os.environ["FAKE_COPILOT_SCHEMAS"]).glob("*.json"))
    }
    decoder = json.JSONDecoder()
    start = prompt.find("{")
    while start >= 0:
        try:
            found, _ = decoder.raw_decode(prompt, start)
        except ValueError:
            found = None
        if isinstance(found, dict):
            for name, schema in schemas.items():
                if found == schema:
                    return name
        start = prompt.find("{", start + 1)
    return "unknown"


def earlier_role(session: str) -> str | None:
    """同じセッションを前に起こしたときの役。再開のプロンプトにはスキーマが入らないことがある。"""
    path = Path(os.environ["FAKE_COPILOT_LOG"])
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("session") == session and entry.get("role") not in (None, "unknown"):
            return entry["role"]
    return None


def log(entry: dict) -> None:
    with open(os.environ["FAKE_COPILOT_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def stop_on_sigint() -> None:
    # SIGINT を受けたら abort を流して終了コード 0 で終わる（実測の形）。result は出さない
    def handler(signum, frame):
        emit({"type": "abort", "data": {"reason": "user_abort"}})
        os._exit(0)

    signal.signal(signal.SIGINT, handler)


def ask_and_wait() -> int:
    """ask を呼び、フックが拒んで控えた記録を足してから、止められるのを待つ。"""
    emit(
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "call_1",
                "toolName": "bash",
                "arguments": {"command": ASK_COMMAND},
            },
        }
    )
    record = {
        "hook": "park-on-ask",
        "tool": "Bash",
        "denied": True,
        "tool_use_id": ASK_ID,
        "question": "TTL は 60 秒か 300 秒か",
        "command": ASK_COMMAND,
        "answered_from": None,
    }
    with open(os.environ["AUTODEV_HOOK_RECORD"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    stop_on_sigint()
    time.sleep(60)
    return 1


def plan_result() -> dict:
    # cwd は trees/overview。回答はランディレクトリの answers/ にある
    answer_file = Path.cwd().parent.parent / "answers" / f"{ASK_ID}.json"
    answer = json.loads(answer_file.read_text(encoding="utf-8"))["answer"]
    return {
        "design": "# 設計\n\n変えるものは無い。\n",
        "codemap": "# コードマップ\n\n- a.txt\n",
        "tasks": [],
        "quickChecks": [],
        "regressionTests": [],
        "decisions": [f"TTL: {answer}"],
        "deferrals": [],
    }


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        log({"argv": argv})
        sys.stdout.write("GitHub Copilot CLI 0.0.0 (fake)\n")
        return 0
    resumed = "--resume" in argv
    session = option(argv, "--resume") or option(argv, "--session-id") or "none"
    prompt = option(argv, "-p") or ""
    name = role(prompt)
    if name == "unknown" and resumed:
        name = earlier_role(session) or name
    log(
        {
            "argv": argv,
            "role": name,
            "resumed": resumed,
            "cwd": os.getcwd(),
            "prompt": prompt,
            "session": session,
            "copilot_home": os.environ.get("COPILOT_HOME"),
        }
    )
    emit({"type": "session.start", "data": {"sessionId": session}})
    if os.environ.get("FAKE_COPILOT_QUOTA") == "1":
        emit(
            {
                "type": "session.error",
                "data": {
                    "errorType": "quota",
                    "errorCode": "quota_exceeded",
                    "message": "You have no remaining premium requests.",
                },
            }
        )
        return 0
    if name == "plan" and not resumed:
        return ask_and_wait()
    if name == "plan":
        message(plan_result())
    elif name == "supervisor-run":
        message(supervise(prompt))
    elif name in OUTPUTS:
        message(OUTPUTS[name])
    else:
        emit(
            {
                "type": "session.error",
                "data": {"errorType": "query", "message": f"知らない役: {name}"},
            }
        )
        return 1
    finished(session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
