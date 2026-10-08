"""色と記号。hud（`scripts/hud/render/theme.py`）と同じ Catppuccin Mocha の色を使う。

**Nerd Font を前提にしない。** 既定のフォントにもある文字（罫線・`●` `▸` `※`）だけを使う。
"""

from __future__ import annotations

from rich.style import Style

DIM = Style(color="#6c7086")
TEXT = Style(color="#cdd6f4")
ACCENT = Style(color="#89b4fa", bold=True)
MAUVE = Style(color="#cba6f7")
BLUE = Style(color="#74c7ec")
GREEN = Style(color="#a6e3a1")
YELLOW = Style(color="#f9e2af")
RED = Style(color="#f38ba8")
BOLD = Style(bold=True)

#: 一覧に対して Claude がすることの色。表に無いものは ACCENT
ACTION_STYLE = {
    "消すのが基本": RED,
    "縮めるのが基本": YELLOW,
    "多めに残してよい": GREEN,
}

MARK = "●"
ARROW = " ▸ "
NOTE_MARK = "※ "
