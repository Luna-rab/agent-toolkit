"""色と記号。

**Nerd Font を前提にしない。** 既定のフォントにもある文字（`│` `━` `╾` `─` `┃`）だけを使う。
"""

from __future__ import annotations

from rich.style import Style

DIM = Style(color="#6c7086")
ACCENT = Style(color="#89b4fa", bold=True)
MAUVE = Style(color="#cba6f7")
GREEN = Style(color="#a6e3a1")
YELLOW = Style(color="#f9e2af")
RED = Style(color="#f38ba8")
BLUE = Style(color="#74c7ec")
MARK = Style(color="#cdd6f4", bold=True)
BOLD = Style(bold=True)

#: 同じ行に並べる部品の間
GAP = "   "
