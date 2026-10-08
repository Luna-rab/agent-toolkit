"""決定的なステージの中身。

どれも、ドメインが決めたこと（フローの段・git 管理タスクの仕事の相手）をアダプタで実行し、起きた事実
（作った成果物・検証の結果・衝突したファイル・作った PR の番号）を `ProgramOutcome` で返すだけである。
合否と次の一手は、実行器が渡した証拠を見て Task が決める。落ちたら例外を投げ、
実行器がエラーの証拠にする。

2 回流しても同じ結果になるように作る。作り済みのブランチ・worktree・PR はそのまま使う。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from ....adapters.github.forge import Forge
from ....adapters.github.git import Git
from ....adapters.process import globs
from ....domain.services.gate import ChangedFile
from ....domain.services.verify import VerifySelector
from ....domain.value_objects.artifact_ref import ArtifactRef
from ....domain.value_objects.branch_name import BranchName
from ....domain.value_objects.cut_point import CutPoint
from ....domain.value_objects.gate_report import GateReport
from ....domain.value_objects.git_job import GitJob
from ....domain.value_objects.pr_number import PrNumber
from ....domain.value_objects.union_verdict import UnionVerdict
from ....domain.value_objects.verify_command import VerifyCommand
from ....domain.value_objects.verify_result import VerifyResult
from ..stage_context import RunSetting, StageContext


class VerifyRunner(Protocol):
    """ProcessRunner の受け口（検査で差し替える）。"""

    def run(self, command: VerifyCommand, cwd: str | Path) -> VerifyResult: ...


@dataclass(frozen=True)
class Tools:
    setting: RunSetting
    git: Git
    forge: Forge
    runner: VerifyRunner
    clock: Callable[[], str]


@dataclass(frozen=True)
class ProgramOutcome:
    #: 結果の JSON（StageSpec.result が宣言した欄。PR の番号・切った worktree）
    result: dict[str, Any] | None = None
    products: tuple[ArtifactRef, ...] = ()
    verify: tuple[VerifyResult, ...] = ()
    gate: GateReport | None = None
    union: UnionVerdict | None = None
    conflicts: tuple[str, ...] = ()
    #: そのブランチにだけあるコミットの数（数えたステージだけ）
    commits: int | None = None


Program = Callable[[StageContext, Tools], ProgramOutcome]


def own_commits(ctx: StageContext, tools: Tools, tree: Path) -> list[str]:
    """そのブランチにだけあるコミット（古い順）。タスクのブランチの根元（`Task.base_commit`）から HEAD まで。

    ほかのブランチから辿れるかでは決めない。上のタスクを切ったブランチは下のタスクのコミットを含むので、
    それを除くと 0 件になる。
    """
    if ctx.base_commit is None:
        raise RuntimeError(f"{ctx.execution} のタスクのブランチの根元（base_commit）が無い")
    return [str(c) for c in tools.git.commits_since(tree, str(ctx.base_commit))]


def changes_since(tools: Tools, tree: Path, since: str) -> tuple[ChangedFile, ...]:
    """`since` から変わったファイルと、glob との照合の答え（照合の規則はアダプタ）。"""
    setting = tools.setting
    return tuple(
        ChangedFile(
            path,
            is_test=globs.matches_any(path, setting.test_globs),
            untested_ok=globs.matches_any(path, setting.untested_globs),
        )
        for path in tools.git.changed_files(tree, since)
    )


def verify_tree(ctx: StageContext, tools: Tools, tree: Path) -> tuple[VerifyResult, ...]:
    """そのステージが流す検証コマンドを流し、`plan` が決めた種類を付けて返す。"""
    task_tests = ctx.task_spec.task_tests if ctx.task_spec is not None else ()
    plan = VerifySelector.plan(
        ctx.execution.stage,
        task_tests=task_tests,
        quick_checks=ctx.run_quick_checks,
        regression_tests=ctx.run_regression_tests,
    )
    return tuple(replace(tools.runner.run(command, tree), kind=kind) for command, kind in plan)


def head_of(tools: Tools, tree: Path) -> str:
    return str(tools.git.head(tree))


def resolve_cut_point(tools: Tools, point: CutPoint) -> str:
    """ドメインが並べた候補（`CutPoint.refs`）のうち、実在する最初の参照。"""
    git = tools.git
    *preferred, last = point.refs()
    return next((ref for ref in preferred if git.rev_parse(git.repo, ref) is not None), last)


def job_of(ctx: StageContext) -> GitJob:
    if ctx.job is None:
        raise RuntimeError(f"{ctx.execution} は git 管理タスクの仕事の中で走っていない")
    return ctx.job


def job_branch(job: GitJob) -> BranchName:
    if job.branch is None:
        raise RuntimeError(f"仕事 {job.id}（{job.kind.value}）にブランチが無い")
    return job.branch


def overview_pr_of(ctx: StageContext) -> PrNumber:
    if ctx.stack.overview is None:
        raise RuntimeError("概要 PR がまだ無い")
    return ctx.stack.overview.pr


def bullets(lines: Sequence[str]) -> str:
    return "\n".join(f"- {line}" for line in lines) if lines else "（なし）"
