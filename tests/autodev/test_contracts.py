"""指示書（contracts/）・結果の形（schemas/）・PR 本文の雛形（templates/）が、ドメインと合うこと。

指示書とスキーマのパスは、ステージの種類の名前から組むので、実在と形は検査でしか分からない。スキーマは `claude --json-schema` に本文のまま渡り、draft-07 だけが通る。スキーマの欄は、ドメインが読む欄（`StageSpec.result`）と、統括の判断を置き換える
コマンドの欄に照らす。
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autodev.app.stages.prompts.assets import contract_inputs
from autodev.app.stages.prompts.render import Prompts, system_rules
from autodev.app.stages.prompts.sources import SOURCES, Block
from autodev.app.stages.stage_context import StageContext
from autodev.app.supervision.decisions import RUN_DECISIONS, TASK_DECISIONS, payload_key
from autodev.domain.aggregates.review_ledger import ReviewLedger
from autodev.domain.commands.base import Command
from autodev.domain.commands.review_ledger import JudgeFinding, RaiseFinding, RecordFindings
from autodev.domain.flow.flow import FlowStep, Reviewers
from autodev.domain.services.escalation_router import RAISED_IN_TASK, RELAYED_TO_RUN
from autodev.domain.stages.catalog import REVIEWER_STAGES, STAGE_SPECS, StageSpec
from autodev.domain.stages.kinds import ResultField, StageMode
from autodev.domain.supervision import Notice
from autodev.domain.value_objects.base import InvalidValue
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.design_cause import DesignCause
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.finding_id import FindingId
from autodev.domain.value_objects.finding_status import FindingStatus
from autodev.domain.value_objects.finding_target import FindingTarget
from autodev.domain.value_objects.finding_transfer import FindingTransfer
from autodev.domain.value_objects.gate_item import GateItem
from autodev.domain.value_objects.hint import Hint
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.issuer_kind import IssuerKind
from autodev.domain.value_objects.location import Location
from autodev.domain.value_objects.pointers import Pointers
from autodev.domain.value_objects.proposal import Proposal
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.rating import Rating
from autodev.domain.value_objects.reported_finding import ReportedFinding
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.session_id import SessionId
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stall_cause import StallCause
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_kind import TaskKind
from autodev.domain.value_objects.task_spec import TaskSpec
from autodev.domain.value_objects.verify_kind import VerifyKind
from autodev.infra.paths import RunPaths
from autodev.infra.repo_config import FIELDS as REPO_CONFIG_FIELDS
from conftest import AUTODEV_PACKAGE, AUTODEV_SKILL

CONTRACTS = AUTODEV_PACKAGE / "contracts"
SCHEMAS = AUTODEV_PACKAGE / "schemas"
TEMPLATES = AUTODEV_PACKAGE / "templates"

DRAFT_07 = "http://json-schema.org/draft-07/schema#"

#: draft-07 のキーワード。2019-09 から後のもの（`$defs`・`prefixItems`・`unevaluatedProperties` など）は
#: `--json-schema` が受けないので、ここに無いキーワードを使ったら落とす
DRAFT_07_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$ref",
        "$comment",
        "title",
        "description",
        "default",
        "examples",
        "readOnly",
        "writeOnly",
        "type",
        "enum",
        "const",
        "multipleOf",
        "maximum",
        "exclusiveMaximum",
        "minimum",
        "exclusiveMinimum",
        "maxLength",
        "minLength",
        "pattern",
        "format",
        "items",
        "additionalItems",
        "maxItems",
        "minItems",
        "uniqueItems",
        "contains",
        "maxProperties",
        "minProperties",
        "required",
        "properties",
        "patternProperties",
        "additionalProperties",
        "dependencies",
        "propertyNames",
        "if",
        "then",
        "else",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "definitions",
        "contentMediaType",
        "contentEncoding",
    }
)

SUPERVISORS = ("supervisor-run", "supervisor-task")

#: 統括ではなく driver が埋めるコマンドの欄
FILLED_BY_DRIVER = frozenset({"command_id", "issuer", "task"})
#: コマンドに無いが、判断の JSON に置く欄
EXTRA_DECISION_FIELDS: dict[str, frozenset[str]] = {}
#: LLM の統括は書かず、プログラムの統括だけが書くコマンドの欄（git 管理タスクの統括が、取り出した仕事を写す。
#: 計画タスクの統括が、DesignJudge の問いを写す。git 管理タスクの統括が、答え以外では閉じられない印を写す）
WRITTEN_BY_PROGRAM = {
    "run-flow": frozenset({"job"}),
    "escalate": frozenset({"question", "answer_only"}),
}

#: RefreshOverview が埋めるマーカー。1 行に単独で置いた
#: `<!-- autodev:<名前> -->` だけを、1 回の走査で置き換える。RefreshOverview を実装したら、
#: この一覧をそちらへ移し、ここはそれを読む
OVERVIEW_MARKERS = frozenset(
    {"tasks", "waiting", "decisions", "deferrals", "instruction", "signature"}
)
#: タスク PR を作るとき（CreatePR）に埋めるマーカー
TASK_PR_MARKERS = frozenset({"overview-pr", "body"})

ANY_MARKER = re.compile(r"<!-- autodev:([a-z][a-z-]*) -->")
MARKER_LINE = re.compile(r"^[ \t]*<!-- autodev:([a-z][a-z-]*) -->[ \t]*$", re.MULTILINE)

#: 指示書のプレースホルダ（`<ブリーフ>` など）。`D<番号>` のように英数字に続くものと、`<!--`・
#: `<<<<<<<` は含めない
PLACEHOLDER = re.compile(r"(?<![A-Za-z0-9])<([^<>\s!/`\-=][^<>\n`]*)>")


def asset_name(kind: StageKind) -> str:
    """ステージの種類の名前から、指示書とスキーマのファイル名（拡張子を除く）。`TestGen` → `test-gen`。"""
    return re.sub(r"(?<!^)(?=[A-Z])", "-", kind.value).lower()


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def field_names(cls: Any) -> frozenset[str]:
    """dataclass の欄の名前。"""
    return frozenset(f.name for f in dataclasses.fields(cls))


LLM_SPECS: tuple[StageSpec, ...] = tuple(
    spec for spec in STAGE_SPECS.values() if spec.mode is StageMode.LLM
)
STAGE_NAMES = tuple(asset_name(spec.kind) for spec in LLM_SPECS)


@cache
def schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.json").read_text(encoding="utf-8"))


@cache
def contract(name: str) -> str:
    return (CONTRACTS / f"{name}.md").read_text(encoding="utf-8")


def contract_with_shared(name: str) -> str:
    """指示書と、そこからリンクした共有の指示書（`_` で始まるファイル）を合わせた本文。"""
    text = contract(name)
    shared = re.findall(r"\]\((_[a-z-]+\.md)\)", text)
    return "\n".join([text, *((CONTRACTS / s).read_text(encoding="utf-8") for s in set(shared))])


def prop(node: dict[str, Any], *path: str) -> dict[str, Any]:
    """`properties` と `items` をたどる。`[]` は配列の要素。"""
    for key in path:
        node = node["items"] if key == "[]" else node["properties"][key]
    return node


def enum_of(node: dict[str, Any]) -> set[Any]:
    return set(node["enum"])


def accepts(node: dict[str, Any], value: str) -> bool:
    return re.fullmatch(node["pattern"].removeprefix("^").removesuffix("$"), value) is not None


def subschemas(node: dict[str, Any]) -> list[dict[str, Any]]:
    """`node` の直下のスキーマ（`properties` の値・`items`・`anyOf` の枝など）。"""
    found: list[dict[str, Any]] = []
    for key in ("properties", "patternProperties", "definitions"):
        found += node.get(key, {}).values()
    for key in ("items", "additionalProperties", "not", "if", "then", "else", "contains"):
        value = node.get(key)
        if isinstance(value, dict):
            found.append(value)
        elif isinstance(value, list):
            found += value
    for key in ("allOf", "anyOf", "oneOf"):
        found += node.get(key, [])
    return found


def walk(node: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [node]
    for child in subschemas(node):
        nodes += walk(child)
    return nodes


ALL_SCHEMAS = (*STAGE_NAMES, *SUPERVISORS)


# --- 在ること ---


def test_LLMのステージと統括ごとに指示書とスキーマがある():
    for name in ALL_SCHEMAS:
        assert (CONTRACTS / f"{name}.md").is_file(), name
        assert (SCHEMAS / f"{name}.json").is_file(), name


#: templates/ に置く雛形。ステージへのプロンプトの雛形は、実行器のプロンプトの組み立てを書くときに足す
TEMPLATE_NAMES = frozenset({"overview-pr-body", "task-pr-body", "brief"})


def test_新しい作りに無い指示書とスキーマと雛形を置かない():
    contracts = {p.stem for p in CONTRACTS.glob("*.md") if not p.name.startswith("_")}
    assert contracts == set(ALL_SCHEMAS)
    assert {p.stem for p in SCHEMAS.glob("*.json")} == set(ALL_SCHEMAS)
    assert {p.stem for p in TEMPLATES.iterdir()} == TEMPLATE_NAMES


# --- スキーマの形（--json-schema の制約） ---


@pytest.mark.parametrize("name", ALL_SCHEMAS)
def test_スキーマはdraft07で欄を持つobject(name: str):
    root = schema(name)
    assert root["$schema"] == DRAFT_07
    assert root["type"] == "object"
    assert root["properties"]
    assert root["additionalProperties"] is False


@pytest.mark.parametrize("name", ALL_SCHEMAS)
def test_スキーマは根に結合子を置かない(name: str):
    # StructuredOutput のツールの入力スキーマになるので、根は欄を並べた object のままにする。
    # どの欄の組が要るか（統括の decision と中身の欄など）は、受け取る側が確かめて差し戻す
    assert not {"anyOf", "oneOf", "allOf", "not", "if", "then", "else"} & set(schema(name))


@pytest.mark.parametrize("name", ALL_SCHEMAS)
def test_スキーマはdraft07のキーワードだけを使い欄を閉じる(name: str):
    for node in walk(schema(name)):
        assert set(node) <= DRAFT_07_KEYWORDS, set(node) - DRAFT_07_KEYWORDS
        # 組で決まる欄は if/then で書かず、ドメインの取り込みが拒む
        assert not {"if", "then", "else"} & set(node)
        types = node.get("type")
        if types == "object" or (isinstance(types, list) and "object" in types):
            assert node.get("additionalProperties") is False
            assert set(node.get("required", ())) <= set(node["properties"])


# --- ステージの結果の欄とドメイン ---


@pytest.mark.parametrize("spec", LLM_SPECS, ids=STAGE_NAMES)
def test_結果の欄はドメインが読む欄を必ず持つ(spec: StageSpec):
    root = schema(asset_name(spec.kind))
    read = {field.value for field in spec.result}
    assert read <= set(root["properties"])
    assert read <= set(root["required"])
    known = {field.value for field in ResultField}
    # ドメインが読む欄の名前を、ほかの意味で使わない
    assert set(root["properties"]) & known == read


@pytest.mark.parametrize("spec", LLM_SPECS, ids=STAGE_NAMES)
def test_報告の欄は返してよい報告と空だけを受ける(spec: StageSpec):
    root = schema(asset_name(spec.kind))
    if not spec.reports:
        assert "report" not in root["properties"]
        return
    assert enum_of(prop(root, "report")) == {kind.value for kind in spec.reports} | {None}
    assert "reportReason" in root["required"]


def _specs_reading(field: ResultField) -> list[StageSpec]:
    return [spec for spec in LLM_SPECS if field in spec.result]


#: 指摘に宛先（target）を持たせる見る役。DesignReview は持たない（設計の台帳に宛先は無い）
_TARGETED_REVIEWERS = {StageKind.REVIEW, StageKind.ADVERSARIAL_REVIEW}


def test_指摘の欄はRaiseFindingの欄と合う():
    for spec in _specs_reading(ResultField.FINDINGS):
        root = schema(asset_name(spec.kind))
        item = prop(root, "findings", "[]")
        names = {snake(name) for name in item["properties"]}
        targeted = spec.kind in _TARGETED_REVIEWERS
        assert names == {"rating", "body", "location"} | ({"target"} if targeted else set())
        # 宛先は `RaiseFinding.finding_target`（`target` は届け先の台帳）
        assert names - {"target"} <= field_names(RaiseFinding)
        assert "finding_target" in field_names(RaiseFinding)
        assert enum_of(prop(item, "rating")) == {rating.value for rating in Rating}
        if targeted:
            assert enum_of(prop(item, "target")) == {target.value for target in FindingTarget}
            assert "target" in item["required"]


#: コメントと判定で指せる指摘。Gate の項目の指摘は、上げずに指摘を開く項目のものだけが立つ
_GATE_FINDINGS = {f"G-{item.value}" for item in GateItem if item.escalation is None}
_REVIEW_FINDINGS = {"R1", "R12"}
_DESIGN_FINDINGS = {"D1", "D7"}
_FINDING_SAMPLES = (
    _REVIEW_FINDINGS
    | _DESIGN_FINDINGS
    | {f"G-{item.value}" for item in GateItem}
    | {"R0", "D01", "r1", "G-x", "task1"}
)
_COMMENTABLE = {
    StageKind.IMPL: _REVIEW_FINDINGS | _GATE_FINDINGS,
    StageKind.FIX: _REVIEW_FINDINGS | _GATE_FINDINGS,
    StageKind.TEST_FIX: _REVIEW_FINDINGS,
    StageKind.JUDGE: _REVIEW_FINDINGS,
    StageKind.DESIGN_JUDGE: _DESIGN_FINDINGS,
    StageKind.REVISE: _DESIGN_FINDINGS,
}


def _accepted_findings(node: dict[str, Any]) -> set[str]:
    accepted = {sample for sample in _FINDING_SAMPLES if accepts(node, sample)}
    for sample in accepted:
        FindingId(sample)  # 受けた id は、ドメインの指摘の id として正しい
    return accepted


def test_コメントの欄は指せる指摘とCommentFindingの欄に合う():
    specs = _specs_reading(ResultField.COMMENTS)
    assert {spec.kind for spec in specs} == set(_COMMENTABLE)
    for spec in specs:
        item = prop(schema(asset_name(spec.kind)), "comments", "[]")
        assert set(item["properties"]) == {"finding", "body"}
        assert _accepted_findings(prop(item, "finding")) == _COMMENTABLE[spec.kind]


def test_判定の欄はJudgeFindingの欄と動かせる状態に合う():
    for spec in _specs_reading(ResultField.VERDICTS):
        item = prop(schema(asset_name(spec.kind)), "verdicts", "[]")
        assert {snake(name) for name in item["properties"]} - {"target"} <= field_names(
            JudgeFinding
        )
        targeted = spec.kind is StageKind.JUDGE
        assert set(item["properties"]) == {"finding", "to", "comment"} | (
            {"target"} if targeted else set()
        )
        if targeted:
            # 書き換えない判定のために null も書ける。Codex の strict な出力スキーマのため必須
            assert enum_of(prop(item, "target")) == {t.value for t in FindingTarget} | {None}
            assert "target" in item["required"]
        # carried へは再計画の移管だけが動かす
        judged = {status.value for status in FindingStatus if status is not FindingStatus.CARRIED}
        assert enum_of(prop(item, "to")) == judged
        assert _accepted_findings(prop(item, "finding")) == _COMMENTABLE[spec.kind]


def test_停滞の原因と設計の原因はドメインの列挙と合う():
    for spec in _specs_reading(ResultField.STALL_CAUSE):
        node = prop(schema(asset_name(spec.kind)), "stallCause")
        assert enum_of(node) == {cause.value for cause in StallCause} | {None}
    for spec in _specs_reading(ResultField.DESIGN_CAUSE):
        node = prop(schema(asset_name(spec.kind)), "designCause")
        assert enum_of(prop(node, "kind")) == {cause.value for cause in DesignCause}
        # MarkReverted の to_version（DesignVersion は 1 から）
        reverted = prop(node, "revertedTo")
        assert reverted["minimum"] == 1
        assert "integer" in reverted["type"]


_TASK_SAMPLES = ("task1", "task12", "task0", "task01", "planning", "git", "task", "Task1")


def _is_implementation_task(value: str) -> bool:
    try:
        return TaskId(value).kind is TaskKind.IMPLEMENTATION
    except InvalidValue:
        return False


def assert_task_ids(node: dict[str, Any]) -> None:
    """実装タスクの id だけを受ける。"""
    for sample in _TASK_SAMPLES:
        assert accepts(node, sample) == _is_implementation_task(sample), sample


def test_提案の欄はProposalとTaskSpecの欄に合う():
    proposal_fields = {
        ResultField.DESIGN,
        ResultField.TASKS,
        ResultField.QUICK_CHECKS,
        ResultField.REGRESSION_TESTS,
        ResultField.STOP,
        ResultField.DISCARD,
        ResultField.CARRY,
        ResultField.DECISIONS,
        ResultField.DEFERRALS,
    }
    assert {snake(field.value) for field in proposal_fields} <= field_names(Proposal)
    for spec in _specs_reading(ResultField.TASKS):
        root = schema(asset_name(spec.kind))
        item = prop(root, "tasks", "[]")
        spec_fields = {camel(name) for name in field_names(TaskSpec)}
        assert set(item["properties"]) == spec_fields | {"id", "blockedBy"}
        assert set(item["required"]) == set(item["properties"])
        assert_task_ids(prop(item, "id"))
        assert_task_ids(prop(item, "blockedBy", "[]"))
        assert prop(root, "design")["minLength"] == 1
        for name in ("quickChecks", "regressionTests", "decisions", "deferrals"):
            assert prop(root, name, "[]")["type"] == "string"
    for spec in _specs_reading(ResultField.STOP) + _specs_reading(ResultField.DISCARD):
        root = schema(asset_name(spec.kind))
        assert_task_ids(prop(root, "stop", "[]"))
        assert_task_ids(prop(root, "discard", "[]"))
    for spec in _specs_reading(ResultField.CARRY):
        item = prop(schema(asset_name(spec.kind)), "carry", "[]")
        assert set(item["properties"]) == {camel(name) for name in field_names(FindingTransfer)}
        assert _accepted_findings(prop(item, "finding")) == _REVIEW_FINDINGS
        assert_task_ids(prop(item, "fromTask"))
        assert_task_ids(prop(item, "toTask"))


def test_本文とタイトルとコードマップと期待値待ちの欄は中身を持つ():
    for field in (ResultField.BODY, ResultField.CODEMAP):
        for spec in _specs_reading(field):
            node = prop(schema(asset_name(spec.kind)), field.value)
            assert node["type"] == "string"
            assert node["minLength"] == 1
    for spec in _specs_reading(ResultField.TITLE):
        node = prop(schema(asset_name(spec.kind)), "title")
        assert node["type"] == "string"
        assert node["minLength"] == 1
        assert "maxLength" in node
        # PR のタイトルは 1 行
        assert accepts(node, "キャッシュを足す")
        assert not accepts(node, "キャッシュを\n足す")
    for spec in _specs_reading(ResultField.AWAITING_EXPECTATIONS):
        node = prop(schema(asset_name(spec.kind)), "awaitingExpectations")
        assert node["type"] == "array"
        assert prop(node, "[]")["type"] == "object"


# --- 統括の判断とコマンド ---


def _decision_payloads(
    name: str, decisions: Mapping[str, type[Command]]
) -> dict[str, dict[str, Any]]:
    """`decision` の値 → 中身の欄のスキーマ。

    根の欄は `decision` と、decision ごとの中身の欄だけ。どの decision でどの欄が要るかはスキーマに
    書かず（根に結合子を置かない）、判断をコマンドに置き換えるアプリケーション層が確かめる。
    """
    root = schema(name)
    assert enum_of(prop(root, "decision")) == set(decisions)
    assert root["required"] == ["decision"]
    keys = {decision: payload_key(decision) for decision in decisions}
    assert set(root["properties"]) == {"decision", *keys.values()}
    return {decision: prop(root, key) for decision, key in keys.items()}


@pytest.mark.parametrize(
    ("name", "decisions", "issuer"),
    [
        ("supervisor-task", TASK_DECISIONS, IssuerKind.TASK_SUPERVISOR),
        ("supervisor-run", RUN_DECISIONS, IssuerKind.RUN_SUPERVISOR),
    ],
)
def test_統括の判断の欄は置き換えるコマンドの欄と合う(
    name: str, decisions: Mapping[str, type[Command]], issuer: IssuerKind
):
    for decision, payload in _decision_payloads(name, decisions).items():
        command = decisions[decision]
        assert issuer in command.ISSUERS
        written = {snake(key) for key in payload["properties"]}
        extra = EXTRA_DECISION_FIELDS.get(decision, frozenset())
        by_program = WRITTEN_BY_PROGRAM.get(decision, frozenset())
        assert written - extra == field_names(command) - FILLED_BY_DRIVER - by_program, decision
        assert set(payload["required"]) == set(payload["properties"]), decision


def _assert_pattern_is(node: dict[str, Any], pattern: re.Pattern[str]) -> None:
    assert node["pattern"] == f"^{pattern.pattern}$"


def test_統括の判断のidの形はドメインの値と合う():
    for name in SUPERVISORS:
        for node in walk(schema(name)):
            for key, child in node.get("properties", {}).items():
                if key in {"respondsTo", "source", "trigger", "escalation"}:
                    _assert_pattern_is(child, EventId.PATTERN)
                elif key == "session":
                    _assert_pattern_is(child, SessionId.PATTERN)
    run = _decision_payloads("supervisor-run", RUN_DECISIONS)
    _assert_pattern_is(prop(run["replan"], "answer"), QuestionId.PATTERN)
    _assert_pattern_is(prop(run["answer"], "question"), QuestionId.PATTERN)
    _assert_pattern_is(prop(run["ask-user"], "question"), QuestionId.PATTERN)
    assert_task_ids(prop(run["insert-task"], "takesOver"))
    assert_task_ids(prop(run["insert-task"], "blockedBy", "[]"))
    assert_task_ids(prop(run["stop-tasks"], "tasks", "[]"))
    assert_task_ids(prop(run["apply-plan"], "stop", "[]"))
    assert_task_ids(prop(run["apply-plan"], "discard", "[]"))
    spec = prop(run["insert-task"], "spec")
    assert set(spec["properties"]) == {camel(name) for name in field_names(TaskSpec)}


def test_フローの段はFlowStepの欄と実装タスクに置けるステージに合う():
    flow = _decision_payloads("supervisor-task", TASK_DECISIONS)["run-flow"]
    step = prop(flow, "steps", "[]")
    assert {snake(key) for key in step["properties"]} == field_names(FlowStep)
    placeable = {
        kind.value
        for kind, spec in STAGE_SPECS.items()
        if spec.parent is None and TaskKind.IMPLEMENTATION in spec.task_kinds
    }
    assert enum_of(prop(step, "stage")) == placeable
    reviewers = prop(step, "reviewers")
    assert set(reviewers["properties"]) == field_names(Reviewers)
    for which in ("first", "later"):
        assert enum_of(prop(reviewers, which, "[]")) == {kind.value for kind in REVIEWER_STAGES}


def test_ラン統括へ上げる欄はPointersとHintとドメインの列挙に合う():
    escalate = _decision_payloads("supervisor-task", TASK_DECISIONS)["escalate"]
    relayed = RELAYED_TO_RUN[TaskKind.IMPLEMENTATION]
    assert enum_of(prop(escalate, "kind")) == {kind.value for kind in relayed}
    pointers = prop(escalate, "pointers")
    assert set(pointers["properties"]) == {camel(name) for name in field_names(Pointers)}
    hint = prop(escalate, "hint")
    assert set(hint["properties"]) == {camel(name) for name in field_names(Hint)}
    assert enum_of(prop(hint, "stallCause")) == {cause.value for cause in StallCause} | {None}
    assert enum_of(prop(hint, "designCause")) == {cause.value for cause in DesignCause} | {None}
    assert enum_of(prop(hint, "gateItems", "[]")) == {item.value for item in GateItem}
    accepted = _accepted_findings(prop(hint, "findingIds", "[]"))
    assert accepted == _REVIEW_FINDINGS | _DESIGN_FINDINGS | _GATE_FINDINGS


# --- 指示書 ---


def declared_placeholders(text: str) -> set[str]:
    """`## 入力` の表の 1 列目に書いたプレースホルダ。"""
    section = text.split("## 入力", 1)[1].split("\n## ", 1)[0]
    return {
        match
        for line in section.splitlines()
        if line.startswith("| `<")
        for match in PLACEHOLDER.findall(line.split("|")[1])
    }


@pytest.mark.parametrize("name", ALL_SCHEMAS)
def test_指示書のプレースホルダは入力の表に書いてある(name: str):
    text = contract(name)
    declared = declared_placeholders(text)
    assert declared
    assert set(PLACEHOLDER.findall(text)) == declared
    # リンクした共有の指示書も同じプロンプトで読むので、リンク元の入力の表に無い名前を使わない
    assert set(PLACEHOLDER.findall(contract_with_shared(name))) <= declared


@pytest.mark.parametrize("name", ALL_SCHEMAS)
def test_指示書の入力はどれもプロンプトの組み立てに出どころがある(name: str):
    # プロンプトの組み立ては、指示書の入力の表を読んで埋める。同じ表を同じ順で読めている
    assert set(contract_inputs(name)) == declared_placeholders(contract(name))
    assert set(contract_inputs(name)) <= set(SOURCES)


def test_統括の指示書は起こされるときの知らせの名前を全部書く():
    wakes = {
        name: contract(name).split("## 起こされるとき", 1)[1].split("\n## ", 1)[0]
        for name in SUPERVISORS
    }
    for notice in Notice:
        assert any(f"（`{notice.value}`）" in text for text in wakes.values()), notice
    for text in wakes.values():
        assert f"（`{Notice.DECISION_REJECTED.value}`）" in text


def test_プロンプトの組み立ての出どころはどれかの指示書が使う():
    used = {placeholder for name in ALL_SCHEMAS for placeholder in contract_inputs(name)}
    assert set(SOURCES) == used


@pytest.mark.parametrize("name", ALL_SCHEMAS)
def test_指示書はpushとghを禁じて結果の欄の意味を書く(name: str):
    text = contract(name)
    forbidden = text.split("## してはいけないこと", 1)[1]
    assert "`git push`" in forbidden
    assert "`gh`" in forbidden
    explained = contract_with_shared(name)
    for key in schema(name)["properties"]:
        assert f"`{key}`" in explained, key


@pytest.mark.parametrize("name", SUPERVISORS)
def test_統括の指示書は状態を読むコマンドのほかにdriverのコマンドを叩かないと書く(name: str):
    forbidden = contract(name).split("## してはいけないこと", 1)[1]
    assert "`<状態を読むコマンド>` のほかに driver のコマンドを叩く" in forbidden


#: ask の呼び方。PreToolUse の defer が効くのは、そのターンのツール呼び出しが 1 つだけのとき
ASK_IN_ONE_TURN = "1 つのターンで ask だけを 1 回呼ぶ。質問が複数あれば、ターンを分けて呼ぶ。"


@pytest.mark.parametrize("spec", LLM_SPECS, ids=STAGE_NAMES)
def test_askで聞けるステージだけがaskを1つのターンで1回呼ぶと書く(spec: StageSpec):
    text = contract(asset_name(spec.kind))
    assert spec.guard is not None
    if spec.guard.can_ask:
        assert "<autodev> ask --question " in text
        assert ASK_IN_ONE_TURN in text
    else:
        assert "ask --question" not in text
    for kind in spec.reports:
        assert f"`{kind.value}`" in text


def test_提案のタスクの数の下限はPlanとReplanとReviseで同じ():
    # Revise は Plan と Replan のどちらの続きでも走るので、スキーマでは下限を決められない。初回の計画の
    # 1 件以上は指示書（_proposal.md）が求め、ドメインが拒む
    for spec in _specs_reading(ResultField.TASKS):
        assert "minItems" not in prop(schema(asset_name(spec.kind)), "tasks")


def test_共有の指示書はリンクした指示書からだけ読まれる():
    shared = {p.name for p in CONTRACTS.glob("_*.md")}
    linked = {
        link for name in ALL_SCHEMAS for link in re.findall(r"\]\((_[a-z-]+\.md)\)", contract(name))
    }
    assert shared == linked


def _flow_table() -> dict[str, tuple[set[str], set[str]]]:
    """タスク統括の指示書の「置けるステージ」の表: ステージ → (要る, 作る)。"""
    text = contract("supervisor-task").split("### 置けるステージ", 1)[1].split("\n#", 1)[0]
    rows: dict[str, tuple[set[str], set[str]]] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) == 4 and cells[0].startswith("`"):
            needs, produces = (set(re.findall(r"`([a-z-]+)`", cell)) for cell in cells[1:3])
            rows[cells[0].strip("`")] = (needs, produces)
    return rows


def test_タスク統括の指示書のステージの表はStageSpecと合う():
    rows = _flow_table()
    placeable = {
        kind
        for kind, spec in STAGE_SPECS.items()
        if spec.parent is None and TaskKind.IMPLEMENTATION in spec.task_kinds
    }
    assert set(rows) == {kind.value for kind in placeable}
    for kind in placeable:
        spec = STAGE_SPECS[kind]
        needs, produces = rows[kind.value]
        assert needs == {a.value for a in spec.needs | spec.optional}, kind
        assert produces == {a.value for a in spec.produces | spec.may_produce}, kind


def _escalation_sources() -> dict[str, set[str]]:
    """タスク統括の指示書の「上がってくるエスカレーション」の表: 種類 → 出す所に書いたステージ。"""
    text = contract("supervisor-task").split("## 上がってくるエスカレーション", 1)[1]
    text = text.split("\n## ", 1)[0]
    stage_names = {kind.value for kind in StageKind}
    rows: dict[str, set[str]] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) == 3 and cells[0].startswith("`"):
            named = set(re.findall(r"`([A-Za-z]+)`", cells[1])) & stage_names
            rows[cells[0].strip("`")] = named
    return rows


def test_タスク統括の指示書のエスカレーションの出す所はStageSpecと合う():
    rows = _escalation_sources()
    assert set(rows) == {kind.value for kind in RAISED_IN_TASK[TaskKind.IMPLEMENTATION]}
    implementation = [
        spec for spec in STAGE_SPECS.values() if TaskKind.IMPLEMENTATION in spec.task_kinds
    ]
    for kind in RAISED_IN_TASK[TaskKind.IMPLEMENTATION]:
        expected = {spec.kind.value for spec in implementation if kind in spec.raises}
        if kind in {item.escalation for item in GateItem}:
            expected.add(StageKind.GATE.value)
        if expected:
            # 停滞（ジャッジの判定の後に台帳が見つける）と 2 回続けたエラー（どのステージでも）は、
            # ステージの宣言からは決まらない
            assert rows[kind.value] == expected, kind


def test_統括の指示書は受けるエスカレーションと判断を全部書く():
    task = contract("supervisor-task")
    for kind in RAISED_IN_TASK[TaskKind.IMPLEMENTATION] | RELAYED_TO_RUN[TaskKind.IMPLEMENTATION]:
        assert f"`{kind.value}`" in task, kind
    for item in GateItem:
        assert f"`{item.value}`" in task, item
    run = contract("supervisor-run")
    for kind in set().union(*RELAYED_TO_RUN.values()):
        assert f"`{kind.value}`" in run, kind
    for decision in RUN_DECISIONS:
        assert f"`{decision}`" in run, decision
    for decision in TASK_DECISIONS:
        assert f"`{decision}`" in task, decision


# --- 雛形 ---


def markers_of(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    # 1 行に単独で置いたマーカーだけが埋まる。文の中に置いたマーカーは埋まらずに残る
    assert len(ANY_MARKER.findall(text)) == len(MARKER_LINE.findall(text)), path.name
    # 本文を string.Template に通さないので、`${…}` は埋まらずに残る
    assert "${" not in text, path.name
    return set(MARKER_LINE.findall(text))


def test_概要PRの雛形のマーカーはRefreshOverviewが埋めるものと合う():
    assert markers_of(TEMPLATES / "overview-pr-body.md") == OVERVIEW_MARKERS
    write_overview = contract(asset_name(StageKind.WRITE_OVERVIEW))
    for marker in OVERVIEW_MARKERS:
        assert f"`<!-- autodev:{marker} -->`" in write_overview, marker


def test_タスクPRの雛形のマーカーはPRを作るときに埋めるものと合う():
    assert markers_of(TEMPLATES / "task-pr-body.md") == TASK_PR_MARKERS


def test_READMEのリポジトリの設定の表はリポジトリの設定の鍵を全部載せ古い鍵verifyを読まないと書く():
    row = next(
        line
        for line in (AUTODEV_SKILL / "README.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| `~/.config/autodev/repos/")
    )
    for key in REPO_CONFIG_FIELDS:
        assert key in row, key
    assert "`verify`" in row


@pytest.mark.parametrize("path", ["SKILL.md", "DOMAIN.html"])
def test_検証コマンドの種類の名前は入口の文書に載る(path: str):
    text = (AUTODEV_SKILL / path).read_text(encoding="utf-8")
    for kind in VerifyKind:
        assert kind.value in text, kind


# --- 結果の返し方はエージェントごとの実装が足す ---


def test_system_rulesはghとgit_pushの禁止と書き換えてはいけない場所を書く():
    text = system_rules("Impl のステージ")
    for word in (
        "`gh`",
        "`git push`",
        "ランディレクトリ",
        "events.db",
        "対象リポジトリの手元の checkout",
        "ほかのタスクの worktree",
        "ホームの設定ファイル",
    ):
        assert word in text, word


def test_system_rulesと続きの指示はツール名を含まず返し方はエージェントに任せる(tmp_path: Path):
    assert "StructuredOutput" not in system_rules("Impl のステージ")
    paths = RunPaths(RunName("r"), tmp_path / "r")
    execution = ExecutionId(TaskId("task1"), StageKind.IMPL, 0, 1)
    context = StageContext(
        execution=execution,
        step_index=0,
        step=FlowStep(StageKind.IMPL),
        tree=tmp_path,
    )
    assert "StructuredOutput" not in Prompts(paths, lambda: []).continuation(context)


# --- プロンプトの <指摘> ---


def test_プロンプトの指摘の各件に宛先が載る():
    task = TaskId("task2")
    stream = StreamId.review(task)
    ledger = ReviewLedger(stream)
    review = ExecutionId(task, StageKind.REVIEW, 1, 1)
    command = RecordFindings(
        command_id=CommandId("c1"),
        issuer=Issuer.policy("review-loop", EventId("task/task2#3")),
        ledger=stream,
        source=review,
        findings=(
            ReportedFinding(
                Rating.MUST_FIX, "テストが無い", Location("src/a.py"), FindingTarget.TESTS
            ),
            ReportedFinding(Rating.NIT, "名前が長い"),
        ),
    )
    for event in ledger.handle(command):
        ledger.apply(event, command.command_id)
    sources = SimpleNamespace(target=task, ledger=lambda s: ledger if s == stream else None)
    block = SOURCES["指摘"](sources)  # ty: ignore[invalid-argument-type]
    assert isinstance(block, Block)
    found = json.loads(block.text.removeprefix("```json\n").removesuffix("\n```"))
    assert [(f["id"], f["target"]) for f in found] == [("R1", "tests"), ("R2", "impl")]
