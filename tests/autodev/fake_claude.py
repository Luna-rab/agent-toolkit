"""検査で `claude -p` の代わりに起動する偽物。本物は叩かない。

`FAKE_CLAUDE_SCENARIO` で振る舞いを選び、受けた引数・環境変数・標準入力を `FAKE_CLAUDE_RECORD` に
JSON で書く。流すイベントの形は本物の claude に合わせてある。
"""

from __future__ import annotations

import json
import os
import sys
import time


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def init() -> None:
    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "x",
            "capabilities": ["interrupt_cancel_queued_v1"],
        }
    )


def result(**fields: object) -> None:
    body = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "result": "done",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 7,
        },
        "total_cost_usd": 0.25,
        "permission_denials": [],
    }
    body.update(fields)
    emit(body)


received: list[str] = []


def record() -> None:
    path = os.environ.get("FAKE_CLAUDE_RECORD")
    if not path:
        return
    keep = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "AUTODEV_GUARD",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_CONFIG_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_KEY_1",
        "GIT_CONFIG_VALUE_1",
        "GIT_TERMINAL_PROMPT",
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "argv": sys.argv[1:],
                "cwd": os.getcwd(),
                "env": {k: os.environ[k] for k in keep if k in os.environ},
                "oauth": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
                "stdin": received,
            },
            fh,
            ensure_ascii=False,
        )


def read_line() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    received.append(line)
    record()
    return json.loads(line)


def drain() -> None:
    while read_line() is not None:
        pass


def success() -> None:
    read_line()
    init()
    emit(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {}}],
            },
        }
    )
    emit(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "is_error": True,
                        "content": "PreToolUse:Edit hook error: [python3 x]: 止めた",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "t0",
                        "is_error": True,
                        "content": [{"type": "text", "text": "PreToolUse:Bash hook error: 止めた"}],
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "is_error": True,
                        "content": "ふつうの失敗",
                    },
                ]
            },
        }
    )
    emit(
        {
            "type": "assistant",
            "message": {
                "id": "m2",
                "content": [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {}}],
            },
        }
    )
    emit({"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "…"}]}})
    init()
    result(structured_output={"ok": True}, stop_reason="end_turn")
    drain()


def empty_structured() -> None:
    read_line()
    result(result="スキーマを満たせない")
    drain()


def deferred() -> None:
    # 再開ではプロンプトが来ない（標準入力はすぐ閉じる）
    drain()
    result(
        stop_reason="tool_deferred",
        deferred_tool_use={
            "id": "toolu_01",
            "name": "Bash",
            "input": {"command": "/s/bin/autodev ask --question 'どちら?'"},
        },
    )


def no_result() -> None:
    sys.stderr.write("error: unknown option '--bogus'\n")
    sys.exit(1)


def init_then_crash() -> None:
    """セッションを開いてから、result を返さずに落ちた。"""
    init()
    sys.stderr.write("panic: 落ちた\n")
    sys.exit(1)


def session_not_found() -> None:
    """`--resume` に見つからないセッションを渡した。claude 2.1.288 で確かめた形（プロンプトの有無で
    変わらない）。init を出さず、`result` の欄の無い result を返して終了コード 1 で終わる。"""
    session = sys.argv[sys.argv.index("--resume") + 1]
    message = f"No conversation found with session ID: {session}"
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "duration_ms": 0,
            "is_error": True,
            "num_turns": 0,
            "stop_reason": None,
            "session_id": session,
            "total_cost_usd": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "permission_denials": [],
            "errors": [message],
        }
    )
    sys.stderr.write(message + "\n")
    sys.exit(1)


def max_turns() -> None:
    """claude 2.1.288 で `--max-turns 1` を超えた形。`result` の欄は無く、理由は `errors` に載る。"""
    read_line()
    init()
    emit(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "num_turns": 2,
            "stop_reason": "tool_use",
            "terminal_reason": "max_turns",
            "usage": {"input_tokens": 10, "output_tokens": 190},
            "total_cost_usd": 0.02,
            "permission_denials": [],
            "errors": ["Reached maximum number of turns (1)"],
        }
    )
    drain()
    sys.exit(1)


