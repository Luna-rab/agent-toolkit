"""`Review` を rich で描き、ANSI の色付きの文字列にする。

描いた文字列は、ユーザーの画面では `Stop hook feedback:` の後ろにそのまま出る。**Markdown は
描かれず、ANSI の色と罫線は描かれる**（実測）。同じ文字列が Claude にも渡るので、描き方を変えても
基準の文は削らない。一覧を罫線と色で目立たせ、基準は薄い色にして目が一覧に向くようにする。

**`additionalContext` は 10KB を超えると、Claude には先頭の 2KB しか渡らない**（残りはファイルに
移される）。だから大きさを抑える描き方にする。

- 罫線は左の縦線だけにする。右の縦線を引くと、各行を右端まで空白で埋めることになる
- 色は 256 色にする。truecolor の `38;2;r;g;b` は `38;5;n` の倍の長さになる
- 一覧は表にせず 1 件 1 行にする。表は列の幅を揃えるために空白で埋め、長いパスがあると見出しの列が
  細くなって何行にも割れる

1 行目は `Stop hook feedback:` の直後に来るので、罫線ではなく見出しの 1 行にする。

**文は自分で折り返してから rich に渡す。** rich は空白で折り返すので、日本語の文に混ざった
半角空白（「OS の違い」「1 の答え」）で行が切れ、右側が大きく空く。`fold` は「。」「、」の後ろで
切り、行が幅の半分に届かないときだけ文字の位置で切る。
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.console import Console
from rich.style import Style
from rich.text import Text

from turnreview.core.review import Review, Section
from turnreview.render import theme

#: 左の縦線と、その後ろの空白
RAIL = 2
#: 画面で 1 行目の頭に付く `Stop hook feedback: ` の幅
FEEDBACK_PREFIX = cell_len("Stop hook feedback: ")
#: 行頭に置かない約物。来たら前の行の最後の 1 文字と一緒に次の行へ送る
NO_LINE_START = frozenset("、。，．）」』】〉》・：；！？")
#: 折り返すときに、この文字の後ろで切る。前の組ほど優先する。空白は英単語の切れ目のためで、
#: 「2 で」のような数字と助詞の間で切らないよう、句読点が無いときだけ使う
BREAK_AFTER = (frozenset("。"), frozenset("、"), frozenset(" "))


def break_point(line: str, width: int) -> int:
    """行の中で切る位置（その文字の直後）。幅の半分より前でしか切れないなら 0 を返す。"""
    for marks in BREAK_AFTER:
        for index in range(len(line) - 1, 0, -1):
            if cell_len(line[: index + 1]) * 2 < width:
                break
            if line[index] in marks:
                return index + 1
    return 0


def fold(text: str, width: int) -> str:
    """表示幅に収まるよう折り返す。句読点の後ろで切り、切ると短くなりすぎるときだけ文字の位置で切る。

    折り返した行の頭の空白は落とす。
    """
    lines: list[str] = []
    line = ""
    for char in text:
        if cell_len(line + char) <= width:
            line += char
            continue
        cut = break_point(line, width)
        rest = line[cut:].lstrip() if cut else ""
        # 句読点の後ろで切ると行頭に約物が来るなら、句読点では切らない
        if cut and (rest or char not in NO_LINE_START):
            lines.append(line[:cut].rstrip())
            line = (rest + char).lstrip()
            continue
        if char == " ":
            lines.append(line)
            line = ""
            continue
        carry = ""
        # 送った文字も行頭に置けない（「」）」の「）」など）なら、もう 1 文字さかのぼる
        while char in NO_LINE_START and len(line) > 1 and (not carry or carry[0] in NO_LINE_START):
            line, carry = line[:-1], line[-1] + carry
        lines.append(line)
        line = carry + char
    lines.append(line)
    return "\n".join(lines)


class Frame:
    """左の縦線で囲んだ 1 つの塊を、行の並びとして組む。"""

    def __init__(self, width: int, border: Style) -> None:
        self.width = width
        self.border = border
        self.lines: list[Text] = []

    def top(self, title: str, tail: Text | None = None) -> None:
        line = Text.assemble(("╭─ ", self.border), (title, theme.BOLD))
        if tail is not None:
            line.append(" · ", theme.DIM)
            line.append_text(tail)
        self.lines.append(line)

    def divider(self) -> None:
        self.lines.append(Text("├─", style=self.border))

    def text(self, body: str, style: Style) -> None:
        for part in fold(body, self.width - RAIL).splitlines():
            self.lines.append(Text.assemble(("│ ", self.border), (part, style)))

    def row(self, place: str, label: str, note: str) -> None:
        """`path:line  見出し  添え書き` の 1 行。収まらなければ見出しから下を字下げして次の行へ送る。"""
        tail = f"  {note}" if note else ""
        if cell_len(f"{place}  {label}{tail}") <= self.width - RAIL:
            self.lines.append(
                Text.assemble(
                    ("│ ", self.border),
                    (place, theme.BLUE),
                    ("  ", ""),
                    (label, theme.TEXT),
                    (tail, theme.DIM),
                )
            )
            return
        self.lines.append(Text.assemble(("│ ", self.border), (place, theme.BLUE)))
        for part in fold(label + tail, self.width - RAIL - 2).splitlines():
            self.lines.append(Text.assemble(("│   ", self.border), (part, theme.TEXT)))

    def bottom(self) -> None:
        self.lines.append(Text("╰─", style=self.border))


def section_frame(section: Section, width: int, *, compact: bool) -> list[Text]:
    frame = Frame(width, theme.MAUVE)
    action = theme.ACTION_STYLE.get(section.action, theme.ACCENT)
    tail = Text(section.action, style=action) if section.action else None
    frame.top(f"{section.title}（{len(section.rows)} {section.unit}）", tail)
    for row in section.rows:
        if compact:
            frame.lines.append(Text.assemble(("│ ", frame.border), (row.place, theme.BLUE)))
        else:
            frame.row(row.place, row.label, row.note)
    if section.rules:
        frame.divider()
        for rule in section.rules:
            frame.text(rule, theme.DIM)
    frame.bottom()
    return frame.lines


def rules_frame(title: str, rules: tuple[str, ...], width: int) -> list[Text]:
    frame = Frame(width, theme.DIM)
    frame.top(title)
    for rule in rules:
        frame.text(rule, theme.DIM)
    frame.bottom()
    return frame.lines


def reply_frame(review: Review, width: int) -> list[Text]:
    frame = Frame(width, theme.YELLOW)
    frame.top("見直し終えたら")
    frame.text(review.reply_format, theme.YELLOW + theme.BOLD)
    for rule in review.reply_rules:
        frame.text(rule, theme.DIM)
    frame.bottom()
    return frame.lines


def lines_of(review: Review, width: int, *, compact: bool = False) -> list[Text]:
    lead = theme.MARK + " turnreview"
    first, *rest = fold(review.title, width - FEEDBACK_PREFIX - cell_len(lead + theme.ARROW)).split(
        "\n"
    )
    lines = [Text.assemble((lead, theme.ACCENT), (theme.ARROW, theme.DIM), (first, theme.BOLD))]
    lines.extend(Text(part, style=theme.BOLD) for part in rest)
    for section in review.sections:
        lines.extend(section_frame(section, width, compact=compact))
    lines.extend(rules_frame(review.common_title, review.common_rules, width))
    lines.extend(reply_frame(review, width))
    for note in review.notes:
        lines.extend(
            Text(part, style=theme.RED) for part in fold(theme.NOTE_MARK + note, width).splitlines()
        )
    return lines


def render(review: Review, width: int, *, color: bool = True, compact: bool = False) -> str:
    """`compact` なら一覧を `path:line` だけにする。件数が多くて上限を超えるときに使う。"""
    # 標準出力は端末ではないので、色を付けるよう明示する。折り返しは `fold` で済ませてある
    console = Console(
        force_terminal=color,
        color_system="256" if color else None,
        width=10_000,
        soft_wrap=True,
        highlight=False,
        markup=False,
        emoji=False,
    )
    with console.capture() as captured:
        for line in lines_of(review, width, compact=compact):
            console.print(line)
    return captured.get().rstrip()
