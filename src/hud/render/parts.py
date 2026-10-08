"""行を組み立てる部品。棒・色の段階・幅に収める処理。"""

from __future__ import annotations

from dataclasses import dataclass

from rich.style import Style
from rich.text import Text

from hud.render.theme import DIM, GAP, MARK


@dataclass
class Part:
    """1 行を組み立てる部品。幅が足りないときは `keep` の小さいものから落とす。"""

    text: Text
    keep: int = 0


def pct_color(pct: float) -> Style:
    """0% の緑から 100% の赤へ、黄を経て連続に変える。"""
    p = max(0.0, min(pct, 100.0)) / 100
    green, yellow, red = (0xA6, 0xE3, 0xA1), (0xF9, 0xE2, 0xAF), (0xF3, 0x8B, 0xA8)
    lo, hi, t = (green, yellow, p * 2) if p < 0.5 else (yellow, red, (p - 0.5) * 2)
    rgb = "".join(f"{round(a + (b - a) * t):02x}" for a, b in zip(lo, hi, strict=True))
    return Style(color=f"#{rgb}")


def bar(pct: float, width: int, mark: float | None = None) -> Text:
    """0.5 マス刻みの棒。`mark`（0〜100）を渡すと、その位置に目盛り `┃` を置く。

    罫線（`━` `╾` `─`）で描く。マスの上下中央に引かれるので、棒を縦に並べても上下の行と
    接しない（ブロック要素 `█` はマスの高さいっぱいを塗るので、行どうしがくっつく）。
    """
    halves = round(max(0.0, min(pct, 100.0)) * width * 2 / 100)
    cells = ["━"] * (halves // 2) + ["╾"] * (halves % 2)
    filled = len(cells)
    cells += ["─"] * (width - filled)
    at = None if mark is None else min(width - 1, int(max(0.0, mark) * width / 100))
    out = Text()
    for i, cell in enumerate(cells):
        if i == at:
            out.append("┃", style=MARK)
        else:
            out.append(cell, style=pct_color(pct) if i < filled else DIM)
    return out


def joined(parts: list[Part], sep: str = GAP) -> Text:
    out = Text()
    for i, part in enumerate(parts):
        if i:
            out.append(sep)
        out.append_text(part.text)
    return out


def natural_width(parts: list[Part]) -> int:
    """何も落とさずに並べたときの幅。"""
    return joined([p for p in parts if p.text.plain]).cell_len


def fit(parts: list[Part], width: int) -> Text:
    """左から並べる。`width` に収まらなければ `keep` の小さい部品から落とす。

    **右寄せや行末の空白で幅を埋めない。** 端末の幅を変えても Claude Code は描き直さないので、
    次に描き直すまで前の幅の出力が残る。左から並べておけば、縮めたときに切れるのは行末だけで済む。
    """
    live = [p for p in parts if p.text.plain]
    while natural_width(live) > width and len(live) > 1:
        drop = min(range(len(live)), key=lambda i: (live[i].keep, -i))
        del live[drop]
    return joined(live)


def clip(text: Text, width: int, *, pad: bool = False) -> Text:
    out = text.copy()
    out.truncate(width, overflow="ellipsis", pad=pad)
    return out
