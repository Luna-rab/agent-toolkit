"""検査で使う見本の値。型注釈から、どのクラスにも見本を 1 つ組む。

イベントとコマンドを足しても、往復の検査に見本を書き足さなくてよいようにするため。欄はすべて
埋める（既定値のある欄も、None でない値で埋める）。既定値のまま往復させると、その欄の変換を
確かめたことにならない。
"""

from __future__ import annotations

import dataclasses
import types
import typing
from enum import Enum
from typing import Any, Union

from autodev.domain.stages.catalog import STAGE_SPECS
from autodev.domain.stages.kinds import ResultField
from autodev.domain.value_objects.base import Number, Text
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.commit_sha import CommitSha
from autodev.domain.value_objects.decision import Decision
from autodev.domain.value_objects.decision_origin import DecisionOrigin
from autodev.domain.value_objects.design_version import DesignVersion
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.finding_id import FindingId
from autodev.domain.value_objects.glob_pattern import GlobPattern
from autodev.domain.value_objects.instruction import Instruction
from autodev.domain.value_objects.location import Location
from autodev.domain.value_objects.model_class import AgentKind, Effort, ModelChoice, ModelName
from autodev.domain.value_objects.parallel_limit import ParallelLimit
from autodev.domain.value_objects.pr_number import PrNumber
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.repository import Repository
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.seq import Seq
from autodev.domain.value_objects.session_id import SessionId
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.verify_command import VerifyCommand

TASK = TaskId("task2")

_F = ResultField

#: ステージの結果の JSON で、欄ごとの「何も挙げない」既定の値（schemas/ の形に合わせる）
RESULT_DEFAULTS: dict[ResultField, Any] = {
    _F.REPORT: None,
    _F.REPORT_REASON: None,
    _F.FINDINGS: [],
    _F.COMMENTS: [],
    _F.VERDICTS: [],
    _F.STALL_CAUSE: None,
    _F.STALL_REASON: None,
    _F.DESIGN_CAUSE: None,
    _F.DESIGN: "設計の本文",
    _F.TASKS: [],
    _F.QUICK_CHECKS: [],
    _F.REGRESSION_TESTS: [],
    _F.STOP: [],
    _F.DISCARD: [],
    _F.CARRY: [],
    _F.DECISIONS: [],
    _F.DEFERRALS: [],
    _F.CODEMAP: "コードの地図",
    _F.AWAITING_EXPECTATIONS: [],
    _F.DEFECTS: [],
    _F.UNCHANGED: False,
    _F.BODY: "本文",
    _F.TITLE: "タイトル",
    _F.PR: 7,
    _F.WORKTREE_TASK: "task1",
    _F.TREE: "trees/task1",
    _F.BRANCH: None,
    _F.BASE: "c" * 40,
    _F.ONTO: "d" * 40,
}


def stage_result(stage: StageKind, **fields: Any) -> dict[str, Any]:
    """そのステージが読む欄（StageSpec.result）をすべて持つ結果の JSON。`fields` は JSON のキーで上書きする。"""
    result = {field.value: RESULT_DEFAULTS[field] for field in STAGE_SPECS[stage].result}
    return {**result, **fields}


#: 包んだ値と、形に縛りのある組の見本
FIXED: dict[Any, Any] = {
    RunName: RunName("add-cache"),
    TaskId: TASK,
    QuestionId: QuestionId("q-scope"),
    SessionId: SessionId("0f8fad5b-d9cb-469f-a165-70867728950e"),
    CommitSha: CommitSha("a" * 40),
    FindingId: FindingId("R3"),
    BranchName: BranchName("stack/add-cache--task-2"),
    PrNumber: PrNumber(12),
    Seq: Seq(4),
    # 見本の StreamId は指摘の台帳にする（ReviewCommand・FindingOrigin が台帳しか受けない）
    StreamId: StreamId.review(TASK),
    EventId: EventId("task/task2#7"),
    CommandId: CommandId("task/task2#7/stall-policy/0"),
    Instruction: Instruction("キャッシュを足す"),
    Repository: Repository("/home/me/repo"),
    VerifyCommand: VerifyCommand("uv run pytest -q"),
    GlobPattern: GlobPattern("**/test_*.py"),
    Location: Location("src/cache.py:42"),
    DesignVersion: DesignVersion(2),
    ParallelLimit: ParallelLimit(3),
    ModelName: ModelName("claude-opus-5-5"),
    # agent が受けない effort の組は作れない（Enum を最後の値で埋めると codex に max が付く）。
    # agent=codex を往復で確かめられる組にする
    ModelChoice: ModelChoice(ModelName("gpt-5.5"), Effort.HIGH, AgentKind.CODEX),
    Decision: Decision("A にする", DecisionOrigin.USER, QuestionId("q-scope")),
    str: "本文",
    int: 2,
    bool: True,
}


def sample(tp: Any) -> Any:  # noqa: PLR0911  型の種類ごとの分岐
    if tp in FIXED:
        return FIXED[tp]
    origin = typing.get_origin(tp)
    if origin is Union or origin is types.UnionType:
        (arg,) = [a for a in typing.get_args(tp) if a is not type(None)]
        return sample(arg)
    if origin is tuple:
        return (sample(typing.get_args(tp)[0]),)
    if origin is frozenset:
        return frozenset({sample(typing.get_args(tp)[0])})
    if origin is dict:
        return {"verdict": "ok", "items": [1, "x", None]}
    if isinstance(tp, type) and issubclass(tp, Enum):
        # 先頭は既定値に使われやすいので、最後の値で埋める
        return list(tp)[-1]
    if isinstance(tp, type) and issubclass(tp, (Text, Number)):
        raise KeyError(f"FIXED に {tp.__name__} の見本が無い")
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        hints = typing.get_type_hints(tp)
        return tp(**{f.name: sample(hints[f.name]) for f in dataclasses.fields(tp) if f.init})
    raise TypeError(f"見本を組めない型: {tp}")
