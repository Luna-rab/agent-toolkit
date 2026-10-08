"""値オブジェクトの、作るときの検査（`autodev/domain/value_objects/`）。"""

from __future__ import annotations

import pytest

from autodev.domain.value_objects.base import InvalidValue
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.commit_sha import CommitSha
from autodev.domain.value_objects.cut_point import CutPoint
from autodev.domain.value_objects.decision import Decision
from autodev.domain.value_objects.decision_origin import DecisionOrigin
from autodev.domain.value_objects.design_version import DesignVersion
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.finding_id import FindingId
from autodev.domain.value_objects.gate_item import GateItem
from autodev.domain.value_objects.gate_item_result import GateItemResult
from autodev.domain.value_objects.gate_report import GateReport
from autodev.domain.value_objects.git_job import GitJob
from autodev.domain.value_objects.git_job_kind import GitJobKind
from autodev.domain.value_objects.instruction import Instruction
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.issuer_kind import IssuerKind
from autodev.domain.value_objects.location import Location
from autodev.domain.value_objects.model_class import (
    Effort,
    ModelChoice,
    ModelClass,
    ModelClasses,
    ModelName,
)
from autodev.domain.value_objects.overview_pr_title import OverviewPrTitle
from autodev.domain.value_objects.parallel_limit import ParallelLimit
from autodev.domain.value_objects.planned_task import PlannedTask
from autodev.domain.value_objects.pr_number import PrNumber
from autodev.domain.value_objects.proposal import Proposal
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.repository import Repository
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.session_id import SessionId
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_kind import TaskKind
from autodev.domain.value_objects.task_pr_title import TaskPrTitle
from autodev.domain.value_objects.task_spec import TaskSpec
from autodev.domain.value_objects.task_status import TaskStatus
from autodev.domain.value_objects.verify_command import VerifyCommand

SESSION = SessionId("0f8fad5b-d9cb-469f-a165-70867728950e")


@pytest.mark.parametrize("name", ["a", "add-cache", "x1-2", "a" * 49])
def test_ラン名は英小文字と数字とハイフンの49字まで(name: str):
    assert RunName(name).value == name


@pytest.mark.parametrize(
    "name", ["", "-a", "Add", "a_b", "a b", "../x", "a/b", "a" * 50, "a--b", "a-"]
)
def test_ブランチ名と置き場のパスを壊すラン名を拒む(name: str):
    with pytest.raises(InvalidValue):
        RunName(name)


def test_文字列でない値で包む値を作れない():
    with pytest.raises(InvalidValue):
        RunName(3)  # ty: ignore[invalid-argument-type]  JSON から読んだ値は型が保証されない
    with pytest.raises(InvalidValue):
        PrNumber(True)


def test_タスクidは番号付きか計画とgit管理の固定の名前():
    assert TaskId.numbered(3) == TaskId("task3")
    assert TaskId("task3").number == 3
    assert TaskId.planning().number is None
    assert TaskId.git().value == "git"
    assert TaskId("task3").kind is TaskKind.IMPLEMENTATION
    assert TaskId.planning().kind is TaskKind.PLANNING
    assert TaskId.git().kind is TaskKind.GIT
    for bad in ("task0", "task01", "task", "Task1", "plan"):
        with pytest.raises(InvalidValue):
            TaskId(bad)


def test_識別子の形():
    CommitSha("0123456789abcdef0123456789abcdef01234567")
    SessionId("0f8fad5b-d9cb-469f-a165-70867728950e")
    QuestionId("scope-1")
    for make, bad in (
        (CommitSha, "abc"),
        (CommitSha, "A" * 40),
        (SessionId, "not-a-uuid"),
        (QuestionId, "Scope"),
        (QuestionId, "-x"),
    ):
        with pytest.raises(InvalidValue):
            make(bad)


def test_指摘のidはタスクと設計とgateの項目で形が違う():
    assert FindingId.review(3) == FindingId("R3")
    assert FindingId.design(1) == FindingId("D1")
    assert FindingId.gate(GateItem.VERIFY) == FindingId("G-verify")
    for bad in ("R0", "r3", "X3", "G-", "G-Verify"):
        with pytest.raises(InvalidValue):
            FindingId(bad)


