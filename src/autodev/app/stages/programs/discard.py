"""git 管理タスクの決定的なステージのうち、積んだものを破棄するものの中身。"""

from __future__ import annotations

from ..stage_context import StageContext
from .common import ProgramOutcome, Tools, job_of, overview_pr_of
from .stacking import link_stack


def _cut_index(ctx: StageContext) -> int:
    """閉じる中で一番下の、積んだ列の中の位置。閉じるものが無ければ列の長さ。"""
    cut = job_of(ctx).cut_from
    entries = list(ctx.stack.entries)
    return entries.index(cut) if cut is not None and cut in entries else len(entries)


def close_prs(ctx: StageContext, tools: Tools) -> ProgramOutcome:
    """破棄したタスクの中で一番下の PR から上を、すべて閉じる（上から閉じる）。"""
    tree = tools.setting.paths.overview_tree
    for entry in reversed(ctx.stack.entries[_cut_index(ctx) :]):
        tools.forge.close_pr(tree, entry.pr)
    return ProgramOutcome()


def unstack(ctx: StageContext, tools: Tools) -> ProgramOutcome:
    """スタックを GitHub の上で解く。閉じた PR はスタックに残り、上の PR をマージできなくするため。"""
    tree = tools.setting.paths.overview_tree
    number = tools.forge.stack_number(tree, overview_pr_of(ctx))
    if number is not None:
        tools.forge.unstack(tree, number)
    return ProgramOutcome()


def relink(ctx: StageContext, tools: Tools) -> ProgramOutcome:
    """残した PR（概要 PR と、閉じた所より下）でスタックを作り直す。概要 PR だけなら、次に積むタスクの
    StackLink が作り直す（スタックは 2 本以上の PR から成る）。"""
    kept = [overview_pr_of(ctx), *(e.pr for e in ctx.stack.entries[: _cut_index(ctx)])]
    if len(kept) >= 2:
        link_stack(ctx, tools, kept)
    return ProgramOutcome()
