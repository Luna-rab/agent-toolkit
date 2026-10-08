"""Codex のガードのフックの入口（`autodev hook deny-writes|park-on-ask`）。

判定の規則は Claude のフック（`claude/guard.py`）と同じで、ここがするのは Codex の入力への当てはめと
記録の書き足しである。

- Codex の PreToolUse は `tool_name` が `Bash` か `apply_patch`、`tool_input.command` にコマンド行か
  パッチの本文を渡す。拒むのは終了コード 2 と標準エラーの理由
- 拒んだ呼び出しは Codex の JSONL に出ない。だから呼ばれるたびに、`AUTODEV_HOOK_RECORD` のファイルへ
  `HookRecord` を JSON 1 行で書き足し、Codex の実装がそれを数える
- `AUTODEV_GUARD` が無いときは何もせず通す（普段の対話の Codex でも走るため）。在るのに記録のファイルが
  無い・書けないときは、判定せずに止める。記録を書かずに通すと、記録 0 件の確かめが信頼の問題と取り違える
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from ...domain.guard import AskVerdict
from ...domain.value_objects.guard import Guard
from ..claude.guard import (
    DENY_ASK,
    DENY_EXIT,
    DENY_UNAVAILABLE,
    DENY_UNREADABLE,
    GUARD_ENV,
    GuardContext,
    GuardUnavailable,
    HookReply,
    answer_path,
    deny,
    guard_writes,
    load,
    parse_ask,
)

HOOK_RECORD_ENV = "AUTODEV_HOOK_RECORD"
DENY_WRITES = "deny-writes"
PARK_ON_ASK = "park-on-ask"

DENY_NO_RECORD = (
    "フックの記録のファイル（" + HOOK_RECORD_ENV + "）が渡っていない・書けないので止めた"
    "（driver の不具合）: {reason}"
)
ASK_ANSWERED_RULE = "この質問の回答は届いている。autodev ask を呼び直さない"
PARKED = "質問を driver に渡した。回答が届くまで次のツールを呼ばずに待つ"


@dataclass(frozen=True)
class HookRecord:
    hook: str
    tool: str
    denied: bool
    tool_use_id: str | None = None
    question: str | None = None
    command: str | None = None
    answered_from: str | None = None


def _record_path(environ: Mapping[str, str]) -> str:
    path = environ.get(HOOK_RECORD_ENV)
    if not path:
        raise OSError(f"{HOOK_RECORD_ENV} が空か無い")
    # 判定の前に、書けることを確かめる
    with open(path, "a", encoding="utf-8"):
        pass
    return path


def _append(path: str, record: HookRecord) -> None:
    line = json.dumps(asdict(record), ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(line)


def _park(
    guard: Guard, context: GuardContext, payload: Mapping[str, object], records: str
) -> tuple[HookReply, HookRecord | None]:
    """ask の呼び出しの判定。ask でなければ (通す, None)。"""
    tool_input = payload.get("tool_input")
    if payload.get("tool_name") != "Bash" or not isinstance(tool_input, Mapping):
        return HookReply(), None
    command = str(tool_input.get("command") or "")
    ask = parse_ask(command)
    if ask is None:
        return HookReply(), None
    tool_use_id = payload.get("tool_use_id")
    try:
        if not isinstance(tool_use_id, str):
            raise ValueError("tool_use_id が無い")
        answer = answer_path(context.answers_dir, tool_use_id)
    except ValueError as error:
        return deny(DENY_UNREADABLE.format(reason=error)), HookRecord(PARK_ON_ASK, "Bash", True)
    if guard.judge_ask(answered=True) is AskVerdict.REFUSE:
        return deny(DENY_ASK), HookRecord(PARK_ON_ASK, "Bash", True, tool_use_id)
    found = _answer_for(context, answer, tool_use_id, ask.question, records)
    if found is not None:
        source, text = found
        reply = deny(f"{ASK_ANSWERED_RULE}。回答:\n{text}")
        return reply, HookRecord(PARK_ON_ASK, "Bash", False, tool_use_id, answered_from=source)
    record = HookRecord(PARK_ON_ASK, "Bash", True, tool_use_id, ask.question, command)
    return deny(PARKED), record


def _answer_for(
    context: GuardContext, answer: Path, tool_use_id: str, question: str, records: str
) -> tuple[str, str] | None:
    """届いている回答 (回答の tool_use_id, 本文)。この呼び出しの id の回答が先で、無ければ同じ質問を控えた前の記録の id。

    Codex は再開でツール呼び出しを作り直し、tool_use_id が変わる。質問の本文が違えば別の質問として扱う。
    """
    if answer.is_file():
        return tool_use_id, _answer_text(answer)
    for record in reversed(read_records(records)):
        if record.question != question or record.tool_use_id is None:
            continue
        try:
            earlier = answer_path(context.answers_dir, record.tool_use_id)
        except ValueError:
            continue
        if earlier.is_file():
            return record.tool_use_id, _answer_text(earlier)
    return None


def _answer_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    try:
        body = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(body, dict) and isinstance(body.get("answer"), str):
        return body["answer"]
    return raw


def run_codex_hook(handler: str, stdin: str, environ: Mapping[str, str]) -> HookReply:
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
        name = payload.get("tool_name")
        tool = name if isinstance(name, str) else ""
        guard, context = load(environ)
        if handler == DENY_WRITES:
            reply = guard_writes(guard, context, payload)
            record: HookRecord | None = HookRecord(
                DENY_WRITES, tool, reply.exit_code == DENY_EXIT, _id(payload)
            )
        elif handler == PARK_ON_ASK:
            reply, record = _park(guard, context, payload, path)
            record = record or HookRecord(PARK_ON_ASK, tool, False, _id(payload))
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


def _id(payload: Mapping[str, object]) -> str | None:
    found = payload.get("tool_use_id")
    return found if isinstance(found, str) else None


def read_records(path: str | os.PathLike[str], start: int = 0) -> list[HookRecord]:
    """`start` 行目から後ろの記録。無いファイルは []。読めない行は飛ばす（行数には数える）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            lines = _lines(stream.read())
    except OSError:
        return []
    found: list[HookRecord] = []
    for line in lines[max(start, 0) :]:
        try:
            body = json.loads(line)
            if not isinstance(body, dict):
                continue
            found.append(
                HookRecord(
                    hook=str(body["hook"]),
                    tool=str(body["tool"]),
                    denied=body["denied"] is True,
                    tool_use_id=_text(body.get("tool_use_id")),
                    question=_text(body.get("question")),
                    command=_text(body.get("command")),
                    answered_from=_text(body.get("answered_from")),
                )
            )
        except (ValueError, KeyError):
            continue
    return found


def _lines(text: str) -> list[str]:
    """記録の行。JSON の中の U+2028 などで割れないよう、改行（\\n）だけで分ける。"""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def count_records(path: str | os.PathLike[str]) -> int:
    """今のファイルの行数。無ければ 0。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return len(_lines(stream.read()))
    except OSError:
        return 0