def test_autodevが切るブランチの名前の規約():
    run = RunName("add-cache")
    assert BranchName.overview(run) == BranchName("stack/add-cache--task-0")
    assert BranchName.for_task(run, 2) == BranchName("stack/add-cache--task-2")
    assert BranchName.for_task(run, 2, branch_round=1) == BranchName("stack/add-cache--task-2-r1")
    with pytest.raises(InvalidValue):
        BranchName.for_task(run, 0)


def test_概要PRのタイトルにはautodevの印を1つだけ付ける():
    expected = OverviewPrTitle("[autodev] キャッシュを足す")
    assert OverviewPrTitle.for_run("キャッシュを足す\n") == expected
    assert OverviewPrTitle.for_run("[autodev] キャッシュを足す") == expected
    for bad in (" ", "[autodev]"):
        with pytest.raises(InvalidValue):
            OverviewPrTitle.for_run(bad)
    for bad in ("キャッシュを足す", "[autodev #4] キャッシュを足す"):
        with pytest.raises(InvalidValue):
            OverviewPrTitle(bad)


def test_タスクPRのタイトルには概要PRの番号の印を1つだけ付ける():
    expected = TaskPrTitle("[autodev #4] キャッシュを足す")
    assert TaskPrTitle.for_task("キャッシュを足す\n", PrNumber(4)) == expected
    for already in ("[autodev #4] キャッシュを足す", "[autodev] キャッシュを足す"):
        assert TaskPrTitle.for_task(already, PrNumber(4)) == expected
    for bad in (" ", "[autodev #4]"):
        with pytest.raises(InvalidValue):
            TaskPrTitle.for_task(bad, PrNumber(4))
    for bad in ("キャッシュを足す", "[autodev] キャッシュを足す", "[autodev #0] キャッシュを足す"):
        with pytest.raises(InvalidValue):
            TaskPrTitle(bad)


@pytest.mark.parametrize("name", ["main", "release/1.2", "feature/x-y"])
def test_ランのbaseもブランチ名として持てる(name: str):
    BranchName(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-x",
        "a..b",
        "a//b",
        "a/",
        "/a",
        "a.lock",
        "a.lock/b",
        "a/.b",
        ".a",
        "a.",
        "HEAD",
        "a b",
        "a@{1}",
    ],
)
def test_gitが受けないブランチ名を拒む(name: str):
    with pytest.raises(InvalidValue):
        BranchName(name)


def test_ストリームのid():
    task = TaskId("task2")
    assert StreamId.task(task) == StreamId("task/task2")
    assert StreamId.review(task).is_review
    assert StreamId.design_review().is_review
    assert not StreamId.design().is_review
    for bad in ("task/", "review/x", "tasks/task2", "runs"):
        with pytest.raises(InvalidValue):
            StreamId(bad)


def test_イベントのidはストリームと版から決まる():
    event = EventId.of(StreamId.task(TaskId("task2")), 7)
    assert event == EventId("task/task2#7")
    assert event.stream == StreamId("task/task2")
    assert event.version == 7
    for bad in ("task/task2#0", "task/task2", "nope#1"):
        with pytest.raises(InvalidValue):
            EventId(bad)


def test_ポリシーのコマンドのidは受けたイベントと受け手の名前から決まる():
    event = EventId("task/task2#7")
    assert CommandId.derived(event, "stall") == CommandId.derived(event, "stall")
    assert CommandId.derived(event, "stall") != CommandId.derived(event, "stall", 1)
    assert CommandId.derived(event, "stall") != CommandId.derived(event, "gate")


def test_実行のidはラウンドが0以上で試行が1以上():
    execution = ExecutionId(TaskId("task2"), StageKind.JUDGE, 4, 1)
    assert str(execution) == "task2-Judge-r4-a1"
    with pytest.raises(InvalidValue):
        ExecutionId(TaskId("task2"), StageKind.JUDGE, -1, 1)
    with pytest.raises(InvalidValue):
        ExecutionId(TaskId("task2"), StageKind.JUDGE, 1, 0)


