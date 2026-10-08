"""Claude Code の transcript から、この turn に編集ツールで足した行を求める。

**Bash 越しの書き換えは見えない。** `sed -i`・heredoc・リダイレクトで書き換えた
分は transcript の `tool_input` に現れないので取りこぼす。

**SubagentStop では `agent_transcript_path` を読む。** `transcript_path` は親の transcript で、
サブエージェントの編集は載っていない。

`decision: "block"` ではなく `additionalContext` で返す。ループ防止（`stop_hook_active` と
8 連続打ち切り）は同じで、transcript の表示が hook エラーではなく feedback になる。
SubagentStop でもサブエージェントが続きを書く（実測）。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from turnreview.core import turn
from turnreview.ports import files, transcript

EDIT_TOOLS = ("Edit", "Write", "MultiEdit")
#: `Stop hook feedback:` の欄は ANSI の色を描く（実測）
COLOR = True


def transcript_field(payload: dict) -> str:
    """payload のどのキーに、この turn の transcript のパスがあるか。"""
    return "agent_transcript_path" if turn.is_subagent(payload) else "transcript_path"


def is_user_prompt(entry: dict) -> bool:
    """transcript の 1 行が人の発言か。turn の区切りに使う。

    tool_result だけの user エントリ（ツールの戻り値）と、Claude Code が差し込む
    isMeta 付きのエントリは区切りにしない。
    """
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return any(block.get("type") == "text" for block in content if isinstance(block, dict))
    return False


def edits_in_last_turn(entries: Iterable[dict]) -> list[dict]:
    """直近のユーザー発言より後にある Edit / Write / MultiEdit の入力を、時系列順に返す。"""
    edits: list[dict] = []
    for entry in entries:
        if is_user_prompt(entry):
            edits.clear()
            continue
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") in EDIT_TOOLS
                and isinstance(block.get("input"), dict)
            ):
                edits.append({"name": block["name"], **block["input"]})
    return edits


def added_lines_by_file(
    edits: list[dict], cwd: Path, temp_roots: Iterable[Path]
) -> dict[Path, set[str]]:
    roots = tuple(temp_roots)
    result: dict[Path, set[str]] = {}
    for edit in edits:
        raw_path = edit.get("file_path")
        if not isinstance(raw_path, str):
            continue
        name = edit["name"]
        lines: set[str] = set()
        if name == "Write":
            lines = turn.added_lines("", str(edit.get("content") or ""))
        elif name == "Edit":
            lines = turn.added_lines(
                str(edit.get("old_string") or ""), str(edit.get("new_string") or "")
            )
        elif name == "MultiEdit":
            for sub in edit.get("edits") or []:
                if isinstance(sub, dict):
                    lines |= turn.added_lines(
                        str(sub.get("old_string") or ""), str(sub.get("new_string") or "")
                    )
        turn.add_lines(result, raw_path, lines, cwd, roots)
    return result


def added_in_turn(payload: dict) -> tuple[dict[Path, set[str]], Path]:
    """この turn に編集ツールで足した行をファイルごとに返す。2 つ目は相対パスの起点。"""
    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    path = Path(str(payload.get(transcript_field(payload)) or ""))
    if not path.is_file():
        return {}, cwd
    edits = edits_in_last_turn(transcript.entries(path))
    return added_lines_by_file(edits, cwd, files.temp_roots()), cwd


def hook_output(payload: dict, message: str) -> dict:
    """stdout に出す JSON。"""
    return {
        "hookSpecificOutput": {
            "hookEventName": payload.get("hook_event_name", "Stop"),
            "additionalContext": message,
        }
    }
