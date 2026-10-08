"""Copilot の起動の下ごしらえ（ログインの確かめ・プラグイン・環境変数）。"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..agent.types import AgentCall
from .hook import DENY_WRITES, PARK_ON_ASK

HOOK_TIMEOUT_SEC = 600
HOOK_MODULE = "autodev.adapters.copilot.hook"
#: `-m` は autodev を import できないと main に入る前に終了コード 1 で終わる。1 は呼び出しを通してしまうので、
#: 0 以外はすべて 2（止める）に揃える
FAIL_CLOSED = " || exit 2"
HOOK_RECORD_ENV = "AUTODEV_HOOK_RECORD"


def source_home(env: Mapping[str, str]) -> Path:
    found = env.get("COPILOT_HOME")
    return Path(found) if found else Path.home() / ".copilot"


def _strip_comments(text: str) -> str:
    """`//` と `/* */` のコメントを外す。文字列の中は残す。"""
    out: list[str] = []
    index, size = 0, len(text)
    in_string = False
    while index < size:
        char = text[index]
        pair = text[index : index + 2]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < size:
                out.append(text[index + 1])
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            out.append(char)
        elif pair == "//":
            while index < size and text[index] != "\n":
                index += 1
            continue
        elif pair == "/*":
            end = text.find("*/", index + 2)
            index = size if end < 0 else end + 2
            continue
        else:
            out.append(char)
        index += 1
    return "".join(out)


def read_config(path: Path) -> dict[str, Any]:
    """コメント付き JSON の設定。無ければ {}。読めない・object でなければ ValueError。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ValueError(f"{path} を読めない: {error}") from error
    try:
        body = json.loads(_strip_comments(text))
    except ValueError as error:
        raise ValueError(f"{path} が JSON として読めない: {error}") from error
    if not isinstance(body, dict):
        raise ValueError(f"{path} が JSON の object でない")
    return body


def has_login(source: Path) -> bool:
    config = read_config(source / "config.json")
    return bool(config.get("authTokens") or config.get("loggedInUsers"))


def _hook(handler: str) -> dict[str, Any]:
    command = shlex.join([sys.executable, "-I", "-m", HOOK_MODULE, handler]) + FAIL_CLOSED
    return {"type": "command", "bash": command, "timeoutSec": HOOK_TIMEOUT_SEC}


def write_plugin(directory: str | os.PathLike[str]) -> Path:
    """`--plugin-dir` に渡すディレクトリ。マニフェストと、preToolUse のフック定義を書く。"""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "autodev-guard",
        "description": "autodev のガード（書き込みの制限と ask の受け渡し）",
        "version": "1.0.0",
        "hooks": "hooks.json",
    }
    hooks = {"version": 1, "hooks": {"preToolUse": [_hook(DENY_WRITES), _hook(PARK_ON_ASK)]}}
    for name, body in (("plugin.json", manifest), ("hooks.json", hooks)):
        (root / name).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return root


def env_for(call: AgentCall, *, base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update(call.env)
    env[HOOK_RECORD_ENV] = f"{call.log_path}.hooks"
    return env
