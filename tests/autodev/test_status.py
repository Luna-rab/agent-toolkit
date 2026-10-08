"""`status --json` の組み立てと進み具合のファイル（`infra/status.py`）・ランディレクトリの置き場（`infra/paths.py`）。"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from autodev_fakes import Queue, enqueue, execution, factory, task

from autodev.adapters.process import children
from autodev.app.driving.mainloop import MainLoop
from autodev.domain.events.base import Event
from autodev.domain.events.design import DesignAmbiguous, DesignProposed, DesignSettled
from autodev.domain.events.questions import QuestionPosted
from autodev.domain.events.review_ledger import FindingClosed, FindingRaised, FixCounted
from autodev.domain.events.run import (
    EscalationRaised,
    RunFinished,
    RunPanicked,
    RunResumed,
    RunStarted,
    TaskMarkedStacked,
    TasksPlanned,
    TasksStopped,
    TaskStarted,
    TaskStatusChanged,
)
from autodev.domain.events.stack import OverviewRecorded, TaskStacked
from autodev.domain.events.task import (
    ExecutionRestarted,
    FlowAccepted,
    GateFailed,
    HandoffFailed,
    NoteAdded,
    StageCancelled,
    StageCompleted,
    StageFailed,
    StageInterrupted,
    StageReported,
    StageRequested,
    StageStarted,
    TaskOpened,
)
from autodev.domain.flow.flow import Cursor, Flow, FlowStep
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.artifact_ref import ArtifactRef
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.commit_sha import CommitSha
from autodev.domain.value_objects.decision import Decision
from autodev.domain.value_objects.decision_origin import DecisionOrigin
from autodev.domain.value_objects.design_version import DesignVersion
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.finding_id import FindingId
from autodev.domain.value_objects.gate_item import GateItem
from autodev.domain.value_objects.gate_item_result import GateItemResult
from autodev.domain.value_objects.instruction import Instruction
from autodev.domain.value_objects.interrupt_cause import InterruptCause
from autodev.domain.value_objects.location import Location
from autodev.domain.value_objects.model_class import (
    AgentKind,
    Effort,
    ModelClass,
    ModelClasses,
    ModelName,
)
from autodev.domain.value_objects.parallel_limit import ParallelLimit
from autodev.domain.value_objects.planned_task import PlannedTask
from autodev.domain.value_objects.pointers import Pointers
from autodev.domain.value_objects.pr_number import PrNumber
from autodev.domain.value_objects.proposal import Proposal
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.rating import Rating
from autodev.domain.value_objects.repository import Repository
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.stack_entry import StackEntry
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_kind import TaskKind
from autodev.domain.value_objects.task_spec import TaskSpec
from autodev.domain.value_objects.task_status import TaskStatus
from autodev.infra.files import utc_now
from autodev.infra.lock import DriverLock
from autodev.infra.paths import RunPaths, state_root
from autodev.infra.status import status as status_module
from autodev.infra.status import status_sections
from autodev.infra.status.status import (
    all_statuses,
    build_status,
    prune_progress,
    read_progress,
    remove_progress,
    run_status,
    write_progress,
)
from autodev.infra.status.status_sections import Replayed
from autodev.infra.store.eventstore import EventStore
from autodev.infra.store.rejections import Rejection, RejectionLog, read_rejections


@pytest.fixture
def paths(tmp_path: Path) -> RunPaths:
    return RunPaths.of(RunName("demo"), {"AUTODEV_STATE_DIR": str(tmp_path)})


def waiting(view: Replayed) -> list[str]:
    queue = view.aggregates.get(StreamId.stack())
    return [t.value for t in queue.waiting] if isinstance(queue, Queue) else []


def test_再生した集約に差し込んだ欄と進み具合と拒んだ記録を混ぜて返す(paths: RunPaths):
    loop = MainLoop(
        EventStore.open(paths.events_db), factory, [], on_rejected=RejectionLog(paths.rejections)
    )
    loop.process(enqueue(1, "c1"))
    loop.process(enqueue(2, "c2"))
    loop.process(enqueue(1, "c3"))
    write_progress(paths, execution(1), {"turns": 3, "tool": "Bash"})
    # driver の書く接続を開いたまま読む
    status = build_status(
        paths,
        factory,
        {"stack": waiting, "progress": lambda view: dict(view.progress)},
    )
    assert status["format"] == 2
    assert status["name"] == "demo"
    assert status["last_seq"] == 2
    assert status["updated_at"]
    assert status["stack"] == ["task1", "task2"]
    assert status["progress"] == {"task1-Impl-r0-a1": {"turns": 3, "tool": "Bash"}}
    assert [(r["command_id"], r["reason"]) for r in status["rejections"]] == [
        ("c3", "すでに列にある")
    ]


def test_拒んだ記録はcommand_idと理由の組で重ねない(paths: RunPaths):
    def rejection(command_id: str, reason: str) -> Rejection:
        return Rejection("x", "EnqueueStack", command_id, {"kind": "policy"}, reason)

    log = RejectionLog(paths.rejections)
    log(rejection("c1", "すでに列にある"))
    log(rejection("c1", "すでに列にある"))
    # 落ちた後の配り直しで、開き直した driver がもう一度拒む
    again = RejectionLog(paths.rejections)
    again(rejection("c1", "すでに列にある"))
    again(rejection("c2", "別の理由"))
    assert [r["command_id"] for r in read_rejections(paths.rejections)] == ["c1", "c2"]


def test_同じidの判断が別の理由で拒まれたら行を足す(paths: RunPaths):
    # 統括の判断の id は知らせ 1 つに 1 つ。同じ知らせで差し戻されて出し直した判断は同じ id になる
    def rejection(reason: str) -> Rejection:
        return Rejection("x", "AnswerEscalation", "d1", {"kind": "run-supervisor"}, reason)

    log = RejectionLog(paths.rejections)
    log(rejection("1 回目の理由"))
    log(rejection("2 回目の理由"))
    RejectionLog(paths.rejections)(rejection("2 回目の理由"))
    assert [r["reason"] for r in read_rejections(paths.rejections)] == [
        "1 回目の理由",
        "2 回目の理由",
    ]


def test_走っていない実行の進み具合を片付ける(paths: RunPaths):
    prune_progress(paths, [])
    for number in (1, 2):
        write_progress(paths, execution(number), {"turns": number})
    prune_progress(paths, [execution(2)])
    assert list(read_progress(paths)) == ["task2-Impl-r0-a1"]


def test_ランが無ければ作らずに落ちる(paths: RunPaths):
    with pytest.raises(FileNotFoundError):
        build_status(paths, factory, {})
    assert not paths.root.exists()


def test_骨組みの欄と同じ名前は差し込めない(paths: RunPaths):
    with pytest.raises(ValueError, match="rejections"):
        build_status(paths, factory, {"rejections": waiting})


def test_進み具合は書き直せて消せて読めないものは飛ばす(paths: RunPaths):
    write_progress(paths, execution(1), {"turns": 1})
    write_progress(paths, execution(1), {"turns": 2})
    write_progress(paths, execution(2), {"turns": 5})
    (paths.progress / "broken.json").write_text("{", encoding="utf-8")
    assert read_progress(paths) == {
        "task1-Impl-r0-a1": {"turns": 2},
        "task2-Impl-r0-a1": {"turns": 5},
    }
    remove_progress(paths, execution(1))
    remove_progress(paths, execution(1))
    assert list(read_progress(paths)) == ["task2-Impl-r0-a1"]
    # 一時ファイルを残していない
    assert sorted(p.name for p in paths.progress.iterdir()) == [
        "broken.json",
        "task2-Impl-r0-a1.json",
    ]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"AUTODEV_STATE_DIR": "/srv/runs", "XDG_STATE_HOME": "/x"}, "/srv/runs"),
        ({"XDG_STATE_HOME": "/x/state"}, "/x/state/autodev"),
        # 相対パスの XDG_STATE_HOME は無効（XDG の仕様）
        ({"XDG_STATE_HOME": "rel"}, str(Path.home() / ".local/state/autodev")),
        ({}, str(Path.home() / ".local/state/autodev")),
    ],
)
def test_ランの置き場はXDG_STATE_HOMEを見て差し替えられる(env: dict[str, str], expected: str):
    assert state_root(env) == Path(expected)


def test_ランディレクトリの中のパス():
    paths = RunPaths.of(RunName("demo"), {"AUTODEV_STATE_DIR": "/s"})
    assert paths.events_db == Path("/s/demo/events.db")
    assert paths.progress_of(execution(3)) == Path("/s/demo/progress/task3-Impl-r0-a1.json")
    assert paths.answer("toolu_1") == Path("/s/demo/answers/toolu_1.json")
    assert paths.task_tree(task(2)) == Path("/s/demo/trees/task2")
    assert paths.task_results(task(2)) == Path("/s/demo/tasks/task2/results")


# --- 本物の集約で再生した欄（status_sections.SECTIONS） ---

RUN = StreamId.run()
PLANNING = TaskId.planning()
GIT = TaskId.git()
T1 = TaskId.numbered(1)
T2 = TaskId.numbered(2)
HEAD = CommitSha("a" * 40)
OVERVIEW = StackEntry(GIT, BranchName("autodev/demo"), PrNumber(4), BranchName("main"))
PLAN = (
    PlannedTask(T1, TaskSpec("パーサを足す")),
    PlannedTask(T2, TaskSpec("CLI に出す"), frozenset({T1})),
)
ARTIFACTS = (ArtifactRef(ArtifactKind.BRIEF, "brief.md"), ArtifactRef(ArtifactKind.DESIGN, "1"))


def ex(task: TaskId, stage: StageKind, round: int = 0) -> ExecutionId:
    return ExecutionId(task, stage, round, 1)


class Clock:
    """イベントの `at` に入れる時刻。`now` を書き換えると、次に書くイベントの時刻が変わる。"""

    def __init__(self, now: str = "2026-10-04T00:00:00.000000Z") -> None:
        self.now = now

    def __call__(self) -> str:
        return self.now


class Seed:
    """イベントの列を、driver を通さずにそのまま events.db へ書く。"""

    def __init__(self, paths: RunPaths, clock: Callable[[], str] = utc_now) -> None:
        self.store = EventStore.open(paths.events_db, clock)
        self.versions: dict[StreamId, int] = {}
        self.commands = 0

    def __call__(self, stream: StreamId, *events: Event) -> None:
        version = self.versions.get(stream, 0)
        self.commands += 1
        self.store.append(CommandId(f"c{self.commands}"), stream, version, events)
        self.versions[stream] = version + len(events)


def started(name: str = "demo") -> RunStarted:
    return RunStarted(
        RunName(name),
        Instruction("足す"),
        Repository("/repo"),
        BranchName("main"),
        ParallelLimit(2),
    )


def seed_running(seed: Seed) -> None:
    """計画を反映し、task1 が Impl を走らせ、計画タスクが ask で回答を待っているラン。"""
    seed(RUN, started(), TaskStarted(PLANNING, TaskKind.PLANNING), TaskStarted(GIT, TaskKind.GIT))
    seed(StreamId.design(), DesignProposed(Proposal(DesignVersion(1), PLAN)))
    seed(
        StreamId.design(),
        DesignSettled(Proposal(DesignVersion(1), PLAN), ex(PLANNING, StageKind.DESIGN_JUDGE, 1)),
    )
    seed(RUN, TasksPlanned(DesignVersion(1), PLAN, (), ARTIFACTS, replan=False))
    seed(
        RUN,
        TaskStarted(
            T1, TaskKind.IMPLEMENTATION, spec=PLAN[0].spec, branch=BranchName("stack/demo--task-1")
        ),
    )
    seed(StreamId.stack(), OverviewRecorded(OVERVIEW))

    t1 = StreamId.task(T1)
    flow = Flow(
        (
            FlowStep(StageKind.TEST_GEN),
            FlowStep(StageKind.RESOLVE_CONFLICT),
            FlowStep(StageKind.IMPL),
            FlowStep(StageKind.GATE),
        ),
        1,
    )
    seed(
        t1,
        TaskOpened(TaskKind.IMPLEMENTATION, PLAN[0].spec, branch=BranchName("stack/demo--task-1")),
        FlowAccepted(flow),
        StageRequested(ex(T1, StageKind.TEST_GEN), 0),
        StageStarted(ex(T1, StageKind.TEST_GEN), HEAD),
        # ResolveConflict を飛ばした先へ進む
        StageCompleted(ex(T1, StageKind.TEST_GEN), cursor=Cursor(2)),
        StageRequested(ex(T1, StageKind.IMPL), 2),
        StageStarted(ex(T1, StageKind.IMPL), HEAD),
    )

    planning = StreamId.task(PLANNING)
    seed(
        planning,
        TaskOpened(TaskKind.PLANNING),
        FlowAccepted(Flow((FlowStep(StageKind.REPLAN),), 1)),
        StageRequested(ex(PLANNING, StageKind.REPLAN), 0),
        StageStarted(ex(PLANNING, StageKind.REPLAN), HEAD),
        EscalationRaised(
            EscalationKind.ASK, Pointers(), task=PLANNING, origin=ex(PLANNING, StageKind.REPLAN)
        ),
    )
    seed(
        RUN,
        TaskStatusChanged(PLANNING, TaskStatus.RUNNING, TaskStatus.ESCALATED, "EscalationRaised"),
        EscalationRaised(
            EscalationKind.ASK, Pointers(), task=PLANNING, source=EventId("task/planning#5")
        ),
    )
    seed(
        StreamId.questions(),
        QuestionPosted(QuestionId("q1"), "どちらの形にするか", EventId("run#7")),
    )
    seed(
        StreamId.design(),
        DesignProposed(Proposal(DesignVersion(2), PLAN)),
        DesignAmbiguous(ex(PLANNING, StageKind.DESIGN_JUDGE, 1)),
    )


def test_走っているランのタスクと実行と回答待ちを見せる(paths: RunPaths):
    seed_running(Seed(paths))
    write_progress(paths, ex(T1, StageKind.IMPL), {"turns": 7, "lastTool": "Edit"})

    status = run_status(paths)

    assert status["run"].pop("started_at") <= status["updated_at"]
    assert status["run"] == {
        "phase": "running",
        "awaiting_answer": True,
        "repository": "/repo",
        "base": "main",
        "limit": 2,
        "resumes": 0,
        "panic_cause": None,
        "directory": str(paths.root.absolute()),
        "driver_running": False,
        "live_children": [],
        # 回答を待っているので、driver が止まっていても止まったとは見せない
        "driver_stopped": False,
        "stacked_tasks": 0,
        "stack_target_tasks": 2,
        # models の欄の無い RunStarted（この変更より前のラン）は既定値で動くので、既定値を見せる
        "models": DEFAULT_MODELS,
    }
    assert [(t["id"], t["kind"], t["status"]) for t in status["tasks"]] == [
        ("planning", "planning", "escalated"),
        ("git", "git", "running"),
        ("task1", "implementation", "running"),
        ("task2", "implementation", "pending"),
    ]
    task1 = status["tasks"][2]
    assert task1["title"] == "パーサを足す"
    assert task1["branch"] == "stack/demo--task-1"
    assert task1["pr"] is None
    assert [(s["stage"], s["state"]) for s in task1["flow"]["steps"]] == [
        ("TestGen", "done"),
        ("ResolveConflict", "skipped"),
        ("Impl", "current"),
        ("Gate", "pending"),
    ]
    assert all("executions" not in t for t in status["tasks"])
    assert [[(e["id"], e["status"]) for e in s["executions"]] for s in task1["flow"]["steps"]] == [
        [("task1-TestGen-r0-a1", "completed")],
        [],
        [("task1-Impl-r0-a1", "running")],
        [],
    ]
    assert (task1["earlier_executions"], task1["unplaced_executions"]) == ([], [])
    (running,) = task1["flow"]["steps"][2]["executions"]
    assert running["progress"] == {"turns": 7, "lastTool": "Edit"}
    assert running["started_at"]
    assert status["tasks"][3]["blocked_by"] == ["task1"]
    assert status["tasks"][3]["flow"] is None
    assert status["tasks"][0]["escalations"] == [
        {
            "id": "task/planning#5",
            "kind": "ask",
            "origin": "planning-Replan-r0-a1",
            "reason": "",
            "question": None,
        }
    ]

    assert status["escalations"] == [
        {
            "id": "run#7",
            "kind": "ask",
            "task": "planning",
            "source": "task/planning#5",
            "for_user": False,
            "answer_only": False,
            "failures": 0,
            "reason": "",
            "question": None,
        }
    ]
    assert status["questions"] == [
        {"id": "q1", "body": "どちらの形にするか", "escalation": "run#7"}
    ]
    assert status["stack"]["overview"] == {
        "task": "git",
        "branch": "autodev/demo",
        "pr": 4,
        "base": "main",
    }
    assert status["stack"]["top"] == "autodev/demo"
    assert status["plan"] == {
        "planning": False,
        "planned": True,
        "applied_design": 1,
        "replans_without_stack": 0,
        "design": {
            "versions": [1, 2],
            "settled": 1,
            "proposal": {
                "version": 2,
                "state": "awaiting",
                "round": 1,
                "awaiting": "design-ambiguous",
            },
        },
        "findings": [],
    }
    json.dumps(status)


def test_積んだタスクと止めたタスクとランの終わりを見分けられる(paths: RunPaths):
    seed = Seed(paths)
    seed(RUN, started(), TaskStarted(PLANNING, TaskKind.PLANNING), TaskStarted(GIT, TaskKind.GIT))
    seed(RUN, TasksPlanned(DesignVersion(1), PLAN, (), ARTIFACTS, replan=False))
    seed(RUN, TaskStarted(T1, TaskKind.IMPLEMENTATION, spec=PLAN[0].spec))
    seed(
        RUN,
        TaskStatusChanged(T1, TaskStatus.RUNNING, TaskStatus.GATED, "TaskGated"),
        TaskStatusChanged(T1, TaskStatus.GATED, TaskStatus.STACKING, "GitJobTaken"),
        TaskMarkedStacked(T1, PrNumber(5)),
        TasksStopped(frozenset({T2})),
        RunFinished(ready_overview=True),
    )
    entry = StackEntry(T1, BranchName("stack/demo--task-1"), PrNumber(5), OVERVIEW.branch)
    seed(StreamId.stack(), OverviewRecorded(OVERVIEW), TaskStacked(entry))

    status = run_status(paths)
    # git 管理タスクの仕上げが残っている
    assert status["run"]["phase"] == "finishing"
    by_id = {t["id"]: t for t in status["tasks"]}
    assert (by_id["task1"]["status"], by_id["task1"]["pr"]) == ("stacked", 5)
    assert (by_id["task2"]["status"], by_id["task2"]["terminal"]) == ("dropped", True)
    assert by_id["planning"]["status"] == "finished"
    assert status["stack"]["entries"] == [
        {"task": "task1", "branch": "stack/demo--task-1", "pr": 5, "base": "autodev/demo"}
    ]

    seed(RUN, TaskStatusChanged(GIT, TaskStatus.RUNNING, TaskStatus.FINISHED, "FlowFinished"))
    assert run_status(paths)["run"]["phase"] == "finished"


DEFAULT_MODELS = {
    "lead": {"model": "opus", "effort": "high", "agent": "claude"},
    "review": {"model": "opus", "effort": "medium", "agent": "claude"},
    "implement": {"model": "sonnet", "effort": "medium", "agent": "claude"},
    "write": {"model": "sonnet", "effort": "medium", "agent": "claude"},
}


def test_既定で始めたランはrunのmodelsにクラスごとの既定のモデルとeffortを見せる(paths: RunPaths):
    Seed(paths)(RUN, dataclasses.replace(started(), models=ModelClasses.default()))
    assert run_status(paths)["run"]["models"] == DEFAULT_MODELS


def test_modelsを替えて始めたランはrunのmodelsに替えた値を見せる(paths: RunPaths):
    models = (
        ModelClasses.default()
        .with_choice(ModelClass.IMPLEMENT, model=ModelName("claude-opus-5-5"), effort=Effort.XHIGH)
        .with_choice(ModelClass.WRITE, effort=Effort.LOW)
    )
    Seed(paths)(RUN, dataclasses.replace(started(), models=models))
    assert run_status(paths)["run"]["models"] == {
        **DEFAULT_MODELS,
        "implement": {"model": "claude-opus-5-5", "effort": "xhigh", "agent": "claude"},
        "write": {"model": "sonnet", "effort": "low", "agent": "claude"},
    }


def test_codexを記録したランはrunのmodelsのそのクラスにagentのcodexを見せる(paths: RunPaths):
    models = ModelClasses.default().with_choice(
        ModelClass.REVIEW, model=ModelName("gpt-5.5"), effort=Effort.HIGH, agent=AgentKind.CODEX
    )
    Seed(paths)(RUN, dataclasses.replace(started(), models=models))
    assert run_status(paths)["run"]["models"] == {
        **DEFAULT_MODELS,
        "review": {"model": "gpt-5.5", "effort": "high", "agent": "codex"},
    }


def test_パニックしたランは終えた後でもパニックと見せる(paths: RunPaths):
    seed = Seed(paths)
    seed(RUN, started(), TaskStarted(GIT, TaskKind.GIT))
    assert run_status(paths)["run"]["phase"] == "planning"
    assert run_status(paths)["run"]["panic_cause"] is None
    seed(RUN, RunFinished(ready_overview=False), RunPanicked("利用枠"))
    run = run_status(paths)["run"]
    assert (run["phase"], run["panic_cause"]) == ("panicked", "利用枠")
    # 呼び直したら、もうパニックしていない
    seed(RUN, RunResumed((GIT,), after_panic=True))
    run = run_status(paths)["run"]
    assert (run["phase"], run["panic_cause"]) == ("finishing", None)


def test_StartRunを拒まれてイベントが無いランも読める(paths: RunPaths):
    EventStore.open(paths.events_db).close()
    status = run_status(paths)
    assert (status["last_seq"], status["updated_at"]) == (0, None)
    assert status["run"]["phase"] == "not-started"
    assert (status["tasks"], status["stack"]["overview"], status["plan"]["design"]["proposal"]) == (
        [],
        None,
        None,
    )


def test_全ランをラン名の順に返し読めないランはerrorで残す(tmp_path: Path):
    env = {"AUTODEV_STATE_DIR": str(tmp_path)}
    for name in ("zeta", "alpha"):
        Seed(RunPaths.of(RunName(name), env))(RUN, started(name))
    # events.db の無い所はランではない
    (tmp_path / "pr-body-markers").mkdir()
    (tmp_path / "Bad_Name").mkdir()
    (tmp_path / "Bad_Name" / "events.db").write_bytes(b"")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "events.db").write_bytes(b"not sqlite")

    found = all_statuses(env)
    assert [(s["name"], "error" in s) for s in found] == [
        ("Bad_Name", True),
        ("alpha", False),
        ("broken", True),
        ("zeta", False),
    ]
    assert found[0]["error"].startswith("InvalidValue: ")
    assert all_statuses({"AUTODEV_STATE_DIR": str(tmp_path / "none")}) == []


def test_欄を作る所の不具合では全ランをerrorで残して警告する(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    env = {"AUTODEV_STATE_DIR": str(tmp_path)}
    for name in ("alpha", "beta"):
        Seed(RunPaths.of(RunName(name), env))(RUN, started(name))

    def broken_section(view: Replayed) -> None:
        raise KeyError("x")

    monkeypatch.setitem(status_sections.SECTIONS, "run", broken_section)

    assert all_statuses(env) == [
        {"name": "alpha", "error": "KeyError: 'x'"},
        {"name": "beta", "error": "KeyError: 'x'"},
    ]
    assert [r.levelname for r in caplog.records] == ["WARNING", "WARNING"]


def test_型の違う集約を空の集約に置き換えない(paths: RunPaths):
    loop = MainLoop(
        EventStore.open(paths.events_db), factory, [], on_rejected=RejectionLog(paths.rejections)
    )
    loop.process(enqueue(1, "c1"))
    with pytest.raises(TypeError, match="Queue"):
        build_status(paths, factory, {"stack": lambda view: view.stack})


def step_ids(task: dict[str, Any]) -> list[list[str]]:
    return [[e["id"] for e in step["executions"]] for step in task["flow"]["steps"]]


def test_書き直す前のフローで走っている実行は段の外の前の版の欄にだけ出る(paths: RunPaths):
    seed = Seed(paths)
    seed(RUN, started(), TaskStarted(T1, TaskKind.IMPLEMENTATION, spec=PLAN[0].spec))
    done = ex(T1, StageKind.TEST_GEN)
    old = ex(T1, StageKind.IMPL)
    again = ExecutionId(T1, StageKind.TEST_GEN, 0, 2)
    steps = (FlowStep(StageKind.TEST_GEN), FlowStep(StageKind.IMPL))
    seed(
        StreamId.task(T1),
        TaskOpened(TaskKind.IMPLEMENTATION, PLAN[0].spec),
        FlowAccepted(Flow(steps, 1)),
        StageRequested(done, 0),
        StageStarted(done, HEAD),
        StageCompleted(done, cursor=Cursor(1)),
        StageRequested(old, 1),
        StageStarted(old, HEAD),
        # 版 2 も同じ段の並び。段の添字（step）だけでは版 1 の Impl を見分けられない
        FlowAccepted(Flow(steps, 2)),
        StageRequested(again, 0),
        StageStarted(again, HEAD),
    )
    # 完了した実行の進み具合のファイルが残っていても見せない
    write_progress(paths, old, {"turns": 3})
    write_progress(paths, done, {"turns": 9})

    (task1,) = run_status(paths)["tasks"]
    assert task1["flow"]["version"] == 2
    assert step_ids(task1) == [["task1-TestGen-r0-a2"], []]
    assert [
        (e["id"], e["flow_version"], e["step"], e["status"], e["progress"])
        for e in task1["earlier_executions"]
    ] == [("task1-Impl-r0-a1", 1, 1, "running", {"turns": 3})]
    assert task1["unplaced_executions"] == []


def test_今の版の実行で段の添字に当たらないものは段の外の欄に出る(paths: RunPaths):
    seed = Seed(paths)
    seed(RUN, started(), TaskStarted(T1, TaskKind.IMPLEMENTATION, spec=PLAN[0].spec))
    placed = ex(T1, StageKind.IMPL)
    stray = ex(T1, StageKind.GATE)
    seed(
        StreamId.task(T1),
        TaskOpened(TaskKind.IMPLEMENTATION, PLAN[0].spec),
        FlowAccepted(Flow((FlowStep(StageKind.IMPL),), 1)),
        StageRequested(placed, 0),
        StageStarted(placed, HEAD),
        StageRequested(stray, 3),
    )

    (task1,) = run_status(paths)["tasks"]
    assert step_ids(task1) == [["task1-Impl-r0-a1"]]
    assert [(e["id"], e["step"]) for e in task1["unplaced_executions"]] == [("task1-Gate-r0-a1", 3)]
    assert task1["earlier_executions"] == []


def all_executions(task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """段の下・前の版・段の外の実行を、実行の id で引けるようにする。"""
    found = [e for step in task["flow"]["steps"] for e in step["executions"]]
    found += task["earlier_executions"] + task["unplaced_executions"]
    return {e["id"]: e for e in found}


@pytest.fixture
def clock() -> Clock:
    return Clock()


def ended_task(paths: RunPaths, clock: Clock) -> Seed:
    """Impl と Gate の 2 段のフローで、実行を 1 つずつ始められるタスク task1。"""
    seed = Seed(paths, clock)
    seed(RUN, started(), TaskStarted(T1, TaskKind.IMPLEMENTATION, spec=PLAN[0].spec))
    seed(
        StreamId.task(T1),
        TaskOpened(TaskKind.IMPLEMENTATION, PLAN[0].spec),
        FlowAccepted(Flow((FlowStep(StageKind.IMPL), FlowStep(StageKind.GATE)), 1)),
    )
    return seed


def attempt(number: int, stage: StageKind = StageKind.IMPL) -> ExecutionId:
    return ExecutionId(T1, stage, 0, number)


def begin(seed: Seed, execution: ExecutionId, step: int = 0) -> None:
    seed(StreamId.task(T1), StageRequested(execution, step), StageStarted(execution, HEAD))


def test_落ちた実行は理由と落ちた時刻を持ち走っている実行は終了時刻を持たない(
    paths: RunPaths, clock: Clock
):
    seed = ended_task(paths, clock)
    clock.now = "2026-10-04T01:00:00.000000Z"
    begin(seed, attempt(1))
    begin(seed, attempt(2))
    clock.now = "2026-10-04T01:02:03.000000Z"
    seed(StreamId.task(T1), StageFailed(attempt(1), "落ちた"))

    found = all_executions(run_status(paths)["tasks"][0])
    failed = found["task1-Impl-r0-a1"]
    assert (failed["status"], failed["end_reason"], failed["ended_at"]) == (
        "failed",
        "落ちた",
        "2026-10-04T01:02:03.000000Z",
    )
    assert failed["started_at"] == "2026-10-04T01:00:00.000000Z"
    running = found["task1-Impl-r0-a2"]
    assert (running["status"], running["ended_at"], running["end_reason"]) == (
        "running",
        None,
        None,
    )


def test_止めた理由を持つイベントで終えた実行はその理由を出す(paths: RunPaths, clock: Clock):
    seed = ended_task(paths, clock)
    task = StreamId.task(T1)
    begin(seed, attempt(1))
    seed(task, ExecutionRestarted(attempt(1), "再開できなかった", HEAD))
    begin(seed, attempt(2))
    seed(task, StageCompleted(attempt(2)), HandoffFailed(attempt(2), "受けなかった"))
    # 始める前にフローを捨てた
    seed(task, StageRequested(attempt(3), 0), StageCancelled(attempt(3), "フローを捨てた"))

    found = all_executions(run_status(paths)["tasks"][0])
    assert [(found[str(attempt(n))]["end_reason"]) for n in (1, 2, 3)] == [
        "再開できなかった",
        "受けなかった",
        "フローを捨てた",
    ]
    assert found[str(attempt(3))]["ended_at"] is not None


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("", "design-gap"), ("設計に無い関数が要る", "設計に無い関数が要る")],
)
def test_報告で終えた実行は理由が空なら報告の種類を理由にする(
    paths: RunPaths, clock: Clock, reason: str, expected: str
):
    seed = ended_task(paths, clock)
    begin(seed, attempt(1))
    seed(StreamId.task(T1), StageReported(attempt(1), EscalationKind.DESIGN_GAP, reason))

    reported = all_executions(run_status(paths)["tasks"][0])[str(attempt(1))]
    assert (reported["status"], reported["end_reason"]) == ("reported", expected)


def test_中断した実行は止めた理由を持ち続きから始めたら終わりの欄が空に戻る(
    paths: RunPaths, clock: Clock
):
    seed = ended_task(paths, clock)
    begin(seed, attempt(1))
    clock.now = "2026-10-04T02:00:00.000000Z"
    seed(StreamId.task(T1), StageInterrupted(attempt(1), InterruptCause.PANIC))

    stopped = all_executions(run_status(paths)["tasks"][0])[str(attempt(1))]
    assert (stopped["status"], stopped["interrupted_by"], stopped["end_reason"]) == (
        "interrupted",
        "panic",
        None,
    )
    assert stopped["ended_at"] == "2026-10-04T02:00:00.000000Z"

    clock.now = "2026-10-04T03:00:00.000000Z"
    seed(StreamId.task(T1), StageStarted(attempt(1), HEAD))
    resumed = all_executions(run_status(paths)["tasks"][0])[str(attempt(1))]
    assert (
        resumed["status"],
        resumed["ended_at"],
        resumed["interrupted_by"],
        resumed["end_reason"],
    ) == ("running", None, None, None)
    assert resumed["started_at"] == "2026-10-04T03:00:00.000000Z"


def test_完了した実行は理由を持たず完了した時刻を持つ(paths: RunPaths, clock: Clock):
    seed = ended_task(paths, clock)
    begin(seed, attempt(1))
    clock.now = "2026-10-04T04:05:06.000000Z"
    seed(StreamId.task(T1), StageCompleted(attempt(1), cursor=Cursor(1)))

    completed = all_executions(run_status(paths)["tasks"][0])[str(attempt(1))]
    assert (completed["status"], completed["end_reason"], completed["ended_at"]) == (
        "completed",
        None,
        "2026-10-04T04:05:06.000000Z",
    )


def test_落ちたGateの実行は落ちた項目と理由を持ち通ったGateの実行は持たない(
    paths: RunPaths, clock: Clock
):
    seed = ended_task(paths, clock)
    task = StreamId.task(T1)
    passed = attempt(1, StageKind.GATE)
    failed = attempt(2, StageKind.GATE)
    begin(seed, passed, 1)
    seed(task, StageCompleted(passed))
    begin(seed, failed, 1)
    verify = GateItemResult(GateItem.VERIFY, False, "pytest が 1 で終えた")
    seed(task, GateFailed(failed, (verify,), cursor=Cursor(0)))

    found = all_executions(run_status(paths)["tasks"][0])
    assert found[str(failed)]["gate_failures"] == [
        {"item": "verify", "reason": "pytest が 1 で終えた"}
    ]
    assert found[str(passed)]["gate_failures"] == []
    # 落ちた Gate の実行は、Gate の段の下に出る
    (task1,) = run_status(paths)["tasks"]
    assert step_ids(task1)[1] == [str(passed), str(failed)]


def test_タスクとランのエスカレーションは理由と本文を出す(paths: RunPaths):
    seed = Seed(paths)
    seed(RUN, started(), TaskStarted(T1, TaskKind.IMPLEMENTATION, spec=PLAN[0].spec))
    seed(
        StreamId.task(T1),
        TaskOpened(TaskKind.IMPLEMENTATION, PLAN[0].spec),
        FlowAccepted(Flow((FlowStep(StageKind.IMPL),), 1)),
        EscalationRaised(
            EscalationKind.ASK,
            Pointers(),
            task=T1,
            reason="判定が割れた",
            question="どちらにするか",
        ),
        EscalationRaised(EscalationKind.STALL, Pointers(), task=T1, reason="直らない"),
    )
    seed(
        RUN,
        EscalationRaised(
            EscalationKind.NEEDS_HUMAN,
            Pointers(),
            task=T1,
            source=EventId("task/task1#3"),
            reason="人が決める",
            question="続けるか",
        ),
        EscalationRaised(EscalationKind.NEEDS_REPLAN, Pointers(), task=T1, reason="計画し直す"),
    )

    status = run_status(paths)
    assert [(e["reason"], e["question"]) for e in status["tasks"][0]["escalations"]] == [
        ("判定が割れた", "どちらにするか"),
        ("直らない", None),
    ]
    assert [(e["reason"], e["question"]) for e in status["escalations"]] == [
        ("人が決める", "続けるか"),
        ("計画し直す", None),
    ]


def test_driverが止まっているかはフェーズと回答待ちと錠で決まる(paths: RunPaths):
    seed = Seed(paths)
    seed(RUN, started(), TaskStarted(PLANNING, TaskKind.PLANNING), TaskStarted(GIT, TaskKind.GIT))
    seed(RUN, TasksPlanned(DesignVersion(1), PLAN, (), ARTIFACTS, replan=False))
    run = run_status(paths)["run"]
    assert (run["phase"], run["awaiting_answer"], run["driver_stopped"]) == (
        "running",
        False,
        True,
    )

    seed(
        StreamId.questions(),
        QuestionPosted(QuestionId("q1"), "どちらの形にするか", EventId("run#7")),
    )
    run = run_status(paths)["run"]
    assert (run["awaiting_answer"], run["driver_stopped"]) == (True, False)


def test_走っているはずのフェーズでも錠を握るプロセスがあればdriverは止まっていない(
    paths: RunPaths, holder: subprocess.Popen[str]
):
    seed = Seed(paths)
    seed(RUN, started(), TaskStarted(PLANNING, TaskKind.PLANNING), TaskStarted(GIT, TaskKind.GIT))
    seed(RUN, TasksPlanned(DesignVersion(1), PLAN, (), ARTIFACTS, replan=False))
    run = run_status(paths)["run"]
    assert (run["phase"], run["driver_running"], run["driver_stopped"]) == (
        "running",
        True,
        False,
    )


def test_終えたランではdriverが走っていなくても止まったと見せない(paths: RunPaths):
    seed = Seed(paths)
    seed(RUN, started(), TaskStarted(GIT, TaskKind.GIT))
    seed(RUN, TasksPlanned(DesignVersion(1), PLAN, (), ARTIFACTS, replan=False))
    seed(
        RUN,
        TasksStopped(frozenset({T1, T2})),
        RunFinished(ready_overview=True),
        TaskStatusChanged(GIT, TaskStatus.RUNNING, TaskStatus.FINISHED, "FlowFinished"),
    )
    run = run_status(paths)["run"]
    assert (run["phase"], run["driver_running"], run["driver_stopped"]) == (
        "finished",
        False,
        False,
    )


def test_積んだ数と分母は止めた引き継いだ破棄した実装タスクを分母から外す(paths: RunPaths):
    numbered = [TaskId.numbered(n) for n in range(1, 7)]
    plan = tuple(PlannedTask(t, TaskSpec(f"件名 {t}")) for t in numbered)
    seed = Seed(paths)
    seed(RUN, started(), TaskStarted(PLANNING, TaskKind.PLANNING), TaskStarted(GIT, TaskKind.GIT))
    seed(RUN, TasksPlanned(DesignVersion(1), plan, (), ARTIFACTS, replan=False))
    to = (
        TaskStatus.STACKED,
        TaskStatus.STACKED,
        TaskStatus.RUNNING,
        TaskStatus.DROPPED,
        TaskStatus.SUPERSEDED,
        TaskStatus.DISCARDED,
    )
    seed(
        RUN,
        *(
            TaskStatusChanged(t, TaskStatus.PENDING, status, "x")
            for t, status in zip(numbered, to, strict=True)
        ),
        # 計画タスクと git 管理タスクは、終えても数えない
        TaskStatusChanged(PLANNING, TaskStatus.RUNNING, TaskStatus.FINISHED, "x"),
    )
    run = run_status(paths)["run"]
    assert (run["stacked_tasks"], run["stack_target_tasks"]) == (2, 3)


def test_タスクの台帳の指摘は台帳の順にタスクへ設計の台帳の指摘は計画へ出す(paths: RunPaths):
    seed = Seed(paths)
    seed_running(seed)
    judge = ex(T1, StageKind.JUDGE)
    seed(
        StreamId.review(T1),
        FindingRaised(FindingId("R1"), Rating.MUST_FIX, "例外を握りつぶす", Location("a.py:12")),
        FindingRaised(FindingId("R2"), Rating.NIT, "名前が長い"),
        FixCounted(ex(T1, StageKind.FIX), (FindingId("R1"), FindingId("R2"))),
        FindingClosed(FindingId("R2"), "短くした", judge),
    )
    seed(
        StreamId.design_review(),
        FindingRaised(FindingId("D1"), Rating.SHOULD_FIX, "境界が曖昧", design=DesignVersion(3)),
    )

    status = run_status(paths)
    by_id = {t["id"]: t for t in status["tasks"]}
    assert by_id["task1"]["findings"] == [
        {
            "id": "R1",
            "rating": "must-fix",
            "status": "open",
            "body": "例外を握りつぶす",
            "location": "a.py:12",
            "fixes": 1,
            "stalled": False,
            "comments": [],
            "design": None,
        },
        {
            "id": "R2",
            "rating": "nit",
            "status": "closed",
            "body": "名前が長い",
            "location": None,
            "fixes": 1,
            "stalled": False,
            "comments": ["短くした"],
            "design": None,
        },
    ]
    # 計画タスクの指摘は設計の台帳にあるが、タスクの欄には出さず plan に出す
    assert (by_id["planning"]["findings"], by_id["git"]["findings"]) == ([], [])
    assert by_id["task2"]["findings"] == []
    assert status["plan"]["findings"] == [
        {
            "id": "D1",
            "rating": "should-fix",
            "status": "open",
            "body": "境界が曖昧",
            "location": None,
            "fixes": 0,
            "stalled": False,
            "comments": [],
            "design": 3,
        }
    ]


def test_修正を重ねても開いたままの指摘は停滞したと出す(paths: RunPaths):
    seed = Seed(paths)
    seed(RUN, started(), TaskStarted(T1, TaskKind.IMPLEMENTATION, spec=PLAN[0].spec))
    r1 = FindingId("R1")
    seed(
        StreamId.review(T1),
        FindingRaised(r1, Rating.SHOULD_FIX, "直らない"),
        FixCounted(ex(T1, StageKind.FIX, 1), (r1,)),
        FixCounted(ex(T1, StageKind.FIX, 2), (r1,)),
    )
    (finding,) = run_status(paths)["tasks"][0]["findings"]
    assert (finding["fixes"], finding["stalled"]) == (2, True)


def test_specと判断の履歴を出どころ付きで出す(paths: RunPaths):
    spec = TaskSpec(
        "パーサを足す", dod="読める", acceptance=("空なら空", "壊れたら落ちる"), scope=("a.py",)
    )
    seed = Seed(paths)
    seed(
        RUN,
        started(),
        TaskStarted(PLANNING, TaskKind.PLANNING),
        TaskStarted(T1, TaskKind.IMPLEMENTATION, spec=spec),
    )
    seed(
        StreamId.task(T1),
        TaskOpened(TaskKind.IMPLEMENTATION, spec),
        NoteAdded(Decision("A にする", DecisionOrigin.USER, QuestionId("q1"))),
        NoteAdded(Decision("B で進める", DecisionOrigin.RUN_SUPERVISOR)),
    )
    seed(StreamId.task(PLANNING), TaskOpened(TaskKind.PLANNING))

    by_id = {t["id"]: t for t in run_status(paths)["tasks"]}
    assert by_id["task1"]["spec"] == {
        "dod": "読める",
        "acceptance": ["空なら空", "壊れたら落ちる"],
        "scope": ["a.py"],
    }
    assert by_id["task1"]["notes"] == [
        {"text": "A にする", "origin": "user", "question": "q1"},
        {"text": "B で進める", "origin": "run-supervisor", "question": None},
    ]
    assert (by_id["planning"]["spec"], by_id["planning"]["notes"]) == (None, [])


#: 錠を握ったら `held` を 1 行書き、標準入力が閉じるまで握り続ける
_HOLD_LOCK = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def holder(paths: RunPaths) -> Iterator[subprocess.Popen[str]]:
    """`driver.lock` を握っている、別のプロセス（同じプロセスの flock では driver の代わりにならない）。"""
    paths.root.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(paths.driver_lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None and process.stdout.readline() == "held\n"
    yield process
    assert process.stdin is not None
    process.stdin.close()
    process.wait()


def test_ほかのプロセスが錠を握っていればdriverが走っていると見せる(
    paths: RunPaths, holder: subprocess.Popen[str]
):
    Seed(paths)(RUN, started())
    assert run_status(paths)["run"]["driver_running"] is True
    assert holder.stdin is not None
    holder.stdin.close()
    holder.wait()
    assert run_status(paths)["run"]["driver_running"] is False


def test_錠を確かめても錠のファイルを作らず錠を残さない(paths: RunPaths):
    Seed(paths)(RUN, started())
    assert run_status(paths)["run"]["driver_running"] is False
    assert not paths.driver_lock.exists()
    with DriverLock(paths.driver_lock):
        pass
    assert run_status(paths)["run"]["driver_running"] is False
    with DriverLock(paths.driver_lock):
        pass


@pytest.fixture
def sleeper(paths: RunPaths) -> Iterator[int]:
    """前の driver が控えを置いたまま残した子。"""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    children.track_in(paths.children)
    try:
        children.record(child.pid, ["python", "-c", "sleep"])
    finally:
        children.track_in(None)
    yield child.pid
    child.kill()
    child.wait()


def test_driverが止まっていて前の子が生きていればそのpidを見せる(paths: RunPaths, sleeper: int):
    Seed(paths)(RUN, started())
    run = run_status(paths)["run"]
    assert (run["driver_running"], run["live_children"]) == (False, [sleeper])


def test_driverが走っていれば生きている子を前の子に数えない(
    paths: RunPaths, sleeper: int, holder: subprocess.Popen[str]
):
    Seed(paths)(RUN, started())
    run = run_status(paths)["run"]
    assert (run["driver_running"], run["live_children"]) == (True, [])


def test_控えを見ている間にdriverが起動したら子を前の子に数えない(
    paths: RunPaths, sleeper: int, monkeypatch: pytest.MonkeyPatch
):
    Seed(paths)(RUN, started())
    answers = iter([False, True])
    monkeypatch.setattr(status_module, "held_elsewhere", lambda _path: next(answers))
    run = run_status(paths)["run"]
    assert (run["driver_running"], run["live_children"]) == (True, [])


def test_statusは死んだ子の控えも読めない控えも消さない(paths: RunPaths):
    Seed(paths)(RUN, started())
    paths.children.mkdir(parents=True, exist_ok=True)
    # pid 1 は生きているが、開始時刻と boot_id が違うので別のプロセスである
    dead = paths.children / "1.json"
    dead.write_text(json.dumps({"pid": 1, "boot": "x", "start": "0"}), encoding="utf-8")
    broken = paths.children / "2.json"
    broken.write_text("{", encoding="utf-8")
    assert run_status(paths)["run"]["live_children"] == []
    assert dead.exists() and broken.exists()


def test_控えの置き場を読めなくてもstatusは落ちない(paths: RunPaths):
    Seed(paths)(RUN, started())
    paths.children.mkdir(parents=True)
    (paths.children / "1.json").write_text("{}", encoding="utf-8")
    paths.children.chmod(0)
    try:
        assert run_status(paths)["run"]["live_children"] == []
    finally:
        paths.children.chmod(0o755)
