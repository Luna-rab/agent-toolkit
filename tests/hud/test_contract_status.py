"""本物の `status --json` を statusline に渡す契約の検査。

ほかの HUD の検査は、手で書いた見本（`hud_samples.py`）を読む。見本と本物の形が
ずれても、それらは全部通る。ここでは、イベントの列から `autodev.infra.status.all_statuses` で
本物の JSON を組み立て、偽の入口に返させて statusline を通す。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from hud_samples import session, write_fake_entry

from autodev.domain.value_objects.run_name import RunName
from autodev.infra.paths import RunPaths
from autodev.infra.status.status import all_statuses
from conftest import REPO_ROOT

# 組み立ての道具は autodev の検査のものを使う（tests/autodev は sys.path に無い）
AUTODEV_TESTS = REPO_ROOT / "tests" / "autodev"
if str(AUTODEV_TESTS) not in sys.path:
    sys.path.insert(0, str(AUTODEV_TESTS))
# ty は上で足した置き場を知らないので解決できない
from test_status import Seed, seed_running  # noqa: E402  # ty: ignore[unresolved-import]


def test_本物のstatusを読んでstatuslineが描いて0で終わる(tmp_path):
    env = {"AUTODEV_STATE_DIR": str(tmp_path / "state")}
    seed_running(Seed(RunPaths.of(RunName("demo"), env)))
    statuses = all_statuses(env)
    assert [st["name"] for st in statuses] == ["demo"]

    out = subprocess.run(
        [str(Path(sys.executable).parent / "agent-hud"), "statusline"],
        input=json.dumps(session(time.time())),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            **write_fake_entry(tmp_path, statuses),
            "COLUMNS": "120",
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
        check=False,
    )
    assert out.returncode == 0, out.stderr
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", line).strip() for line in out.stdout.splitlines()]
    # seed_running のランでは、計画タスクの Replan と task1 の Impl が走っている
    head = next(line for line in lines if line.startswith("autodev demo ▸ "))
    assert "planning 再計画 r0 · task1 実装 r0" in head
    assert "回答待ち q1" in head
    # driver.lock を握るプロセスは無いが、回答待ちなので止まっているとは言わない
    assert "driver 停止" not in head
    assert any("◼ task1 パーサを足す" in line for line in lines), lines
