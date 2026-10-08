"""決定的なステージの種類から中身を引く表（`PROGRAMS`）と、中身を流す入口（`run_program`）。"""

from __future__ import annotations

from collections.abc import Mapping

from ....domain.value_objects.stage_kind import StageKind
from ..stage_context import StageContext
from .common import Program, ProgramOutcome, Tools
from .discard import close_prs, relink, unstack
from .implementation import confirm_red, gate
from .overview import create_overview_pr, ready_overview, refresh_overview
from .planning import prepare
from .stacking import (
    check_union,
    create_pr,
    cut_branch,
    integration_check,
    push,
    rebase,
    stack_link,
)

_S = StageKind


#: 決定的なステージの種類 → 中身
def run_program(ctx: StageContext, tools: Tools) -> ProgramOutcome:
    """ステージの中身を流す。宣言があれば、途中の rebase を先に取りやめ（`StageSpec.abandons_rebase`）、
    始めた時点へ戻す（`StageSpec.restores_start`）。取りやめてから戻すのは、rebase の途中で reset すると
    rebase の状態が残るためである。

    戻すのは `reset --keep` で、`--hard` と clean は使わない。初めて流すときは HEAD が始めた時点なので
    何も変わらず、汚れた worktree はステージの中身（git rebase）が断って落ちる。黙って消すと気づけない。
    流し直しは中身を終えた後なので、追跡していないファイルは残っていない。
    """
    spec, tree = ctx.spec, ctx.tree
    if (tree / ".git").exists():
        if spec.abandons_rebase:
            tools.git.rebase_abort(tree)
        if spec.restores_start and ctx.start_commit is not None:
            tools.git.reset_keep(tree, str(ctx.start_commit))
    return PROGRAMS[ctx.execution.stage](ctx, tools)


PROGRAMS: Mapping[StageKind, Program] = {
    _S.PREPARE: prepare,
    _S.CONFIRM_RED: confirm_red,
    _S.GATE: gate,
    _S.CUT_BRANCH: cut_branch,
    _S.REBASE: rebase,
    _S.CHECK_UNION: check_union,
    _S.INTEGRATION_CHECK: integration_check,
    _S.PUSH: push,
    _S.CREATE_PR: create_pr,
    _S.STACK_LINK: stack_link,
    _S.REFRESH_OVERVIEW: refresh_overview,
    _S.CREATE_OVERVIEW_PR: create_overview_pr,
    _S.READY_OVERVIEW: ready_overview,
    _S.CLOSE_PRS: close_prs,
    _S.UNSTACK: unstack,
    _S.RELINK: relink,
}
