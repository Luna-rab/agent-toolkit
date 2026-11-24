"""テストが共有する置き場の定数。

パッケージ（`hud`・`toolkit`）はインストール済みなので、`sys.path` は足さない。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
#: パッケージの置き場
SRC_ROOT = REPO_ROOT / "src"
HUD_PACKAGE = SRC_ROOT / "hud"
