"""git を叩く。"""

from __future__ import annotations

import subprocess


def status(cwd: str) -> str | None:
    """`git status --porcelain=v2 --branch` の出力。git の外や git が無ければ None。

    statusline は 2 秒ごとに起動し直すので、1 回で済むこのコマンドだけを叩く。
    """
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain=v2", "--branch"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None
