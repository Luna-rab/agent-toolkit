"""claude.ai の利用状況（`/usage` が見ているもの）を取り寄せ、キャッシュに置く。読んだものを解釈しない。

**公開された API ではない。** Claude Code の `/usage` が叩く URL を、Claude Code が保存した OAuth の
トークンで叩く。形が変わったり、トークンが切れていたりして読めなければ、前に取れたものを返し続ける。

statusline は 2 秒ごとに起動し直すので、毎回は取りに行かない。キャッシュが `TTL` 秒より古いときだけ
取りに行き、失敗しても取りに行った時刻は書き込んで、次の `TTL` 秒は取りに行かない。
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

URL = "https://api.anthropic.com/api/oauth/usage"
TTL = 60
TIMEOUT = 3


def claude_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def cache_path() -> str:
    xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(xdg, "claude-hud", "usage.json")


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def token() -> str | None:
    """Claude Code が保存した OAuth のアクセストークン。macOS ではキーチェーンに入るので取れない。"""
    data = read_json(os.path.join(claude_dir(), ".credentials.json"))
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    value = oauth.get("accessToken") if isinstance(oauth, dict) else None
    return value if isinstance(value, str) and value else None


def fetch(access_token: str) -> Any:
    req = urllib.request.Request(
        URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return json.load(res)
    except (OSError, ValueError):
        return None


def write_cache(path: str, entry: dict) -> None:
    """別のセッションの statusline が同時に読むので、一時ファイルに書いてから置き換える。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entry, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def usage(now: float) -> Any:
    """利用状況の JSON。一度も取れていなければ None。`now` は UNIX 時刻。"""
    path = cache_path()
    cached = read_json(path)
    if not isinstance(cached, dict):
        cached = {}
    if now - float(cached.get("checked_at") or 0) < TTL:
        return cached.get("data")
    access_token = token()
    if access_token is None:
        return cached.get("data")
    data = fetch(access_token) or cached.get("data")
    write_cache(path, {"checked_at": now, "data": data})
    return data
