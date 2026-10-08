"""フローの検査。統括が返したフローを、実行の前に確かめる。"""

from __future__ import annotations

from collections.abc import Iterable

from ..stages.catalog import REVIEWER_STAGES, STAGE_SPECS
from ..stages.kinds import SessionScope, StepArgument
from ..value_objects.artifact_kind import ArtifactKind
from ..value_objects.git_job import GitJob
from ..value_objects.stage_kind import StageKind
from ..value_objects.task_kind import TaskKind
from .flow import REQUIRED_AT_END, FlowStep
from .standard import git_job_flow


class FlowValidator:
    """統括が返したフローを、実行の前に検査する。

    落ちた理由をすべて集めて返す。統括に 1 つずつ差し戻すと、直すたびに次の理由で落ちて往復が増える。
    """

    @staticmethod
    def check(
        steps: Iterable[FlowStep],
        task_kind: TaskKind,
        available: Iterable[ArtifactKind] = (),
        job: GitJob | None = None,
    ) -> tuple[str, ...]:
        """空なら通る。`available` はタスクがすでに持っている成果物（フローを書き直しても残る）。

        git 管理タスクのフローは、取り出した仕事を 1 つ持ち、その仕事の決まった並び（`git_job_flow`）
        だけを受ける。ほかのタスクのフローは仕事を持たない。
        """
        steps = tuple(steps)
        if not steps:
            return ("フローが空",)
        reasons: list[str] = []
        for step in steps:
            reasons += _check_placement(step, task_kind)
            reasons += _check_arguments(step)
        reasons += _check_needs(steps, set(available), task_kind)
        reasons += _check_order(steps)
        reasons += _check_job(steps, task_kind, job)
        return tuple(dict.fromkeys(reasons))


def _check_job(steps: tuple[FlowStep, ...], task_kind: TaskKind, job: GitJob | None) -> list[str]:
    if task_kind is not TaskKind.GIT:
        return [] if job is None else ["仕事を持つのは git 管理タスクのフローだけ"]
    if job is None:
        return ["git 管理タスクのフローは、取り出した仕事を 1 つ持つ"]
    if steps != git_job_flow(job):
        return [f"{job.kind.value} の仕事の並びではない"]
    return []


def _check_placement(step: FlowStep, task_kind: TaskKind) -> list[str]:
    spec = STAGE_SPECS[step.stage]
    if spec.parent is not None:
        return [f"{step.stage.value} は {spec.parent.value} の中のステージで、フローに直に置けない"]
    if task_kind not in spec.task_kinds:
        return [f"{step.stage.value} は {task_kind.value} のタスクに置けない"]
    return []


def _check_arguments(step: FlowStep) -> list[str]:
    reasons: list[str] = []
    spec = STAGE_SPECS[step.stage]
    name = step.stage.value
    for argument in sorted(spec.required_arguments - step.arguments, key=lambda a: a.value):
        reasons.append(f"{name} に {argument.value} が無い")
    for argument in sorted(step.arguments - spec.arguments, key=lambda a: a.value):
        accepting = ", ".join(
            sorted(kind.value for kind, other in STAGE_SPECS.items() if argument in other.arguments)
        )
        reasons.append(f"{argument.value} を書けるのは {accepting} だけ（{name} に書いてある）")
    if step.reviewers is not None and StepArgument.REVIEWERS in spec.arguments:
        reasons += _check_reviewers("first", step.reviewers.first)
        if step.reviewers.later is not None:
            reasons += _check_reviewers("later", step.reviewers.later)
    if step.fresh_session and STAGE_SPECS[step.stage].session is not SessionScope.TASK:
        reasons.append(f"{name} はタスクの間続くセッションを持たないので、fresh_session を書けない")
    if step.instruction is not None and not step.instruction.strip():
        reasons.append(f"{name} の instruction が空。渡す言葉が無ければ書かない")
    return reasons


def _check_reviewers(which: str, reviewers: tuple[StageKind, ...]) -> list[str]:
    reasons: list[str] = []
    if not reviewers:
        reasons.append(f"reviewers の {which} が空")
    allowed = ", ".join(sorted(kind.value for kind in REVIEWER_STAGES))
    for kind in reviewers:
        if kind not in REVIEWER_STAGES:
            reasons.append(f"reviewers の {which} に書けるのは {allowed} だけ（{kind.value}）")
    if len(set(reviewers)) != len(reviewers):
        reasons.append(f"reviewers の {which} に同じステージが 2 つある")
    return reasons


def _check_needs(
    steps: tuple[FlowStep, ...], have: set[ArtifactKind], task_kind: TaskKind
) -> list[str]:
    reasons: list[str] = []
    produced: set[ArtifactKind] = set()
    for step in steps:
        spec = STAGE_SPECS[step.stage]
        if missing := spec.needs - have:
            names = ", ".join(sorted(kind.value for kind in missing))
            reasons.append(f"{step.stage.value} に要る {names} が、それより前に作られていない")
        have |= spec.produces
        produced |= spec.produces
    if missing := REQUIRED_AT_END.get(task_kind, frozenset()) - produced:
        names = ", ".join(sorted(kind.value for kind in missing))
        reasons.append(f"フローの終わりまでに {names} が作られない")
    return reasons


def _check_order(steps: tuple[FlowStep, ...]) -> list[str]:
    reasons: list[str] = []
    for index, step in enumerate(steps):
        spec = STAGE_SPECS[step.stage]
        earlier_stages = {earlier.stage for earlier in steps[:index]}
        for earlier in steps[:index]:
            if earlier.stage in spec.before:
                reasons.append(f"{step.stage.value} は {earlier.stage.value} より前に置く")
        if spec.returns_to is not None and spec.returns_to not in earlier_stages:
            reasons.append(
                f"{step.stage.value} は落ちたときに {spec.returns_to.value} へ戻るので、"
                f"{spec.returns_to.value} をそれより前に置く"
            )
    return reasons
