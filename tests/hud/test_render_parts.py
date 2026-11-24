"""行の部品（`hud/render/parts.py`）。"""

from __future__ import annotations

import pytest
from rich.text import Text

from hud.render import parts


@pytest.mark.parametrize(
    ("pct", "mark", "want"),
    [(50, None, "━━──"), (10, None, "╾───"), (100, None, "━━━━"), (50, 75, "━━─┃"), (0, 0, "┃───")],
)
def test_棒は罫線で0_5マスまで刻む(pct, mark, want):
    assert parts.bar(pct, 4, mark).plain == want


@pytest.mark.parametrize(("pct", "rgb"), [(0, "#a6e3a1"), (50, "#f9e2af"), (100, "#f38ba8")])
def test_使用率の色は緑から黄を経て赤へ変わる(pct, rgb):
    color = parts.pct_color(pct).color
    assert color is not None and color.get_truecolor().hex == rgb


def test_幅に収まらなければkeepの小さい部品から落とす():
    row = [
        parts.Part(Text("aaaa"), keep=2),
        parts.Part(Text("bb"), keep=0),
        parts.Part(Text("cc"), keep=1),
    ]
    assert parts.fit(row, 20).plain == "aaaa   bb   cc"
    assert parts.fit(row, 9).plain == "aaaa   cc"
    assert parts.fit(row, 3).plain == "aaaa"
