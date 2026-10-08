"""Codex の transcript（rollout の JSONL）から、この turn に apply_patch で足した行を求める。

turn の区切りは `event_msg` の `user_message`。`response_item` の role が user のものは、
Codex が環境の説明や AGENTS.md を差し込むので区切りにしない。

Codex の Stop は `additionalContext` を受けないので、`decision: "block"` の `reason` で返す。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path

from turnreview.adapters.codex.patch import parse_patch
from turnreview.core import turn
from turnreview.ports import files, transcript

#: Codex の `reason` は続きの指示としてモデルへ渡る。ANSI の色コードが混ざるので使わない
COLOR = False
#: `apply_patch <<'EOF' ... EOF`。Codex はこの形のシェル呼び出しもパッチとして横取りして適用する
HEREDOC = re.compile(
    r"\bapply_?patch\s*<<-?\s*(['\"]?)(\w+)\1[ \t]*\n(.*?)\n[ \t]*\2[ \t]*(?:\n|$)", re.S
)


def is_user_message(entry: dict) -> bool:
    payload = entry.get("payload")
    return (
        entry.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "user_message"
    )


def arguments_of(payload: dict) -> dict:
    """`function_call` の arguments（JSON 文字列）を辞書にする。読めなければ空。"""
    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return {}
    return arguments if isinstance(arguments, dict) else {}


def patch_text(payload: dict) -> str | None:
    """`response_item` の payload が apply_patch の呼び出しなら、パッチ本文を返す。"""
    kind, name = payload.get("type"), payload.get("name")
    if kind == "custom_tool_call" and name == "apply_patch":
        return string_at(payload, "input")
    if kind != "function_call":
        return None
    arguments = arguments_of(payload)
    if name == "apply_patch":
        return string_at(arguments, "input")
    command = arguments.get("command")
    if name == "shell" and isinstance(command, list):
        if command[:1] == ["apply_patch"]:
            return command[1] if len(command) > 1 and isinstance(command[1], str) else None
        command = command[-1] if command and isinstance(command[-1], str) else None
    return heredoc_patch(command) if name in ("shell", "shell_command") else None


def heredoc_patch(script: object) -> str | None:
    """シェルのスクリプトが `apply_patch <<'EOF'` でパッチを渡していれば、その本文。"""
    found = HEREDOC.search(script) if isinstance(script, str) else None
    return found.group(3) if found else None


def string_at(mapping: dict, key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) else None


def workdir_of(payload: dict) -> str | None:
    """呼び出しの arguments の `workdir`。相対パスの起点になる。"""
    return string_at(arguments_of(payload), "workdir") or None


def patches_in_last_turn(entries: Iterable[dict]) -> list[tuple[str, str | None]]:
    """直近の user_message より後にある apply_patch の（本文, workdir）を、時系列順に返す。"""
    patches: list[tuple[str, str | None]] = []
    for entry in entries:
        if is_user_message(entry):
            patches.clear()
        elif entry.get("type") == "response_item" and isinstance(entry.get("payload"), dict):
            text = patch_text(entry["payload"])
            if text is not None:
                patches.append((text, workdir_of(entry["payload"])))
    return patches


def added_lines_by_file(
    patches: list[tuple[str, str | None]], cwd: Path, temp_roots: Iterable[Path]
) -> dict[Path, set[str]]:
    roots = tuple(temp_roots)
    result: dict[Path, set[str]] = {}
    for text, workdir in patches:
        base = cwd / workdir if workdir else cwd
        for patched in parse_patch(text):
            lines = turn.added_lines("\n".join(patched.removed), "\n".join(patched.added))
            turn.add_lines(result, patched.path, lines, base, roots)
    return result


def added_in_turn(payload: dict) -> tuple[dict[Path, set[str]], Path]:
    """この turn に apply_patch で足した行をファイルごとに返す。2 つ目は相対パスの起点。"""
    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    path = Path(str(payload.get("transcript_path") or ""))
    if not path.is_file():
        return {}, cwd
    patches = patches_in_last_turn(transcript.entries(path))
    return added_lines_by_file(patches, cwd, files.temp_roots()), cwd


def hook_output(payload: dict, message: str) -> dict:
    """stdout に出す JSON。"""
    return {"decision": "block", "reason": message}
