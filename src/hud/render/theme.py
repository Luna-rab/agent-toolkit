"""色と記号。

**Nerd Font を前提にしない。** 既定のフォントにもある文字（`│` `━` `╾` `─` `┃` `✔` `◼` `◻` `✘`）
だけを使う。
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

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
#: 左の statusline と右のタスクリストの間
SIDE = Text("  │  ", style=DIM)
#: ステージの並びの間
ARROW = Text(" › ", style=DIM)

#: タスクの状態（`tasks[].status`）ごとの記号・記号の色・件名の色。表に無い状態（pending）は ◻
STATUS_MARK: dict[str, tuple[str, Style, Style]] = {
    "running": ("◼", ACCENT, BOLD),
    "escalated": ("◼", YELLOW, YELLOW),
    "gated": ("◼", ACCENT, Style()),
    "stacking": ("◼", ACCENT, Style()),
    "stacked": ("✔", GREEN, DIM),
    "finished": ("✔", GREEN, DIM),
    "dropped": ("–", DIM, DIM),
    "superseded": ("–", DIM, DIM),
    "discarded": ("–", DIM, DIM),
}
PENDING_MARK = ("◻", DIM, Style())
STATUS_LABEL = {
    "pending": "未着手",
    "running": "実行中",
    "escalated": "エスカレーション中",
    "gated": "積む順番待ち",
    "stacking": "積んでいる",
    "stacked": "スタック済み",
    "dropped": "止めた",
    "superseded": "引き継がれた",
    "discarded": "破棄した",
    "finished": "終えた",
}
#: 実行の状態（`executions[].status`）の表示名
EXECUTION_LABEL = {
    "requested": "始めるのを待つ",
    "running": "実行中",
    "completed": "完了",
    "reported": "報告で終えた",
    "failed": "失敗",
    "interrupted": "中断",
    "deferred": "回答待ち",
    "abandoned": "捨てた",
    "restarted": "やり直した",
    "refused": "結果を受けなかった",
}


def status_mark(status: str) -> tuple[str, Style, Style]:
    return STATUS_MARK.get(status, PENDING_MARK)