def result_and_errors() -> None:
    read_line()
    result(is_error=True, result="本文", errors=["診断"])
    drain()
    sys.exit(1)


def rate_limited() -> None:
    read_line()
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
    result(is_error=True, api_error_status=429, result="You've hit your limit")
    drain()
    sys.exit(1)


def assistant_error() -> None:
    """API のエラーで終わったターン。assistant のイベントの一番上に `error` が付く（claude 2.1.288 の
    実行ファイルの型の定義から読んだ形。実測ではない）。`error: "rate_limit"` は 429 のほかに、529 の
    過負荷や `model_blocked` にも付く。

    形は `FAKE_CLAUDE_ASSISTANT`（JSON）で決める: `text`（本文）、`parent`（`parent_tool_use_id`）、
    `recovered`（その後にふつうの応答が続いたか）、`result`（result を返すか）。
    """
    shape = json.loads(os.environ["FAKE_CLAUDE_ASSISTANT"])
    read_line()
    init()
    emit(
        {
            "type": "assistant",
            "message": {"id": "m1", "content": [{"type": "text", "text": shape["text"]}]},
            "parent_tool_use_id": shape.get("parent"),
            "error": "rate_limit",
        }
    )
    if shape.get("recovered"):
        emit(
            {
                "type": "assistant",
                "message": {"id": "m2", "content": [{"type": "text", "text": "続ける"}]},
                "parent_tool_use_id": None,
            }
        )
    if shape.get("result"):
        result(is_error=True, result=shape["text"])
        drain()
    sys.exit(1)


def overage_rejected_then_error() -> None:
    """ふつうの呼び出しにも、`overageStatus: rejected` の rate_limit_event が出る（段 6 の実測）。"""
    read_line()
    init()
    emit(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "rateLimitType": "five_hour",
                "overageStatus": "rejected",
                "overageDisabledReason": "org_level_disabled",
                "isUsingOverage": False,
            },
        }
    )
    result(is_error=True, subtype="error_during_execution", result="落ちた")
    drain()
    sys.exit(1)


def rate_event_then_success() -> None:
    """上限の知らせが届いても、成功で終わったなら上限には当たっていない。"""
    read_line()
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
    result(result="rate limit の話をした")
    drain()


def interruptible() -> None:
    read_line()
    init()
    while True:
        message = read_line()
        if message is None:
            return
        if message.get("type") == "control_request":
            # claude 2.1.288 で、ツールの実行中に止めた形（段 6 の p3a2.jsonl）
            result(
                subtype="error_during_execution",
                is_error=True,
                result=None,
                stop_reason="tool_use",
                terminal_reason="aborted_tools",
                errors=[
                    "[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"
                ],
            )
            drain()
            return


def stubborn() -> None:
    """interrupt を受けても result を返さない。"""
    read_line()
    init()
    while True:
        time.sleep(0.05)
        read_line()


def lingering() -> None:
    """result の後、標準入力を閉じても終わらない。"""
    read_line()
    result()
    time.sleep(60)


SCENARIOS = {
    "success": success,
    "empty-structured": empty_structured,
    "deferred": deferred,
    "no-result": no_result,
    "init-then-crash": init_then_crash,
    "session-not-found": session_not_found,
    "max-turns": max_turns,
    "result-and-errors": result_and_errors,
    "rate-limited": rate_limited,
    "rate-event-then-success": rate_event_then_success,
    "assistant-error": assistant_error,
    "overage-rejected-then-error": overage_rejected_then_error,
    "interruptible": interruptible,
    "stubborn": stubborn,
    "lingering": lingering,
}


if __name__ == "__main__":
    record()
    SCENARIOS[os.environ.get("FAKE_CLAUDE_SCENARIO", "success")]()
