"""autodev-watch の右ペイン。ラン・タスク・段の詳細。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from rich.console import Group
from rich.text import Text

from hud.core.headline import Headline
from hud.core.pipeline import Mark, Step, full_name
from hud.core.runs import (
    age,
    escalations,
    flow_of,
    moment,
    open_findings,
    questions,
    run_of,
    settled_counts,
    short,
    task_executions,
    task_label,
    tasks,
)
from hud.core.stagelist import OLDER, OUTSIDE, StageItem
from hud.render.parts import bar
from hud.render.tasklist import driver_notes, pipeline
from hud.render.theme import (
    ACCENT,
    BOLD,
    DIM,
    EXECUTION_LABEL,
    RED,
    STATUS_LABEL,
    YELLOW,
    status_mark,
)

PROGRESS_BAR_WIDTH = 20
#: ランの詳細に出す、拒んだコマンドの件数（新しいものから）
REJECTIONS_SHOWN = 3
#: git 管理タスクの仕事（`flow.job.kind`）の表示名
JOB_LABEL = {
    "cut-overview": "概要ブランチを切る",
    "cut-task": "タスクのブランチを切る",
    "cut-stack-top": "スタックの一番上を固定する",
    "open-overview": "概要 PR を開く",
    "rewrite-overview": "概要 PR を書き直す",
    "stack": "積む",
    "discard": "破棄する",
    "finish": "仕上げ",
}
#: 段に入らない実行（`StageItem.note`）の説明
NOTE_LABEL = {
    OLDER: "書き直す前のフローの実行",
    OUTSIDE: "今のフローの実行だが、step がどの段にも当たらない",
}
#: 設計の提案の状態（`plan.design.proposal.state`）の表示名
PROPOSAL_LABEL = {"judging": "ジャッジ中", "revising": "書き直し中", "awaiting": "回答待ち"}
#: 指摘の評価の色
RATING_STYLE = {"must-fix": RED, "should-fix": YELLOW, "nit": DIM}
#: 判断の履歴の出どころ（`notes[].origin`）の表示名
ORIGIN_LABEL = {"user": "ユーザーの回答", "run-supervisor": "ラン統括の回答"}


def run_detail(head: Headline, st: dict, now: dt.datetime) -> Group:
    """ランを選んだときの詳細。進み具合・回答待ちの質問・エスカレーション・スタック・計画。"""
    title = Text(head.run_name, style=BOLD).append(f" · {head.phase}", style=DIM)
    if head.waiting:
        title.append(" · 回答待ち", style=YELLOW)
    if head.overview_pr:
        title.append(f" · 概要 PR #{head.overview_pr}", style=DIM)
    parts: list[Any] = [title, run_meta(st, now)]
    notes = driver_notes(head)
    if head.panic_cause:
        notes.append(Text(f"パニックの原因: {head.panic_cause}", style=RED))
    if notes:
        parts.append(Text(" · ").join(notes))
    parts.append(Text())

    pct = head.stacked / head.total * 100 if head.total else 0.0
    progress = Text("進み具合  ", style=BOLD).append_text(bar(pct, PROGRESS_BAR_WIDTH))
    progress.append(f" {head.stacked}/{head.total} スタック済み", style=DIM)
    if head.escalated:
        progress.append(f" · エスカレーション中 {head.escalated}", style=YELLOW)
    parts.append(progress)
    parts += [task_line(task) for task in tasks(st)]
    parts.append(Text())

    asked = questions(st)
    if asked:
        parts.append(Text("回答を待っている質問", style=BOLD))
        for q in asked:
            parts.append(Text(f"{q.get('id')}", style=YELLOW).append(f"  {q.get('body') or ''}"))
        parts.append(Text())
    raised = escalations(st)
    if raised:
        parts += [Text("エスカレーション", style=BOLD), *map(run_escalation, raised), Text()]
    parts += [Text("スタック", style=BOLD), stack_text(st["stack"]), Text()]
    parts += [Text("計画", style=BOLD), plan_text(st["plan"])]
    if st["plan"]["findings"]:
        parts.append(Text("設計レビューの指摘", style=DIM))
        parts += findings_lines(st["plan"]["findings"])
    parts.append(Text())
    rejected = st["rejections"]
    if rejected:
        parts.append(Text(f"拒んだコマンド {len(rejected)} 件", style=BOLD))
        for r in rejected[-REJECTIONS_SHOWN:]:
            parts.append(Text(f"{r.get('type')} ", style=DIM).append(str(r.get("reason") or "")))
    return Group(*parts)


def run_meta(st: dict, now: dt.datetime) -> Text:
    run = run_of(st)
    out = Text(style=DIM)
    if run.get("repository"):
        out.append(f"{run['repository']} · base {run.get('base')}")
    started = age(run.get("started_at"), now)
    if started is not None:
        out.append(f" · 始めてから {short(started)}")
    if run.get("resumes"):
        out.append(f" · 呼び直し {run['resumes']} 回")
    updated = age(st.get("updated_at"), now)
    if updated is not None:
        # driver が生きているかの目安ではない。イベントを最後に確定してからの時間でしかない
        out.append(f" · 最後のイベントから {short(updated)}")
    return out


def run_escalation(e: dict) -> Text:
    line = Text(f"{e.get('id')} ", style=DIM).append(str(e.get("kind")), style=YELLOW)
    if e.get("task"):
        line.append(f"  {e['task']}", style=DIM)
    if e.get("for_user"):
        line.append("  /autodev の回答を待つ", style=YELLOW)
    if e.get("failures"):
        line.append(f"  統括が応じなかった {e['failures']} 回", style=RED)
    if e.get("reason"):
        line.append(f"\n    理由: {e['reason']}")
    if e.get("question"):
        line.append(f"\n    問い: {e['question']}")
    return line


def findings_lines(findings: list[dict]) -> list[Text]:
    """指摘。開いているものは本文と場所を評価の重い順に、開いていないものは状態ごとの件数だけ。"""
    out = []
    for f in open_findings(findings):
        style = RATING_STYLE.get(str(f["rating"]), DIM)
        line = Text(f"{f['id']} ", style=DIM).append(str(f["rating"]), style=style)
        line.append(f"  {f['body']}")
        if f.get("location"):
            line.append(f"  {f['location']}", style=DIM)
        if f.get("fixes"):
            line.append(f" · 直した {f['fixes']} 回", style=DIM)
        if f.get("stalled"):
            line.append(" · 停滞", style=RED)
        out.append(line)
    settled = settled_counts(findings)
    if settled:
        out.append(Text(" · ".join(f"{state} {n}" for state, n in settled), style=DIM))
    return out


def job_text(job: dict | None) -> str:
    if job is None:
        return ""
    kind = str(job.get("kind"))
    target = job.get("task") or job.get("branch") or ""
    return f"{JOB_LABEL.get(kind, kind)} {target}".rstrip()


def stack_text(stack: dict) -> Text:
    out = Text()
    overview = stack["overview"]
    if overview is not None:
        out.append(f"概要 PR #{overview.get('pr')} {overview.get('branch')}\n")
    for entry in stack["entries"]:
        out.append(f"  #{entry.get('pr')} {entry.get('task')} ", style="")
        out.append(f"{entry.get('branch')}\n", style=DIM)
    if stack.get("current"):
        out.append(f"処理中: {job_text(stack['current'])}\n", style=ACCENT)
    queue = [job_text(j) for j in stack["queue"]]
    if queue:
        out.append(f"順番待ち: {' / '.join(queue)}\n", style=DIM)
    if stack.get("parked"):
        out.append(f"戻す回数の上限で止めた: {job_text(stack['parked'])}\n", style=RED)
    if stack.get("paused"):
        out.append("再計画の間は積まない\n", style=YELLOW)
    if stack.get("cuts_pending"):
        out.append(f"破棄して閉じ終えていない {stack['cuts_pending']} 本\n", style=YELLOW)
    return out if out.plain else Text("（まだ積んでいない）\n", style=DIM)


def plan_text(plan: dict) -> Text:
    out = Text()
    if plan.get("planning"):
        out.append("計画が進んでいる\n", style=ACCENT)
    elif not plan.get("planned"):
        out.append("まだ計画を反映していない\n", style=DIM)
    design = plan["design"]
    versions = design["versions"]
    if versions:
        out.append(
            f"設計の版 {', '.join(map(str, versions))} · 確定 {design.get('settled')}"
            f" · 反映 {plan.get('applied_design')}\n",
            style=DIM,
        )
    proposal = design["proposal"]
    if proposal is not None:
        state = str(proposal.get("state"))
        out.append(f"提案 v{proposal.get('version')} {PROPOSAL_LABEL.get(state, state)}")
        out.append(f" r{proposal.get('round')}", style=DIM)
        if proposal.get("awaiting"):
            out.append(f" · {proposal['awaiting']}", style=YELLOW)
        out.append("\n")
    if plan.get("replans_without_stack"):
        out.append(f"1 本も積まずに続けた再計画 {plan['replans_without_stack']} 回\n", style=DIM)
    return out if out.plain else Text("（計画を反映した）\n", style=DIM)


def task_line(task: dict) -> Text:
    """タスクの一覧の 1 行（記号・id・件名・状態・PR）。"""
    status = str(task.get("status"))
    mark, mark_style, body_style = status_mark(status)
    line = Text("  ").append(mark, style=mark_style).append(f" {task.get('id', '?')} ", style=DIM)
    line.append(task_label(task), style=body_style)
    label = STATUS_LABEL.get(status, status)
    line.append(f"  {label}" + (f" · #{task['pr']}" if task.get("pr") else ""), style=DIM)
    return line


def task_detail(task: dict, steps: list[Step], now: dt.datetime) -> Group:
    """選んだタスクの詳細。段の並び・走っている実行・エスカレーション・指摘・spec。"""
    status = str(task.get("status"))
    title = Text(f"{task.get('id')} ", style=DIM).append(task_label(task), style=BOLD)
    meta = Text(f"{STATUS_LABEL.get(status, status)} · {task.get('kind')}", style=DIM)
    if task.get("pr"):
        meta.append(f" · PR #{task['pr']}", style=DIM)
    if task.get("branch"):
        meta.append(f" · {task['branch']}", style=DIM)
    parts: list[Any] = [title, meta, *relations(task), Text()]

    flow = flow_of(task)
    parts.append(Text("フロー", style=BOLD))
    if flow is None:
        parts.append(Text("（無い。始めていないか、組み直しを待っている）", style=DIM))
    else:
        parts.append(pipeline(steps))
        if flow.get("halted"):
            parts.append(Text("このフローは捨てた（置き換えを待つ）", style=YELLOW))
        if flow.get("job"):
            parts.append(Text(f"仕事: {job_text(flow['job'])}", style=ACCENT))
    parts.append(Text())

    parts += [Text("実行", style=BOLD), executions_text(task_executions(task), now)]
    raised = escalations(task)
    if raised:
        parts.append(Text("エスカレーション", style=BOLD))
        for e in raised:
            line = Text(f"{e.get('id')} ", style=DIM).append(str(e.get("kind")), style=YELLOW)
            if e.get("origin"):
                line.append(f"  {e['origin']}", style=DIM)
            parts.append(line)
        parts.append(Text())
    return Group(*parts, *review_sections(task))


def review_sections(task: dict) -> list[Any]:
    """タスクの指摘・完了チェックで落ちた項目・spec・判断の履歴。無いものの節は出さない。"""
    parts: list[Any] = []
    if task["findings"]:
        parts += [Text("指摘", style=BOLD), *findings_lines(task["findings"]), Text()]
    failures = last_gate_failures(task)
    if failures:
        parts.append(Text("完了チェックで落ちた項目", style=BOLD))
        for g in failures:
            parts.append(Text(f"{g.get('item')}", style=RED).append(f"  {g.get('reason') or ''}"))
        parts.append(Text())
    if task.get("spec"):
        parts += [*spec_lines(task["spec"]), Text()]
    if task["notes"]:
        parts.append(Text("判断の履歴", style=BOLD))
        for n in task["notes"]:
            origin = str(n.get("origin"))
            line = Text(ORIGIN_LABEL.get(origin, origin), style=DIM)
            if n.get("question"):
                line.append(f" {n['question']}", style=DIM)
            parts.append(line.append(f"  {n.get('text')}"))
    return parts


def last_gate_failures(task: dict) -> list[dict]:
    """今のフローで最後に始めた Gate の実行の、落ちた項目。通っていれば空。"""
    flow = flow_of(task)
    if flow is None:
        return []
    gates = [e for step in flow["steps"] for e in step["executions"] if e["stage"] == "Gate"]
    started = [e for e in gates if e["started_at"] is not None]
    if not started:
        return []
    # started_at は同じ書式（UTC の `...Z`）なので、文字列のまま比べられる
    return list(max(started, key=lambda e: e["started_at"])["gate_failures"])


def spec_lines(spec: dict) -> list[Text]:
    """タスクの spec（完了の定義・受入条件・範囲）。"""
    out = [Text("完了の定義", style=BOLD), Text(str(spec.get("dod") or ""))]
    for label, key in (("受入条件", "acceptance"), ("範囲", "scope")):
        items = spec.get(key) or []
        if items:
            out += [Text(label, style=BOLD), *(Text(f"- {item}") for item in items)]
    return out


def relations(task: dict) -> list[Text]:
    """ほかのタスクとの関係と、知らないと読み違える印。"""
    out = []
    blocked = task["blocked_by"]
    if blocked:
        out.append(Text(f"待っているタスク: {' '.join(map(str, blocked))}", style=DIM))
    if task.get("takes_over"):
        out.append(Text(f"{task['takes_over']} を引き継いだ", style=DIM))
    if task.get("superseded_by"):
        out.append(Text(f"{task['superseded_by']} に引き継がれた", style=DIM))
    if task.get("integration_failed"):
        out.append(Text("統合に失敗した", style=RED))
    if task.get("awaiting_requeue"):
        out.append(Text("下の PR を閉じ終えたら積み直す", style=YELLOW))
    return out


def executions_text(
    runs: list[dict] | tuple[dict, ...], now: dt.datetime, *, ending: bool = False
) -> Text:
    """実行を 1 行ずつ。`ending` なら、終えた実行に終了時刻・所要時間・終わった理由を添える。"""
    out = Text()
    for e in runs:
        out.append_text(execution_line(e, now))
        if ending:
            out.append_text(ending_text(e))
        out.append("\n")
    return out if out.plain else Text("（走っている実行は無い）\n", style=DIM)


def ending_text(e: dict) -> Text:
    """終えた実行の終了時刻（手元の時刻帯）・所要時間・終わった理由。終えていなければ空。"""
    ended = moment(e.get("ended_at"))
    if ended is None:
        return Text()
    out = Text(f" · {ended.astimezone():%H:%M:%S} に終えた", style=DIM)
    seconds = age(e.get("started_at"), ended)
    if seconds is not None:
        out.append(f" · {short(seconds)}", style=DIM)
    reason = e.get("end_reason") or e.get("interrupted_by")
    if reason:
        out.append(f" · {reason}", style=YELLOW)
    return out


def execution_line(e: dict, now: dt.datetime) -> Text:
    """実行 1 つ。走っていれば経過時間と進み具合を添える。"""
    status = str(e.get("status"))
    running = status == "running"
    line = Text("◼ " if running else "  ", style=ACCENT)
    line.append(f"{full_name(str(e.get('stage')))} r{e.get('round')} a{e.get('attempt')}")
    line.append(f"  {EXECUTION_LABEL.get(status, status)}", style=ACCENT if running else DIM)
    seconds = age(e.get("started_at"), now)
    if running and seconds is not None:
        line.append(f" · {short(seconds)}", style=DIM)
    progress = e.get("progress")
    if not isinstance(progress, dict):
        progress = {}
    if progress.get("turns"):
        line.append(
            f" · {progress['turns']}ターン {progress.get('lastTool') or ''}".rstrip(), style=DIM
        )
    if progress.get("hookDenials"):
        line.append(f" · フックが止めた {progress['hookDenials']} 回", style=YELLOW)
    return line


def stage_detail(item: StageItem, now: dt.datetime) -> Group:
    title = Text(item.label, style=BOLD)
    if item.note:
        title.append(f" · {NOTE_LABEL.get(item.note, item.note)}", style=DIM)
    if not item.runs:
        empty = "飛ばした" if item.mark is Mark.SKIPPED else "まだ走っていない"
        return Group(title, Text(), Text(empty, style=DIM))
    return Group(title, Text(), executions_text(item.runs, now, ending=True))


def broken_detail(name: str, error: str) -> Group:
    """status が読めなかったラン。読めない理由だけがある。"""
    return Group(Text(name, style=BOLD).append(" · 読めない", style=RED), Text(), Text(error))


def failure_detail(message: str) -> Text:
    return Text("autodev status を読めない\n\n", style=RED).append(message, style=DIM)
