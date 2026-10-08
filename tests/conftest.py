"""テストが共有する置き場の定数。

パッケージ（`autodev`・`hud`・`turnreview`）はインストール済みなので、`sys.path` は足さない。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
#: パッケージの置き場
SRC_ROOT = REPO_ROOT / "src"
#: autodev のパッケージ。指示書（`contracts/`）・スキーマ・テンプレートもここにある
AUTODEV_PACKAGE = SRC_ROOT / "autodev"
#: autodev のスキル（`SKILL.md`・`README.md`・`DOMAIN.html`）。コードは置かない
AUTODEV_SKILL = REPO_ROOT / "config" / "shared" / "skills" / "autodev"
HUD_PACKAGE = SRC_ROOT / "hud"
TURNREVIEW_PACKAGE = SRC_ROOT / "turnreview"
