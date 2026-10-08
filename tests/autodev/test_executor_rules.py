"""実行器のために足したドメインの規則と、実行器の部品（マーカー・スキーマの照合・スナップショット）。

ドメインの規則はイベントの列とコマンドだけで確かめる（I/O 無し）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from autodev_harness import DRIVER, POLICY, new_id, of_type
from test_task import HEAD, IMPL_FLOW, LOST_SESSION, T1, TaskLoop, ex, gate

from autodev.adapters.claude.schema import ecma_pattern, normalize_nulls, violations
from autodev.app.driving.mainloop import Delivery
from autodev.app.stages.executor import from_parts
from autodev.app.stages.markers import fill
from autodev.app.stages.stage_context import ResumeMode, RunSetting, snapshot
from autodev.domain.aggregates.review_ledger import ReviewLedger
from autodev.domain.aggregates.task import ExecutionStatus, StageStart, StartMode
from autodev.domain.commands.task import (
    BeginStage,
    ChangeScope,
    MarkInterrupted,
    ReportBeginFailure,
    ResumeStage,
)
from autodev.domain.events.run import RunStarted
from autodev.domain.events.task import ExecutionRestarted, StageCompleted, StageFailed
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.gate_item import GateItem
from autodev.domain.value_objects.instruction import Instruction
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.parallel_limit import ParallelLimit
from autodev.domain.value_objects.pointers import Pointers
from autodev.domain.value_objects.repository import Repository
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.session_id import SessionId
from autodev.domain.value_objects.stage_exit import StageExit
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_spec import TaskSpec
from autodev.infra.paths import RunPaths
from conftest import AUTODEV_PACKAGE

S = StageKind
A = ArtifactKind


def session(n: int) -> SessionId:
    return SessionId(f"00000000-0000-4000-8000-{n:012d}")


def begin(task: TaskLoop, execution: ExecutionId, n: int | None) -> None:
    task(
        BeginStage(
            command_id=new_id(),
            issuer=Issuer.executor(execution),
            task=T1,
            execution=execution,
            head=HEAD,
            session=session(n) if n is not None else None,
        )
    )


@pytest.fixture
def task() -> TaskLoop:
    loop = TaskLoop(T1)
    loop.open()
    loop.flow(*IMPL_FLOW)
    return loop


# --- 続けるセッション（StageSpec.session の宣言から） ---


def test_FixはImplのセッションをJudgeは前のラウンドのJudgeのセッションを続ける(task: TaskLoop):
    agg = task.aggregate
    assert agg.session_to_continue(ex(S.IMPL)) is None
    begin(task, ex(S.IMPL), 1)
    task.report(ex(S.IMPL))
    for stage, n in ((S.REVIEW, 2), (S.ADVERSARIAL_REVIEW, 3)):
        assert agg.session_to_continue(ex(stage, 1)) is None  # 毎回まっさら
        begin(task, ex(stage, 1), n)
        task.report(ex(stage, 1))
        task.confirm(ex(stage, 1))
    begin(task, ex(S.JUDGE, 1), 4)
    task.report(ex(S.JUDGE, 1))
    task.conclude(ex(S.JUDGE, 1), "R1")
    assert agg.session_to_continue(ex(S.FIX, 1)) == session(1)
    begin(task, ex(S.FIX, 1), 1)
    task.report(ex(S.FIX, 1))
    assert agg.session_to_continue(ex(S.JUDGE, 2)) == session(4)
    # 決定的なステージにはセッションが無い
    assert agg.session_to_continue(ex(S.GATE)) is None


def test_続けて2回落ちたセッションは捨てfresh_sessionなら続けない(task: TaskLoop):
    agg = task.aggregate
    begin(task, ex(S.IMPL), 1)
    task.report(ex(S.IMPL), result_valid=False)
    assert agg.session_to_continue(ex(S.IMPL, attempt=2)) == session(1)
    begin(task, ex(S.IMPL, attempt=2), 1)
    task.report(ex(S.IMPL, attempt=2), result_valid=False)
    assert agg.session_to_continue(ex(S.IMPL, attempt=3)) is None
    assert agg.session_to_continue(ex(S.IMPL, attempt=3), fresh=True) is None


def test_続きから始めた実行は元の状態を覚え続けられなかった証拠で作り直す(task: TaskLoop):
    begin(task, ex(S.IMPL), 1)
    agg = task.aggregate
    assert agg.executions[ex(S.IMPL)].resumed_from is None
    task(MarkInterrupted(command_id=new_id(), issuer=DRIVER, task=T1, execution=ex(S.IMPL)))
    task(ResumeStage(command_id=new_id(), issuer=DRIVER, task=T1, execution=ex(S.IMPL)))
    assert agg.executions[ex(S.IMPL)].resumed_from is ExecutionStatus.INTERRUPTED
    events = task.report(
        ex(S.IMPL),
        exit=StageExit.ERROR,
        result_valid=False,
        **LOST_SESSION,
        error="--resume で続けられなかった",
    )
    assert of_type(events, ExecutionRestarted)


def test_起こし方はTaskが元の状態と起こした跡から決める(task: TaskLoop):
    agg = task.aggregate
    begin(task, ex(S.IMPL), 1)
    assert agg.how_to_start(ex(S.IMPL)) == StageStart(StartMode.FRESH, resume_session=False)
    # 初めて始めたはずの実行に、もう起こした跡がある（配り直した StageStarted で走らせ直す）
    assert agg.how_to_start(ex(S.IMPL), agent_started=True) == StageStart(
        StartMode.INTERRUPTED, resume_session=True
    )
    task(MarkInterrupted(command_id=new_id(), issuer=DRIVER, task=T1, execution=ex(S.IMPL)))
    task(ResumeStage(command_id=new_id(), issuer=DRIVER, task=T1, execution=ex(S.IMPL)))
    assert agg.how_to_start(ex(S.IMPL)) == StageStart(StartMode.INTERRUPTED, resume_session=True)
    # 前の実行のセッションを続ける初めての実行は、プロンプトを渡して --resume で起こす
    task.report(ex(S.IMPL), result_valid=False)
    begin(task, ex(S.IMPL, attempt=2), 1)
    assert agg.how_to_start(ex(S.IMPL, attempt=2)) == StageStart(StartMode.FRESH, True)


@pytest.mark.parametrize("denials", [0, 10])
def test_resumeで続けられなかった証拠なら失敗に数えず作り直す(task: TaskLoop, denials: int):
    begin(task, ex(S.IMPL), 1)
    events = task.report(
        ex(S.IMPL),
        exit=StageExit.ERROR,
        result_valid=False,
        **LOST_SESSION,
        hook_denials=denials,
        error="No conversation",
    )
    (restarted,) = of_type(events, ExecutionRestarted)
    assert restarted.reason == "No conversation" and restarted.start_commit == HEAD
    assert not of_type(events, StageFailed)
    assert task.aggregate.reset_before_start(ex(S.IMPL, attempt=2)) == HEAD


def test_作り直した後に始められなかった試みを挟んでも次の試みは作り直した時点へ戻す(
    task: TaskLoop,
):
    """作り直しの反応が待ち切れずに戻さず、次の試みの begin も落ちた。その次の試みは戻さずに始めない。"""
    begin(task, ex(S.IMPL), 1)
    task.report(ex(S.IMPL), exit=StageExit.ERROR, result_valid=False, error="lost", **LOST_SESSION)
    task(
        ReportBeginFailure(
            command_id=new_id(),
            issuer=Issuer.executor(ex(S.IMPL, attempt=2)),
            task=T1,
            execution=ex(S.IMPL, attempt=2),
            error="同じ worktree で止めた実行が終わらない",
        )
    )
    agg = task.aggregate
    assert agg.executions[ex(S.IMPL, attempt=3)].status is ExecutionStatus.REQUESTED
    assert agg.reset_before_start(ex(S.IMPL, attempt=3)) == HEAD


def test_作り直した後に始めて落ちた試みの次は戻さない(task: TaskLoop):
    """遡るのは始めていない試みだけ。始めた試みは戻した時点から走ったので、落ちた後はそこから続ける。"""
    begin(task, ex(S.IMPL), 1)
    task.report(ex(S.IMPL), exit=StageExit.ERROR, result_valid=False, error="lost", **LOST_SESSION)
    begin(task, ex(S.IMPL, attempt=2), 1)
    task.report(ex(S.IMPL, attempt=2), result_valid=False)
    agg = task.aggregate
    assert agg.executions[ex(S.IMPL, attempt=3)].status is ExecutionStatus.REQUESTED
    assert agg.reset_before_start(ex(S.IMPL, attempt=3)) is None


def test_フックに止められ続けて打ち切ったなら続けられなかったのではなく失敗にする(task: TaskLoop):
    begin(task, ex(S.IMPL), 1)
    events = task.report(
        ex(S.IMPL),
        exit=StageExit.ERROR,
        result_valid=False,
        **LOST_SESSION,
        hook_denials=11,
        error="フックに 11 回止められた",
    )
    assert of_type(events, StageFailed) and not of_type(events, ExecutionRestarted)
    # 作り直していない次の試みは、戻さずに始める
    assert task.aggregate.reset_before_start(ex(S.IMPL, attempt=2)) is None


def test_書き直す前のフローの決定的なステージは期待する証拠が外れたら作ったことにしない(
    task: TaskLoop,
):
    """落ちた Gate の gated を、書き直した後のフローに残さない。"""
    task.run(ex(S.IMPL))
    for stage in (S.REVIEW, S.ADVERSARIAL_REVIEW):
        task.run(ex(stage, 1))
    task.run(ex(S.JUDGE, 1))
    task.conclude(ex(S.JUDGE, 1))
    begin(task, ex(S.GATE), None)
    # 範囲が変わってフローを捨てた後に、走っていた Gate が落ちた証拠を返す
    task(
        ChangeScope(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            spec=TaskSpec("新しい範囲"),
            artifacts=(),
            pointers=Pointers(),
        )
    )
    events = task.report(ex(S.GATE), gate=gate(GateItem.VERIFY))
    assert of_type(events, StageFailed) and not of_type(events, StageCompleted)
    assert A.GATED not in task.aggregate.artifacts


# --- マーカー ---


def test_マーカーは1回の走査で単独の行だけを置き換え知らないものは残す():
    body = (
        "a\n<!-- autodev:x -->\n `<!-- autodev:x -->`\n<!-- autodev:y -->\n  <!-- autodev:x --> \n"
    )
    out = fill(body, {"x": "<!-- autodev:y --> $$ \\1 ${x}"})
    assert out == (
        "a\n<!-- autodev:y --> $$ \\1 ${x}\n `<!-- autodev:x -->`\n<!-- autodev:y -->\n"
        "<!-- autodev:y --> $$ \\1 ${x}\n"
    )


# --- スキーマの照合 ---

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["report", "items"],
    "properties": {
        "report": {"enum": ["design-gap", None]},
        "reason": {"type": ["string", "null"], "minLength": 1, "maxLength": 3},
        "items": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "pattern": "^R[0-9]+$"},
        },
        "n": {"type": "integer", "minimum": 1},
    },
}


def test_スキーマの形に合わない所を挙げる():
    assert violations({"report": None, "items": ["R1"]}, SCHEMA) == []
    found = violations({"report": "x", "items": ["R1", "R1", "Q"], "n": 0, "z": 1}, SCHEMA)
    assert len(found) == 5
    assert violations({"report": None, "items": [], "n": True}, SCHEMA) == [
        "$.n: 型が integer でない"
    ]
    assert violations(None, SCHEMA) == ["$: 型が object でない"]
    assert violations({"report": None, "items": [], "reason": "abcd"}, SCHEMA) == [
        "$.reason: 文字列が長い"
    ]
    # ECMA の `$` と同じく、末尾の改行の手前では合わない
    assert violations({"report": None, "items": ["R1\n"]}, SCHEMA) == [
        "$.items[0]: 'R1\\n' が形 ^R[0-9]+$ に合わない"
    ]
    one_line = {"type": "string", "pattern": "^[^\\r\\n]+$"}
    assert violations("a", one_line) == []
    assert violations("a\n", one_line) != []
    assert violations("a$", {"type": "string", "pattern": "^[a$]+\\$$"}) == []


def _patterns(node: object) -> list[str]:
    if isinstance(node, dict):
        own = [node["pattern"]] if isinstance(node.get("pattern"), str) else []
        return own + [p for value in node.values() for p in _patterns(value)]
    if isinstance(node, list):
        return [p for value in node for p in _patterns(value)]
    return []


def test_schemasのpatternは終わりの印だけが文字列の終わりに変わる():
    found = [
        p
        for path in sorted((AUTODEV_PACKAGE / "schemas").glob("*.json"))
        for p in _patterns(json.loads(path.read_text(encoding="utf-8")))
    ]
    assert found
    for source in found:
        assert source.endswith("$") and source.count("$") == 1, source
        assert ecma_pattern(source).pattern == source[:-1] + r"\Z"


def test_nullを許す欄の文字列のnullとnoneをnullに直す():
    value = {"report": "None", "reason": "null", "items": ["null"]}
    assert normalize_nulls(value, SCHEMA) == {"report": None, "reason": None, "items": ["null"]}


# --- スナップショット ---


def test_Gateの事実は最後のReviewLoopの最後のラウンドのレビューを写す(task: TaskLoop, tmp_path):
    task.run(ex(S.IMPL))
    task.run(ex(S.REVIEW, 1))
    task.run(ex(S.ADVERSARIAL_REVIEW, 1))
    task.run(ex(S.JUDGE, 1))
    task.conclude(ex(S.JUDGE, 1))
    begin(task, ex(S.GATE), None)
    setting = RunSetting(
        paths=RunPaths(RunName("r"), tmp_path),
        repository=tmp_path,
        base=BranchName("main"),
        instruction="x",
    )
    ledger = ReviewLedger(StreamId.review(T1))
    aggregates = {StreamId.task(T1): task.aggregate, StreamId.review(T1): ledger}
    context = snapshot(aggregates, ex(S.GATE), setting)
    assert context.gate is not None
    assert context.gate.reviewers_expected == (S.REVIEW, S.ADVERSARIAL_REVIEW)
    assert set(context.gate.reviewers_completed) == {S.REVIEW, S.ADVERSARIAL_REVIEW}
    assert context.gate.has_test_gen is False
    assert context.tree == tmp_path / "trees" / "task1"
    assert context.resume is ResumeMode.FRESH and context.status is ExecutionStatus.RUNNING


def test_組み立ての根からはRunStartedの対象リポジトリとbaseと指示で組む(tmp_path):
    paths = RunPaths(RunName("r"), tmp_path / "run")
    started = RunStarted(
        RunName("r"),
        Instruction("足す"),
        Repository(str(tmp_path / "repo")),
        BranchName("develop"),
        ParallelLimit(ParallelLimit.DEFAULT),
    )
    history = [Delivery(1, EventId("run#1"), started, CommandId("c1"), "t")]
    parts = SimpleNamespace(
        paths=paths, aggregates=lambda: {}, prompts=None, history=lambda: history
    )
    executor = from_parts(parts, runtime=None)  # ty: ignore[invalid-argument-type]
    setting = executor.setting
    assert (setting.repository, setting.base, setting.instruction) == (
        tmp_path / "repo",
        BranchName("develop"),
        "足す",
    )
