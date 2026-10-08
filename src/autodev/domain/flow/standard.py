"""計画タスクと git 管理タスクの統括（プログラム）が組む、決まった並びのフロー。"""

from __future__ import annotations

from ..stages.catalog import GIT_JOB_STAGES
from ..value_objects.git_job import GitJob
from ..value_objects.git_job_kind import GitJobKind
from ..value_objects.stage_kind import StageKind
from .flow import FlowStep


def git_job_flow(job: GitJob) -> tuple[FlowStep, ...]:
    """git 管理タスクの統括（プログラム）が、取り出した仕事から組む決まった並び。

    積み直す仕事（前に積んだブランチがある）は、新しい名前のブランチを切り直してから rebase する。
    仕上げは、概要 PR を draft から外すと決めたときだけ ReadyOverview を置く。
    """
    stages = list(GIT_JOB_STAGES[job.kind])
    if job.kind is GitJobKind.STACK and job.previous is not None:
        stages.insert(0, StageKind.CUT_BRANCH)
    if job.kind is GitJobKind.FINISH and not job.ready_overview:
        stages.remove(StageKind.READY_OVERVIEW)
    return tuple(FlowStep(stage) for stage in stages)


def planning_flow(settled_before: bool) -> tuple[FlowStep, ...]:
    """計画タスクの統括（プログラム）が組む決まった並び。

    設計が一度も確定していなければ（初回と、確定する前の再計画）Prepare → Plan から、確定した設計が
    あれば Replan から。
    """
    head = (StageKind.REPLAN,) if settled_before else (StageKind.PREPARE, StageKind.PLAN)
    return tuple(FlowStep(stage) for stage in (*head, StageKind.DESIGN_LOOP))
