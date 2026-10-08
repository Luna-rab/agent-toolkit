"""autodev のタスクリスト。Claude Code のタスクリストのように、現在地・済んだもの・これからを出す。

autodev add-cache ▸ task2 ジャッジ r2 · 4m12s 7ターン Read · 概要 PR #4
  ✔ task1 パーサの土台を作る      #5
  ◼ task2 範囲指定を足す          テスト作成 ✔ › 実装 ✔ › ジャッジ r2 ◼ › 完了チェック › PR 本文
  ◻ task3 CLI に出す
"""

from __future__ import annotations

from rich.text import Text

from hud.core.headline import Headline, State
from hud.core.pipeline import Mark, Step
from hud.core.runs import RATINGS, escalations, open_findings, short, task_label
from hud.core.tasklist import Summary
from hud.render.parts import clip
from hud.render.theme import ACCENT, ARROW, DIM, GREEN, RED, YELLOW, status_mark

SUBJECT_WIDTH = 22
#: エスカレーション中のタスクの行に出す理由の字数
ESCALATION_WIDTH = 24
#: statusline の指摘の件数に添える評価の短い名前
RATING_SHORT = {"must-fix": "must", "should-fix": "should", "nit": "nit"}
#: statusline に出すパニックの原因の幅。全文は autodev-watch の詳細に出す
PANIC_CAUSE_WIDTH = 40
#: 段の並びを出すタスクの状態。積む順番を待つ間も、フローの最後の段まで見せる
PIPELINE_STATUSES = ("running", "gated", "stacking")


def headline(head: Headline) -> Text:
    line = Text("autodev ", style=DIM)
    line.append(head.run_name, style=ACCENT)
    line.append(" ▸ ", style=DIM)
    if head.state is State.PANICKED:
        line.append(f"{head.doing} · 呼び直すまで進まない", style=RED)
        if head.panic_cause:
            line.append(" · ").append_text(
                clip(Text(head.panic_cause, style=RED), PANIC_CAUSE_WIDTH)
            )
    elif head.state is State.RUNNING:
        line.append(head.doing)
        if head.elapsed is not None:
            line.append(f" · {short(head.elapsed)}", style=DIM)
        if head.turns:
            line.append(f" {head.turns}ターン {head.tool}".rstrip(), style=DIM)
    else:
        line.append(f"{head.doing} · スタック済み {head.stacked}/{head.total}", style=DIM)
    if head.waiting:
        line.append(f" · 回答待ち {' '.join(head.waiting)}", style=YELLOW)
    if head.escalated:
        line.append(f" · エスカレーション {head.escalated}", style=YELLOW)
    # 端末の幅で行末から切れるので、回答待ちより後ろに短く置く。呼び直し方は autodev-watch の詳細に出す
    for note in driver_notes(head, brief=True):
        line.append(" · ").append_text(note)
    if head.overview_pr:
        line.append(f" · 概要 PR #{head.overview_pr}", style=DIM)
    return line


def driver_notes(head: Headline, *, brief: bool = False) -> list[Text]:
    """driver が止まっている・前の driver の子が残っている。どちらも人が動くまで直らない。"""
    notes: list[Text] = []
    if head.stopped:
        notes.append(
            Text(
                "driver 停止"
                if brief
                else f"driver が止まっている（run --name {head.run_name} で呼び直す）",
                style=RED,
            )
        )
    if head.leftovers:
        pids = " ".join(map(str, head.leftovers))
        notes.append(
            Text(f"{'残った子' if brief else '前の driver の子が生きている'} pid {pids}", style=RED)
        )
    return notes


def pipeline(steps: list[Step]) -> Text:
    """段の並びを 1 行にする（`テスト作成 ✔ › 実装 ◼ › レビュー › 完了チェック › PR 本文`）。"""
    out = Text()
    for i, step in enumerate(steps):
        if i:
            out.append_text(ARROW)
        if step.mark is Mark.ELIDED:
            out.append("…", style=DIM)
        elif step.mark is Mark.DONE:
            out.append(f"{step.label} ", style=DIM).append("✔", style=GREEN)
        elif step.mark is Mark.SKIPPED:
            out.append(f"{step.label} –", style=DIM)
        elif step.mark is Mark.FAILED:
            out.append(f"{step.label} ", style=DIM).append("✘", style=RED)
        elif step.mark is Mark.CURRENT:
            out.append(f"{step.label} ◼", style=ACCENT)
        else:
            out.append(step.label, style=DIM)
    return out


def task_row(task: dict, steps: list[Step]) -> Text:
    """タスク 1 本の行。`steps` は段の並びを出す状態のときだけ使う。"""
    status = str(task.get("status"))
    mark, mark_style, body_style = status_mark(status)
    row = Text("  ")
    row.append(mark, style=mark_style)
    row.append(f" {task.get('id', '?')} ", style=DIM)
    detail = Text()
    if status == "stacked" and task.get("pr"):
        detail = Text(f"#{task['pr']}", style=DIM)
    elif status == "escalated":
        detail = Text(escalation_head(escalations(task)), style=YELLOW)
    elif status in PIPELINE_STATUSES:
        detail = pipeline(steps)
        if status == "running":
            detail.append_text(findings_count(task["findings"]))
    subject = Text(task_label(task), style=body_style)
    # 右に何か続くときだけ件名の幅をそろえる（続かない行の末尾に空白を残さない）
    row.append_text(clip(subject, SUBJECT_WIDTH, pad=bool(detail.plain)))
    if detail.plain:
        row.append("  ")
        row.append_text(detail)
    return row


def escalation_head(raised: list[dict]) -> str:
    """最初のエスカレーションの理由の頭。理由が空なら種類の名前（ask など）。

    文字数で切る。表示幅で切る `clip` だと、日本語の理由は半分の字数しか残らない。
    """
    if not raised:
        return "エスカレーション中"
    first = raised[0]
    reason = " ".join(str(first.get("reason") or "").split())
    if not reason:
        return str(first.get("kind"))
    return reason if len(reason) <= ESCALATION_WIDTH else reason[:ESCALATION_WIDTH] + "…"


def findings_count(findings: list[dict]) -> Text:
    """開いている指摘の評価ごとの件数（`  指摘 must 2 / should 3`）。開いていなければ空。"""
    opened = open_findings(findings)
    counts = [(r, sum(f["rating"] == r for f in opened)) for r in RATINGS]
    shown = [f"{RATING_SHORT[r]} {n}" for r, n in counts if n]
    return Text(f"  指摘 {' / '.join(shown)}", style=YELLOW) if shown else Text()


def summary_row(summary: Summary) -> Text:
    if summary.status == "stacked":
        return Text("  ✔ ", style=GREEN).append(f"{summary.count} 件完了", style=DIM)
    return Text("  ◻ ", style=DIM).append(f"他 {summary.count} 件", style=DIM)


def failure_row(message: str) -> Text:
    """`status --json` そのものが返らなかった。黙ると「ランが無い」と見分けが付かない。"""
    return Text("autodev status を読めない · ", style=RED).append(message, style=DIM)
