"""git 管理タスクの決定的なステージのうち、概要 PR を作り・更新し・レビュー待ちにするものの中身。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from ....domain.value_objects.cut_point import CutPoint
from ....domain.value_objects.decision_origin import DecisionOrigin
from ....domain.value_objects.overview_pr_title import OverviewPrTitle
from ....domain.value_objects.task_status import TaskStatus
from .. import markers
from ..stage_context import StageContext, WaitingRow
from .common import (
    ProgramOutcome,
    Tools,
    bullets,
    job_branch,
    job_of,
    overview_pr_of,
    resolve_cut_point,
)

_STATUS_LABELS: Mapping[TaskStatus, str] = {
    TaskStatus.PENDING: "⏳ 未着手",
    TaskStatus.RUNNING: "🔄 作業中",
    TaskStatus.ESCALATED: "❓ 回答待ち",
    TaskStatus.GATED: "⏸️ 積む順番待ち",
    TaskStatus.STACKING: "🔄 積んでいる",
    TaskStatus.STACKED: "✅ 積んだ",
    TaskStatus.DROPPED: "⛔ 止めた",
    TaskStatus.SUPERSEDED: "↪️ 引き継がれた",
    TaskStatus.DISCARDED: "🗑️ 破棄した",
}
_ORIGIN_LABELS: Mapping[DecisionOrigin, str] = {
    DecisionOrigin.USER: "ユーザーの回答",
    DecisionOrigin.RUN_SUPERVISOR: "ラン統括の判断",
}
_NONE = "（なし）"


def _cell(text: str) -> str:
    """表の 1 マス。`|` と改行は表を崩すので、エスケープと `<br>` に置き換える。"""
    return " <br> ".join(line.strip() for line in text.strip().splitlines()).replace("|", "\\|")


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + " --- |" * len(header),
        *("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows),
    ]
    return "\n".join(lines)


def _quote(text: str) -> str:
    """起動時の指示を引用にして、本文と見分けられるようにする。空行も引用の中に残す。"""
    return "\n".join(f"> {line}".rstrip() for line in text.strip().splitlines())


def _waiting_section(rows: Sequence[WaitingRow]) -> str:
    """回答待ちは畳まずに見出しごと出す。待ちがあるあいだは概要 PR が draft のままなので、その理由を開いた
    所に置く。待ちが無ければ節ごと出さない。"""
    if not rows:
        return ""
    table = _table(
        ("対象", "種類", "理由"),
        [(str(r.task) if r.task else "ラン全体", f"`{r.kind}`", r.reason or "—") for r in rows],
    )
    return f"## 回答を待っていること\n\n{table}"


def _updated_at(clock: str) -> str:
    """更新した時刻を分までの UTC にする。読めない形なら、そのまま出す。"""
    try:
        at = datetime.fromisoformat(clock.replace("Z", "+00:00"))
    except ValueError:
        return clock
    if at.tzinfo is not None:
        at = at.astimezone(timezone.utc)
    return at.strftime("%Y-%m-%d %H:%M UTC")


def overview_values(ctx: StageContext, tools: Tools) -> dict[str, str]:
    """概要 PR のマーカーの中身。表のラベルに無い状態は、隠さずに値のまま出す。"""
    facts = ctx.overview
    tasks = [
        (
            str(row.task),
            row.title,
            _STATUS_LABELS.get(row.status, row.status.value),
            f"#{row.pr}" if row.pr is not None else "—",
        )
        for row in facts.tasks
    ]
    decisions = [(d, "計画") for d in facts.decisions]
    decisions += [(note.text, _ORIGIN_LABELS[note.origin]) for note in facts.notes]
    name = tools.setting.paths.name
    return {
        "tasks": _table(("タスク", "件名", "状態", "PR"), tasks) if tasks else _NONE,
        "waiting": _waiting_section(facts.waiting),
        "decisions": _table(("決めたこと", "出どころ"), decisions) if decisions else _NONE,
        "deferrals": bullets(list(facts.deferrals)),
        "instruction": _quote(tools.setting.instruction),
        "signature": f"<sub>autodev のラン `{name}` が {_updated_at(tools.clock())} に更新した</sub>",
    }


def _overview_body(ctx: StageContext, tools: Tools) -> str:
    """保存したマーカー入りの本文から、毎回埋め直す。"""
    path = tools.setting.paths.overview_body
    if not path.is_file():
        raise RuntimeError("概要 PR の本文（WriteOverview の結果）が無い")
    return markers.fill(path.read_text(encoding="utf-8"), overview_values(ctx, tools))


def _overview_title(tools: Tools) -> str:
    path = tools.setting.paths.overview_title
    if not path.is_file():
        raise RuntimeError("概要 PR のタイトル（WriteOverview の結果）が無い")
    return str(OverviewPrTitle.for_run(path.read_text(encoding="utf-8")))


def create_overview_pr(ctx: StageContext, tools: Tools) -> ProgramOutcome:
    """概要ブランチを push し、概要 PR を draft で作る。base との差分が 0 だと作れないので、空のコミットを
    1 つ載せる。同じブランチの PR があればそれを使う。"""
    job = job_of(ctx)
    branch = job_branch(job)
    git, tree = tools.git, tools.setting.paths.overview_tree
    base = job.base or tools.setting.base
    if git.commit_count(tree, resolve_cut_point(tools, CutPoint.for_overview(base))) == 0:
        git.commit_empty(tree, f"autodev: ラン {tools.setting.paths.name} の概要")
    git.push(tree, branch)
    found = tools.forge.find_pr(tree, branch)
    if found is not None:
        return ProgramOutcome(result={"pr": int(found.number.value)})
    number = tools.forge.create_pr(
        tree,
        base=base,
        head=branch,
        title=_overview_title(tools),
        body=_overview_body(ctx, tools),
        draft=True,
    )
    return ProgramOutcome(result={"pr": int(number.value)})


def refresh_overview(ctx: StageContext, tools: Tools) -> ProgramOutcome:
    tools.forge.edit_pr(
        tools.setting.paths.overview_tree,
        overview_pr_of(ctx),
        title=_overview_title(tools),
        body=_overview_body(ctx, tools),
    )
    return ProgramOutcome()


def ready_overview(ctx: StageContext, tools: Tools) -> ProgramOutcome:
    tools.forge.ready_pr(tools.setting.paths.overview_tree, overview_pr_of(ctx))
    return ProgramOutcome()