def test_回答はユーザーのものだけが質問のidを持つ():
    human = Decision("A にする", DecisionOrigin.USER, QuestionId("q"))
    assert human.is_human
    assert not Decision("B にする", DecisionOrigin.RUN_SUPERVISOR).is_human
    with pytest.raises(InvalidValue):
        Decision("A にする", DecisionOrigin.USER)
    with pytest.raises(InvalidValue):
        Decision("B にする", DecisionOrigin.RUN_SUPERVISOR, QuestionId("q"))
    with pytest.raises(InvalidValue):
        Decision(" ", DecisionOrigin.RUN_SUPERVISOR)


def test_出す者は種類ごとに要る欄がある():
    event = EventId("task/task2#7")
    assert Issuer.policy("stall", event).kind is IssuerKind.POLICY
    with pytest.raises(InvalidValue):
        Issuer(IssuerKind.POLICY, name="stall")
    with pytest.raises(InvalidValue):
        Issuer(IssuerKind.TASK_SUPERVISOR)
    with pytest.raises(InvalidValue):
        Issuer(IssuerKind.EXECUTOR)
    with pytest.raises(InvalidValue):
        Issuer(IssuerKind.RUN_SUPERVISOR)
    Issuer.cli()
    Issuer.run_supervisor(SESSION)


def test_セッションを持つのは実装タスクの統括だけ():
    Issuer.task_supervisor(TaskId("task2"), SESSION)
    Issuer.task_supervisor(TaskId.git())
    Issuer.task_supervisor(TaskId.planning())
    # 実装タスクの統括は LLM なので、セッションの無い名乗りは偽り
    with pytest.raises(InvalidValue):
        Issuer.task_supervisor(TaskId("task2"))
    # 計画タスクと git 管理タスクの統括はプログラムなので、セッションを持たない
    with pytest.raises(InvalidValue):
        Issuer.task_supervisor(TaskId.git(), SESSION)


def test_指摘の位置は行番号と範囲を省ける():
    assert Location("src/a.py").lines is None
    assert Location("src/a.py:3").path == "src/a.py"
    assert Location("src/a.py:3").lines == (3, 3)
    assert Location("src/a.py:12-15").lines == (12, 15)
    for bad in ("", " src/a.py", "src/a.py:15-12"):
        with pytest.raises(InvalidValue):
            Location(bad)


def test_空の文字列を拒む値():
    for make in (Instruction, VerifyCommand):
        with pytest.raises(InvalidValue):
            make("  ")
    with pytest.raises(InvalidValue):
        TaskSpec(title="")
    with pytest.raises(InvalidValue):
        Repository("relative/path")


def test_数の下限():
    assert ParallelLimit(ParallelLimit.DEFAULT).value == 3
    assert DesignVersion(2).path == "design/v2.md"
    for make in (ParallelLimit, PrNumber, DesignVersion):
        with pytest.raises(InvalidValue):
            make(0)


def test_終端のタスクの状態():
    terminal = {status for status in TaskStatus if status.is_terminal}
    assert terminal == {
        TaskStatus.STACKED,
        TaskStatus.DROPPED,
        TaskStatus.SUPERSEDED,
        TaskStatus.DISCARDED,
        TaskStatus.FINISHED,
    }


def test_gateの落ちを項目で見分ける():
    report = GateReport(
        (
            GateItemResult(GateItem.COMMITS, True),
            GateItemResult(GateItem.VERIFY, False, "pytest が落ちた"),
        )
    )
    assert not report.passed
    assert [r.item for r in report.failed] == [GateItem.VERIFY]
    assert {item: item.escalation for item in GateItem} == {
        GateItem.COMMITS: EscalationKind.GATE_UNFIXABLE,
        GateItem.NO_OPEN_FINDINGS: None,
        GateItem.REVIEWERS_RAN: EscalationKind.GATE_UNFIXABLE,
        GateItem.TESTS_UNCHANGED: EscalationKind.GATE_UNFIXABLE,
        GateItem.UNTESTED_PATHS: EscalationKind.UNTESTED_CHANGE,
        GateItem.VERIFY: None,
    }


