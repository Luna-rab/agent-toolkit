"""statusline を通しで動かす。Claude Code と同じく、標準入力に JSON を渡して標準出力を読む。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from hud_samples import session, write_usage

#: インストールされた入口。`uv run pytest` ならこの仮想環境の bin にある
AGENT_HUD = Path(sys.executable).parent / "agent-hud"


def raw(stdin: str, tmp_path, columns: int) -> subprocess.CompletedProcess[str]:
    # 本物の OAuth トークンで利用状況を取りに行かないよう、Claude Code の設定とキャッシュを空の場所に向ける
    env = {
        **os.environ,
        "COLUMNS": str(columns),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    return subprocess.run(
        [str(AGENT_HUD), "statusline"],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def run(tmp_path, columns: int = 100, data: dict | None = None) -> list[str]:
    """Claude Code が表示する行。Claude Code は各行の前後の空白を削り、空になった行を捨てる。"""
    out = raw(json.dumps(data or session(time.time())), tmp_path, columns)
    assert out.returncode == 0, out.stderr
    shown = [line.strip() for line in out.stdout.strip().split("\n") if line.strip()]
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in shown]


def test_セッションは使用量_場所_利用枠2本の4行(tmp_path):
    lines = run(tmp_path)
    assert lines[:2] == ["Opus 5.5 · high   ctx ━━━━━───── 50%   $3.21", "dotfiles"]
    assert lines[2].startswith("5h ")
    assert lines[2].endswith(" 72% ▲12 2h00m")
    # 窓の 6 割が過ぎた位置（40 マスの 23 番目）に目盛りを置く
    assert lines[2][3:].index("┃") == 23
    assert lines[3] == "7d ━━━━━━━━━━━━╾─────────────────────────── 31%"


def test_クレジットが有効なら月次の棒を利用枠の下に出す(tmp_path):
    write_usage(tmp_path / "cache" / "claude-hud" / "usage.json", time.time())
    lines = run(tmp_path)
    assert len(lines) == 5
    assert lines[4].startswith("mo ")
    assert " 1% $5.64/$800 " in lines[4]


def test_幅で埋めないので縮めても行頭は残る(tmp_path):
    """幅を変えても描き直されないので、右寄せや行末の空白で幅を埋めてはいけない。"""
    assert all(line == line.rstrip() for line in run(tmp_path, columns=200))


def test_幅が足りないと優先度の低い部品から落とす(tmp_path):
    assert run(tmp_path, columns=40)[0] == "Opus 5.5 · high   ctx ━━━━━───── 50%"


def test_入力が壊れていても落ちない(tmp_path):
    assert raw("not json", tmp_path, 100).returncode == 0
