"""LLM のステージが結果の JSON で返した本文を、ランディレクトリに書き出す。

ステージには worktree の外を書かせないので、設計の本文・コードマップ・PR の本文は
結果で返させ、driver が書く。どれも一時ファイルから置き換える。書き出した在りかが、
成果物の実物になる（`ArtifactRef` の約束）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ...domain.value_objects.design_version import DesignVersion
from ...domain.value_objects.task_id import TaskId
from ...infra.files import write_atomic
from ...infra.paths import RunPaths


def next_design_version(paths: RunPaths) -> DesignVersion:
    """まだ使っていない設計の版（`design/v<版>.md` の一番大きい版の次）。版は使い回さない。"""
    used = [int(version.value) for version in paths.design_versions()]
    return DesignVersion(max(used, default=0) + 1)


def write_codemap(paths: RunPaths, body: str) -> None:
    write_atomic(paths.codemap, body)


def write_pr_body(paths: RunPaths, task: TaskId, body: str) -> None:
    write_atomic(paths.pr_body(task), body)


def write_overview_body(paths: RunPaths, body: str) -> None:
    write_atomic(paths.overview_body, body)


def write_overview_title(paths: RunPaths, title: str) -> None:
    write_atomic(paths.overview_title, title)


def write_awaiting(paths: RunPaths, task: TaskId, tests: Sequence[Any]) -> None:
    write_atomic(
        paths.awaiting_expectations(task), json.dumps(list(tests), ensure_ascii=False, indent=2)
    )


def write_result(path: Path, result: Mapping[str, Any]) -> None:
    """ステージの結果の JSON（`tasks/<TaskId>/results/<ExecutionId>.json`。Pointers が指す先）。"""
    write_atomic(path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
