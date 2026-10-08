"""hook が返した色付きの本文を、文字だけで比べられる形にする。

行ごとに ANSI の色と枠の罫線を落とし、2 つ以上続く空白を 2 つに揃える。一覧の行は
`path:line  見出し  添え書き` の形になる。
"""

from __future__ import annotations

import re

ANSI = re.compile(r"\x1b\[[0-9;]*m")
FRAME = "│╭╮╰╯─ "


def plain(text: str) -> str:
    lines = (ANSI.sub("", line).strip(FRAME) for line in text.splitlines())
    return "\n".join(re.sub(r" {2,}", "  ", line) for line in lines if line)
