"""計画タスクの決定的なステージの中身。"""

from __future__ import annotations

from ....domain.value_objects.artifact_kind import ArtifactKind
from ....domain.value_objects.artifact_ref import ArtifactRef
from ....infra.files import write_atomic
from .. import markers
from ..stage_context import StageContext
from .common import ProgramOutcome, Tools, bullets

_A = ArtifactKind


def prepare(ctx: StageContext, tools: Tools) -> ProgramOutcome:
    """起動時の指示とリポジトリごとの設定から、ブリーフを組む（コードは読まない）。"""
    setting = tools.setting
    body = markers.fill(
        markers.template("brief"),
        {
            "instruction": setting.instruction,
            "quick-checks": bullets([f"`{c}`" for c in setting.quick_checks]),
            "regression-tests": bullets([f"`{c}`" for c in setting.regression_tests]),
            "test-paths": bullets([f"`{g}`" for g in setting.test_globs]),
            "protected-paths": bullets([f"`{g}`" for g in setting.protected_globs]),
            "untested-paths": bullets([f"`{g}`" for g in setting.untested_globs]),
        },
    )
    path = setting.paths.brief
    write_atomic(path, body)
    return ProgramOutcome(products=(ArtifactRef(_A.BRIEF, setting.paths.relative(path)),))
