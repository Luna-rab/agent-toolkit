"""statusline の左の行と、autodev のタスクリストを並べる。"""

from __future__ import annotations

from rich.text import Text

from hud.render.parts import Part, clip, fit, natural_width
from hud.render.theme import SIDE


def layout(rows: list[list[Part]], block: list[Text], columns: int) -> list[Text]:
    """タスクリストは、幅が足りれば左の行の右に、足りなければ下に置く。

    右に置くときは、左の列の幅を空白で埋めないと右の列がそろわない。そのため幅を縮めると、
    次に描き直すまで右の列が切れて見える（`refreshInterval` の秒数だけ）。
    """
    if not block:
        return [fit(row, columns) for row in rows]
    left_width = max(natural_width(row) for row in rows)
    right_width = max(line.cell_len for line in block)
    if left_width + SIDE.cell_len + right_width > columns:
        return [fit(row, columns) for row in rows] + [clip(line, columns) for line in block]
    left = [fit(row, left_width) for row in rows]
    out = []
    for i in range(max(len(left), len(block))):
        line = clip(left[i], left_width, pad=True) if i < len(left) else Text(" " * left_width)
        line.append_text(SIDE)
        if i < len(block):
            line.append_text(block[i])
        out.append(line)
    return out
