"""`agent-hud` の入口（`hud.cli`）。サブコマンドの割り当てと、インストールされたコマンドとしての起動。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

AGENT_HUD = str(Path(sys.executable).parent / "agent-hud")


def test_インストールされたagent_hudのヘルプは終了コード0():
    done = subprocess.run([AGENT_HUD, "--help"], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert "statusline" in done.stdout
    assert done.stdout.strip() != ""
