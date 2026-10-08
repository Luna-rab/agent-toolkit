"""実装タスクの決定的なステージの中身（テストが落ちることの確認・ゲート）。"""

from __future__ import annotations

from ....domain.services.gate import GateEvaluator, GateEvidence
from ....domain.value_objects.artifact_kind import ArtifactKind
from ....domain.value_objects.artifact_ref import ArtifactRef
from ..stage_context import StageContext
from .common import ProgramOutcome, Tools, changes_since, head_of, own_commits, verify_tree

_A = ArtifactKind


def confirm_red(ctx: StageContext, tools: Tools) -> ProgramOutcome:
    """テストが実装の前に落ちるかを、そのタスクのテストで確かめる。"""
    head = head_of(tools, ctx.tree)
    return ProgramOutcome(
        products=(ArtifactRef(_A.RED_TESTS, head),), verify=verify_tree(ctx, tools, ctx.tree)
    )


def gate(ctx: StageContext, tools: Tools) -> ProgramOutcome:
    """完了チェックの証拠を集め、項目ごとの合否は GateEvaluator（ドメイン）に任せる。"""
    if ctx.gate is None:
        raise RuntimeError("Gate の事実が写されていない")
    tree = ctx.tree
    head = head_of(tools, tree)
    commits = own_commits(ctx, tools, tree)
    # どちらの一覧を見るか（TestGen の有無）は GateEvaluator が決めるので、在る分は両方集める
    tests = ctx.artifacts.get(_A.TESTS)
    since_tests = changes_since(tools, tree, tests.at) if tests is not None else ()
    changed = changes_since(tools, tree, str(ctx.base_commit)) if commits else ()
    verify = verify_tree(ctx, tools, tree)
    evidence = GateEvidence(
        commits=len(commits),
        open_findings=ctx.gate.open_findings,
        reviewers_expected=ctx.gate.reviewers_expected,
        reviewers_completed=ctx.gate.reviewers_completed,
        has_test_gen=ctx.gate.has_test_gen,
        changed_since_tests=since_tests,
        changed=changed,
        verify=verify,
    )
    report = GateEvaluator.evaluate(evidence).report
    return ProgramOutcome(
        products=(ArtifactRef(_A.GATED, head),),
        verify=verify,
        gate=report,
        commits=len(commits),
    )
