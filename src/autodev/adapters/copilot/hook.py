"""Copilot のガードのフックの入口（`python -I -m autodev.adapters.copilot.hook <handler>`）。

判定の規則は Claude のフック（`claude/guard.py`）と同じで、ここがするのは Copilot の preToolUse の入力
（`sessionId`・`cwd`・`toolName`・`toolArgs`）への当てはめと、記録の書き足しである。

- 拒むのは終了コード 2 と標準エラーの理由。記録は Codex と同じ `HookRecord`（読み手は `read_records`）
- Copilot のフック入力にはツール呼び出しの ID が無いので、ask は session と質問の本文から決めた id で
  回答のファイルと結ぶ
- `AUTODEV_GUARD` が無ければ何もせず通す。在るのに記録のファイルが無い・書けないときは判定せずに止める
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from ...domain.guard import AskVerdict
from ..claude.guard import (
    DENY_ASK,
    DENY_EXIT,
    DENY_UNAVAILABLE,
    DENY_UNREADABLE,
    GUARD_ENV,
    GuardUnavailable,
    HookReply,
    answer_path,
    check_tool,
    deny,
    describe,
    load,
    parse_ask,
)
from ..codex.hook import (
    ASK_ANSWERED_RULE,
    DENY_NO_RECORD,
    PARKED,
    HookRecord,
    _answer_text,
    _append,
    _record_path,
)

DENY_WRITES = "deny-writes"
PARK_ON_ASK = "park-on-ask"

DENY_WRITE_BASH = (
    "write_bash は送る中身を判定できないので、このステージでは使えません。"
    "コマンドは bash で呼んでください。"
)
_PATCH_KEYS = ("patch", "input", "command")


def ask_id(session_id: str, question: str) -> str:
    digest = hashlib.sha256(f"{session_id}\n{question}".encode()).hexdigest()
    return "copilot-" + digest[:40]


def _tool_args(raw: object) -> object:
    """`toolArgs` は object でも JSON の文字列でもよい。JSON でない文字列はそのまま返す。"""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def to_guard_call(payload: Mapping[str, object]) -> tuple[str, dict[str, Any]] | None:
    """Copilot のツールを `check_tool` の (tool, tool_input) へ写す。写さないツールは None。"""
    name = payload.get("toolName")
    args = _tool_args(payload.get("toolArgs"))
    if name == "apply_patch":
        if isinstance(args, Mapping):
            args = next((args[k] for k in _PATCH_KEYS if isinstance(args.get(k), str)), "")
        if not isinstance(args, str):
            raise ValueError("apply_patch の toolArgs がパッチの文字列でない")
        return "apply_patch", {"command": args}
    if name not in {"bash", "create", "edit"}:
        return None
    if not isinstance(args, Mapping):
        raise ValueError(f"{name} の toolArgs が object でない")
    if name == "bash":
        return "Bash", {"command": str(args.get("command") or "")}
    path = args.get("path")
    return ("Write" if name == "create" else "Edit"), {"file_path": path if path else ""}


def _deny_writes(
    payload: Mapping[str, object], environ: Mapping[str, str], tool: str
) -> tuple[HookReply, HookRecord]:
    if tool == "write_bash":
        return deny(DENY_WRITE_BASH), HookRecord(DENY_WRITES, tool, True)
    guard, context = load(environ)
    call = to_guard_call(payload)
    refusal = None
    if call is not None:
        cwd = payload.get("cwd")
        cwd = cwd if isinstance(cwd, str) else None
        refusal = check_tool(guard, context, *call, cwd)
    reply = deny(describe(refusal, context)) if refusal else HookReply()
    return reply, HookRecord(DENY_WRITES, tool, refusal is not None)


def _park(
    payload: Mapping[str, object], environ: Mapping[str, str], tool: str
) -> tuple[HookReply, HookRecord]:
    passed = HookReply(), HookRecord(PARK_ON_ASK, tool, False)
    if tool != "bash":
        return passed
    args = _tool_args(payload.get("toolArgs"))
    if not isinstance(args, Mapping):
        return passed
    command = str(args.get("command") or "")
    ask = parse_ask(command)
    if ask is None:
        return passed
    return _park_ask(payload, environ, tool, ask.question, command)


def _park_ask(
    payload: Mapping[str, object],
    environ: Mapping[str, str],
    tool: str,
    question: str,
    command: str,
) -> tuple[HookReply, HookRecord]:
    guard, context = load(environ)
    session = payload.get("sessionId")
    if not isinstance(session, str) or not session:
        reason = DENY_UNREADABLE.format(reason="sessionId が無い")
        return deny(reason), HookRecord(PARK_ON_ASK, tool, True)
    identifier = ask_id(session, question)
    if guard.judge_ask(answered=True) is AskVerdict.REFUSE:
        return deny(DENY_ASK), HookRecord(PARK_ON_ASK, tool, True, identifier)
    answer = answer_path(context.answers_dir, identifier)
    if answer.is_file():
        reply = deny(f"{ASK_ANSWERED_RULE}。回答:\n{_answer_text(answer)}")
        return reply, HookRecord(PARK_ON_ASK, tool, False, identifier, answered_from=identifier)
    record = HookRecord(PARK_ON_ASK, tool, True, identifier, question, command)
    return deny(PARKED), record


def run_copilot_hook(handler: str, stdin: str, environ: Mapping[str, str]) -> HookReply:
    """フックの入口が呼ぶ。ガードの下では、読めない入力・記録の不具合・例外は、どれも止める側に倒す。"""
    if not environ.get(GUARD_ENV):
        return HookReply()
    try:
        path = _record_path(environ)
    except OSError as error:
        return deny(DENY_NO_RECORD.format(reason=error))
    tool = ""
    try:
        payload = json.loads(stdin)
        if not isinstance(payload, dict):
            raise ValueError("JSON の object でない")
        name = payload.get("toolName")
        tool = name if isinstance(name, str) else ""
        if handler == DENY_WRITES:
            reply, record = _deny_writes(payload, environ, tool)
        elif handler == PARK_ON_ASK:
            reply, record = _park(payload, environ, tool)
        else:
            raise GuardUnavailable(f"知らないフック: {handler}")
    except GuardUnavailable as error:
        reply = deny(DENY_UNAVAILABLE.format(reason=error))
        record = HookRecord(handler, tool, True)
    except Exception as error:
        reply = deny(DENY_UNREADABLE.format(reason=f"{type(error).__name__}: {error}"))
        record = HookRecord(handler, tool, True)
    try:
        _append(path, record)
    except OSError as error:
        return deny(DENY_NO_RECORD.format(reason=error))
    return reply


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) != 1:
            raise ValueError(f"ハンドラ名を 1 つ渡す（{args!r}）")
        reply = run_copilot_hook(args[0], sys.stdin.read(), os.environ)
    except Exception as error:
        sys.stderr.write(f"ガードのフックを読み込めないので、呼び出しを止めました: {error}\n")
        return DENY_EXIT
    if reply.stdout:
        sys.stdout.write(reply.stdout + "\n")
    if reply.stderr:
        sys.stderr.write(reply.stderr + "\n")
    return reply.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
