"""`autodev run` の検査で PATH に置く偽の `codex`。本物は叩かない。

`fake_claude_run.py` の偽の claude と同じ決め打ちの結果を、`codex exec --json` の JSONL で返す。
渡された `--output-schema` のファイルを、`schemas/` の各ファイルを `strict_schema` に通したものと照らして
役を見分ける。

- Plan は、初めて起こされたら ask を呼び、パーク（フック）が拒んだ記録を足して SIGINT を待つ。
  `exec resume` で起こされたら、`answers/<tool_use_id>.json` の回答を `decisions` に写して計画を返す
- ラン統括は、`fake_claude_run.supervise` と同じ答えを返す

`FAKE_CODEX_SCHEMAS` に `schemas/` の置き場、`FAKE_CODEX_LOG` に呼ばれ方を書き足すファイル、
`FAKE_CODEX_DROP_LEAD=1` なら、統括の初回の起動は thread.started の後で止まり、SIGINT で終了コード 1 になる。
`FAKE_CODEX_LOGIN` に `codex login status` の終了コード（既定 0）を渡す。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from fake_claude_run import ASK_ID, OUTPUTS, supervise

from autodev.adapters.codex.schema import strict_schema

ASK_COMMAND = "/skill/bin/autodev ask --question 'TTL は 60 秒か 300 秒か'"


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def message(body: object) -> None:
    emit(
        {
            "type": "item.completed",
            "item": {"id": "item_9", "type": "agent_message", "text": json.dumps(body)},
        }
    )


def completed() -> None:
    emit(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
            },
        }
    )


def option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def role(schema: dict) -> str:
    for path in Path(os.environ["FAKE_CODEX_SCHEMAS"]).glob("*.json"):
        if strict_schema(json.loads(path.read_text(encoding="utf-8"))) == schema:
            return path.stem
    return "unknown"


def log(entry: dict) -> None:
    with open(os.environ["FAKE_CODEX_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def ask_and_wait() -> int:
    """ask を呼び、フックが拒んで控えた記録を足してから、止められるのを待つ。"""
    emit(
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "command_execution", "command": ASK_COMMAND},
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
    # SIGINT を受けたら turn.completed を出さずに終了コード 1 で終わる（実測の形）
    signal.signal(signal.SIGINT, lambda signum, frame: os._exit(1))
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
        sys.stdout.write("codex-cli 0.0.0 (fake)\n")
        return 0
    if argv[:2] == ["login", "status"]:
        log({"argv": argv})
        return int(os.environ.get("FAKE_CODEX_LOGIN", "0"))
    resumed = argv[1] == "resume"
    stdin = sys.stdin.read()
    schema_file = option(argv, "--output-schema")
    assert schema_file is not None
    name = role(json.loads(Path(schema_file).read_text(encoding="utf-8")))
    thread = argv[argv.index("-") - 1] if resumed else f"thread-{os.getpid()}"
    log(
        {
            "argv": argv,
            "role": name,
            "resumed": resumed,
            "cwd": os.getcwd(),
            "stdin": stdin,
            "thread": thread,
        }
    )
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    if name == "plan" and not resumed:
        return ask_and_wait()
    if name == "supervisor-run" and not resumed and os.environ.get("FAKE_CODEX_DROP_LEAD") == "1":
        # thread.started の後で止まる。統括の timeout の SIGINT で、結果を出さずに終了コード 1 で終わる
        signal.signal(signal.SIGINT, lambda signum, frame: os._exit(1))
        time.sleep(60)
        return 1
    if name == "plan":
        message(plan_result())
    elif name == "supervisor-run":
        message(supervise(stdin))
    elif name in OUTPUTS:
        message(OUTPUTS[name])
    else:
        emit({"type": "turn.failed", "error": {"message": f"知らない役: {name}"}})
        return 1
    completed()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