def test_提案の中でタスクのidを重ねない():
    task = PlannedTask(TaskId("task1"), TaskSpec(title="x"))
    with pytest.raises(InvalidValue):
        Proposal(DesignVersion(1), (task, task))


def test_CutBranchが切る元は仕事が決める():
    main, top = BranchName("main"), BranchName("stack/r--task-0")
    old, new = BranchName("stack/r--task-1"), BranchName("stack/r--task-1-r1")
    J = GitJobKind
    overview = GitJob(1, J.CUT_OVERVIEW, branch=top, base=main)
    assert overview.cut_point == CutPoint(main, run_base=True)
    assert GitJob(2, J.CUT_TASK, branch=old, base=top).cut_point == CutPoint(top)
    assert GitJob(3, J.CUT_STACK_TOP, base=top).cut_point == CutPoint(top, detached=True)
    restack = GitJob(4, J.STACK, branch=new, base=top, previous=old)
    # 積み直すタスクは前に積んだブランチから切るが、その先端はタスクのブランチの根元ではない
    assert restack.cut_point == CutPoint(old, roots_branch=False)
    assert GitJob(5, J.DISCARD).cut_point is None


def test_切る元の候補はランのbaseならoriginを先に並べautodevが切ったブランチは手元だけ():
    main, top = BranchName("main"), BranchName("stack/r--task-0")
    assert CutPoint.for_overview(main).refs() == ("origin/main", "main")
    assert CutPoint(top).refs() == ("stack/r--task-0",)
    assert CutPoint(top, detached=True).refs() == ("stack/r--task-0",)


@pytest.mark.parametrize("name", ["opus", "sonnet", "claude-opus-5-5"])
def test_モデルの名前は空でない文字列なら何でも通す(name: str):
    assert ModelName(name).value == name


@pytest.mark.parametrize("name", ["", "  "])
def test_モデルの名前は空と空白だけを拒む(name: str):
    with pytest.raises(InvalidValue):
        ModelName(name)


def test_effortはclaudeとcodexとcopilotのeffortの和の7つの値だけ():
    assert {Effort(v) for v in ("none", "minimal", "low", "medium", "high", "xhigh", "max")} == set(
        Effort
    )
    with pytest.raises(ValueError):
        Effort("huge")


def test_既定のモデルのクラスはleadとreviewがopusでimplementとwriteがsonnet():
    default = ModelClasses.default()
    assert {c: default.of(c) for c in ModelClass} == {
        ModelClass.LEAD: ModelChoice(ModelName("opus"), Effort.HIGH),
        ModelClass.REVIEW: ModelChoice(ModelName("opus"), Effort.MEDIUM),
        ModelClass.IMPLEMENT: ModelChoice(ModelName("sonnet"), Effort.MEDIUM),
        ModelClass.WRITE: ModelChoice(ModelName("sonnet"), Effort.MEDIUM),
    }


def test_モデルのクラスの値は渡した欄だけを替えた新しい値を返す():
    default = ModelClasses.default()
    changed = default.with_choice(ModelClass.IMPLEMENT, effort=Effort.XHIGH)
    assert changed.of(ModelClass.IMPLEMENT) == ModelChoice(ModelName("sonnet"), Effort.XHIGH)
    renamed = changed.with_choice(ModelClass.IMPLEMENT, model=ModelName("claude-opus-5-5"))
    assert renamed.of(ModelClass.IMPLEMENT) == ModelChoice(
        ModelName("claude-opus-5-5"), Effort.XHIGH
    )
    others = [c for c in ModelClass if c is not ModelClass.IMPLEMENT]
    assert [renamed.of(c) for c in others] == [default.of(c) for c in others]
    # 元の値は変わらない
    assert default.of(ModelClass.IMPLEMENT) == ModelChoice(ModelName("sonnet"), Effort.MEDIUM)
