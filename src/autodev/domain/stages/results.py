"""ステージの結果の JSON を、ドメインの値（`value_objects/stage_result.py` の `StageResult`）に読み替える。

読むのは `StageSpec.result` が宣言した欄だけで、欄の名前（JSON のキー）は `ResultField` の値。LLM の
ステージの形は `schemas/<ステージ>.json` が決め、検査（`test_contracts.py`）が宣言と照らす。決定的な
ステージの結果の JSON（PR 番号・切った worktree）は実行器が組む。

Task が ReportStageResult を受けたときに呼ぶ。読めなければ `InvalidValue` で、Task は形の誤りとして
StageFailed にする（構造化出力の検証に任せない）。

- 報告（`report`）は、成果物の実物を確かめる前に読むので、`read_report` で別に読む
- 提案の版は結果の JSON に無い。設計の本文（`design`）を `design/v<版>.md` に書き出した実行器が、
  `proposal` の成果物の在りかに版の番号を書く（`ArtifactRef` の約束）
- Expect の `defects`（受入条件と食い違う出力）は、Expect を出どころとする must-fix の指摘に読み替える
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from ..value_objects.artifact_kind import ArtifactKind
from ..value_objects.base import InvalidValue
from ..value_objects.branch_name import BranchName
from ..value_objects.commit_sha import CommitSha
from ..value_objects.design_cause import DesignCause
from ..value_objects.design_judgement import DesignJudgement
from ..value_objects.design_version import DesignVersion
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.evidence import Evidence
from ..value_objects.finding_comment import FindingComment
from ..value_objects.finding_id import FindingId
from ..value_objects.finding_status import FindingStatus
from ..value_objects.finding_target import FindingTarget
from ..value_objects.finding_transfer import FindingTransfer
from ..value_objects.finding_verdict import FindingVerdict
from ..value_objects.location import Location
from ..value_objects.planned_task import PlannedTask
from ..value_objects.pr_number import PrNumber
from ..value_objects.proposal import Proposal
from ..value_objects.rating import Rating
from ..value_objects.reported_finding import ReportedFinding
from ..value_objects.stage_result import StageResult
from ..value_objects.stall_cause import StallCause
from ..value_objects.task_id import TaskId
from ..value_objects.task_spec import TaskSpec
from ..value_objects.verify_command import VerifyCommand
from ..value_objects.worktree_cut import WorktreeCut
from .catalog import StageSpec
from .kinds import ResultField

_F = ResultField
T = TypeVar("T")


def read_report(spec: StageSpec, result: Mapping[str, Any] | None) -> tuple[EscalationKind, str]:
    """返した報告と、その中身。呼ぶ側が先に `has_report` で、報告を返したかを見る。"""
    assert result is not None and has_report(spec, result)
    value = result[_F.REPORT.value]
    try:
        kind = EscalationKind(value)
    except ValueError as e:
        raise InvalidValue(f"報告の種類が違う: {value!r}") from e
    reason = result.get(_F.REPORT_REASON.value)
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidValue(f"{_F.REPORT.value} を返したら {_F.REPORT_REASON.value} に中身を書く")
    return kind, reason


def has_report(spec: StageSpec, result: Mapping[str, Any] | None) -> bool:
    """報告を読むステージで、結果が報告を返したか。"""
    return (
        _F.REPORT in spec.result and result is not None and result.get(_F.REPORT.value) is not None
    )


def parse_result(
    spec: StageSpec, result: Mapping[str, Any] | None, evidence: Evidence
) -> StageResult:
    """宣言した欄を読む。宣言した欄が無い・形が違う・組で書く欄の片方だけがあるなら InvalidValue。"""
    fields = spec.result
    # 報告を返していない（has_report が偽のときに呼ばれる）のに、報告の中身だけがある
    if (
        _F.REPORT in fields
        and result is not None
        and result.get(_F.REPORT_REASON.value) is not None
    ):
        raise InvalidValue(f"{_F.REPORT.value} が無いのに {_F.REPORT_REASON.value} がある")
    if not fields - {_F.REPORT, _F.REPORT_REASON}:
        return StageResult()
    if result is None:
        raise InvalidValue("結果が無い")
    read = _Reader(result, fields)
    findings = read.list(_F.FINDINGS, _finding)
    findings += read.list(_F.DEFECTS, _defect)
    stall_cause = read.optional(_F.STALL_CAUSE, StallCause)
    stall_reason = read.optional(_F.STALL_REASON, _str)
    if _F.STALL_CAUSE in fields and (stall_cause is None) != (stall_reason is None):
        raise InvalidValue(
            f"{_F.STALL_CAUSE.value} と {_F.STALL_REASON.value} は、両方書くか両方 null にする"
        )
    if stall_reason is not None and not stall_reason.strip():
        raise InvalidValue(f"{_F.STALL_REASON.value} が空")
    return StageResult(
        findings=findings,
        comments=read.list(_F.COMMENTS, _comment),
        verdicts=read.list(_F.VERDICTS, _verdict),
        stall_cause=stall_cause,
        stall_reason=stall_reason,
        design_cause=read.optional(_F.DESIGN_CAUSE, _design_cause),
        proposal=_proposal(read, evidence) if _F.DESIGN in fields else None,
        unchanged=read.value(_F.UNCHANGED, _bool, default=False),
        pr=read.value(_F.PR, lambda v: PrNumber(_int(v)), default=None),
        worktree=_worktree(read) if _F.TREE in fields else None,
        onto=read.value(_F.ONTO, _commit, default=None),
    )


class _Reader:
    """宣言した欄だけを読む。宣言していない欄は、在っても読まない。"""

    def __init__(self, result: Mapping[str, Any], fields: frozenset[ResultField]) -> None:
        self.result = result
        self.fields = fields

    def raw(self, field: ResultField) -> Any:
        if field.value not in self.result:
            raise InvalidValue(f"結果に {field.value} が無い")
        return self.result[field.value]

    def value(self, field: ResultField, read: Callable[[Any], T], default: T) -> T:
        if field not in self.fields:
            return default
        return _at(field.value, read, self.raw(field))

    def optional(self, field: ResultField, read: Callable[[Any], T]) -> T | None:
        if field not in self.fields:
            return None
        raw = self.raw(field)
        return None if raw is None else _at(field.value, read, raw)

    def list(self, field: ResultField, read: Callable[[Any], T]) -> tuple[T, ...]:
        if field not in self.fields:
            return ()
        return _items(field.value, read, self.raw(field))


def _at(where: str, read: Callable[[Any], T], raw: Any) -> T:
    try:
        return read(raw)
    except (InvalidValue, ValueError, TypeError, KeyError) as e:
        raise InvalidValue(f"結果の {where} が読めない: {e}") from e


def _items(where: str, read: Callable[[Any], T], raw: Any) -> tuple[T, ...]:
    if not isinstance(raw, list):
        raise InvalidValue(f"結果の {where} は配列: {raw!r}")
    return tuple(_at(f"{where}[{index}]", read, item) for index, item in enumerate(raw))


def _object(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise InvalidValue(f"object が要る: {raw!r}")
    return raw


def _str(raw: Any) -> str:
    if not isinstance(raw, str):
        raise InvalidValue(f"文字列が要る: {raw!r}")
    return raw


def _int(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InvalidValue(f"整数が要る: {raw!r}")
    return raw


def _bool(raw: Any) -> bool:
    if not isinstance(raw, bool):
        raise InvalidValue(f"真偽値が要る: {raw!r}")
    return raw


def _strings(raw: Any) -> tuple[str, ...]:
    return _items("[]", _str, raw)


def _location(raw: Any) -> Location | None:
    return None if raw is None else Location(_str(raw))


def _finding(raw: Any) -> ReportedFinding:
    item = _object(raw)
    return ReportedFinding(
        Rating(item["rating"]),
        _str(item["body"]),
        _location(item["location"]),
        FindingTarget(item.get("target", FindingTarget.IMPL.value)),
    )


def _defect(raw: Any) -> ReportedFinding:
    """受入条件と食い違う出力を、直す者（Fix）への must-fix の指摘にする。位置はテストの名前。"""
    item = _object(raw)
    test, output, acceptance = _str(item["test"]), _str(item["output"]), _str(item["acceptance"])
    body = f"{test} の出力が受入条件と食い違う。出力: {output} / 受入条件: {acceptance}"
    try:
        location: Location | None = Location(test)
    except InvalidValue:
        location = None
    return ReportedFinding(Rating.MUST_FIX, body, location)


def _comment(raw: Any) -> FindingComment:
    item = _object(raw)
    return FindingComment(FindingId(_str(item["finding"])), _str(item["body"]))


def _verdict(raw: Any) -> FindingVerdict:
    item = _object(raw)
    target = item.get("target")
    return FindingVerdict(
        FindingId(_str(item["finding"])),
        FindingStatus(item["to"]),
        _str(item["comment"]),
        None if target is None else FindingTarget(target),
    )


def _design_cause(raw: Any) -> DesignJudgement:
    item = _object(raw)
    cause = DesignCause(item["kind"])
    reverted = item.get("revertedTo")
    if (cause is DesignCause.REVERTED) != (reverted is not None):
        raise InvalidValue("revertedTo を書くのは、前の版に戻ったときだけ")
    reason = _str(item["reason"])
    if not reason.strip():
        raise InvalidValue("reason が空")
    question = item.get("question")
    if (cause is DesignCause.AMBIGUOUS) != (question is not None):
        raise InvalidValue("question を書くのは、受入条件が曖昧なときだけ（曖昧なら必ず書く）")
    if question is not None and not _str(question).strip():
        raise InvalidValue("question が空")
    return DesignJudgement(
        cause,
        None if reverted is None else DesignVersion(_int(reverted)),
        reason=reason,
        question=question,
    )


def _planned(raw: Any) -> PlannedTask:
    item = _object(raw)
    spec = TaskSpec(
        title=_str(item["title"]),
        dod=_str(item["dod"]),
        acceptance=_strings(item["acceptance"]),
        scope=_strings(item["scope"]),
        entry_points=_strings(item["entryPoints"]),
        boundary=_str(item["boundary"]),
        task_tests=tuple(VerifyCommand(c) for c in _strings(item["taskTests"])),
    )
    blocked = [TaskId(_str(t)) for t in _items("blockedBy", lambda v: v, item["blockedBy"])]
    return PlannedTask(TaskId(_str(item["id"])), spec, frozenset(blocked))


def _transfer(raw: Any) -> FindingTransfer:
    item = _object(raw)
    return FindingTransfer(
        FindingId(_str(item["finding"])),
        TaskId(_str(item["fromTask"])),
        TaskId(_str(item["toTask"])),
    )


def _tasks(raw: Any) -> frozenset[TaskId]:
    return frozenset(TaskId(t) for t in _strings(raw))


def _proposal(read: _Reader, evidence: Evidence) -> Proposal:
    if not _str(read.raw(_F.DESIGN)).strip():
        raise InvalidValue("設計の本文が空")
    written = [p for p in evidence.products if p.kind is ArtifactKind.PROPOSAL]
    if len(written) != 1:
        raise InvalidValue("提案の本文を書き出した版（proposal の成果物）が無い")
    try:
        version = DesignVersion(int(written[0].at))
    except ValueError as e:
        raise InvalidValue(f"proposal の成果物の在りかは版の番号: {written[0].at!r}") from e
    return Proposal(
        design=version,
        tasks=read.list(_F.TASKS, _planned),
        quick_checks=tuple(VerifyCommand(c) for c in read.value(_F.QUICK_CHECKS, _strings, ())),
        regression_tests=tuple(
            VerifyCommand(c) for c in read.value(_F.REGRESSION_TESTS, _strings, ())
        ),
        stop=read.value(_F.STOP, _tasks, frozenset()),
        discard=read.value(_F.DISCARD, _tasks, frozenset()),
        carry=read.list(_F.CARRY, _transfer),
        decisions=read.value(_F.DECISIONS, _strings, ()),
        deferrals=read.value(_F.DEFERRALS, _strings, ()),
    )


def _worktree(read: _Reader) -> WorktreeCut:
    branch = read.optional(_F.BRANCH, lambda v: BranchName(_str(v)))
    task = read.value(_F.WORKTREE_TASK, lambda v: TaskId(_str(v)), default=None)
    if task is None:
        raise InvalidValue(f"結果に {_F.WORKTREE_TASK.value} が無い")
    base = read.value(_F.BASE, _commit, default=None)
    return WorktreeCut(task, _at(_F.TREE.value, _str, read.raw(_F.TREE)), branch, base)


def _commit(raw: Any) -> CommitSha:
    return CommitSha(_str(raw))
