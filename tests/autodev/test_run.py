"""Run 集約（`domain/aggregates/run.py`）。コマンドとイベントの列だけで、不変条件ごとに通る場合と拒む場合を見る。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from autodev_harness import CLI, DRIVER, POLICY, RUN_SUPERVISOR, Loop, names, new_id, of_type

from autodev.domain.aggregates.base import Rejected
from autodev.domain.aggregates.run import Run, RunPhase
from autodev.domain.commands.run import (
    AnswerEscalation,
    ApplyPlan,
    ApplyReplan,
    ClearIntegrationFailure,
    CloseRelayedEscalation,
    EscalateToRun,
    FinishRun,
    InsertTask,
    MarkStacked,
    Panic,
    RecordAnswer,
    RecordIntegrationFailure,
    RecordSettledPlan,
    ReportSupervisorFailure,
    RequestReplan,
    ResumeRun,
    ReturnToQueue,
    StartReadyTasks,
    StartRun,
    StartTask,
    StopTasks,
    UpdateTaskStatus,
)
from autodev.domain.events.run import (
    AllTasksSettled,
    AnswerRecorded,
    EscalationAnswered,
    EscalationRaised,
    IntegrationFailureCleared,
    IntegrationFailureRecorded,
    ReplanRequested,
    RunResumed,
    RunStarted,
    TaskInserted,
    TaskMarkedStacked,
    TasksDiscarded,
    TasksPlanned,
    TasksReturnedToQueue,
    TaskStarted,
    TaskStatusChanged,
    TaskSuperseded,
)
from autodev.domain.events.task import EscalationClosed
from autodev.domain.services.escalation_router import SupervisorLevel
from autodev.domain.supervision import wake_for
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.artifact_ref import ArtifactRef
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.design_version import DesignVersion
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.instruction import Instruction
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.limits import (
    MAX_REPLANS_WITHOUT_STACK,
    MAX_SUPERVISOR_FAILURES,
)
from autodev.domain.value_objects.parallel_limit import ParallelLimit
from autodev.domain.value_objects.planned_task import PlannedTask
from autodev.domain.value_objects.pointers import Pointers
from autodev.domain.value_objects.pr_number import PrNumber
from autodev.domain.value_objects.proposal import Proposal
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.repository import Repository
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_kind import TaskKind
from autodev.domain.value_objects.task_spec import TaskSpec
from autodev.domain.value_objects.task_status import TaskStatus
from autodev.domain.value_objects.verify_command import VerifyCommand

S = TaskStatus
E = EscalationKind
NAME = RunName("add-cache")
PLANNING = TaskId.planning()
GIT = TaskId.git()
ARTIFACTS = (ArtifactRef(ArtifactKind.BRIEF, "brief.md"), ArtifactRef(ArtifactKind.DESIGN, "1"))


def t(number: int) -> TaskId:
    return TaskId.numbered(number)


def spec(title: str) -> TaskSpec:
    return TaskSpec(title)


def planned(number: int, *deps: int, title: str | None = None) -> PlannedTask:
    return PlannedTask(t(number), spec(title or f"t{number}"), frozenset(t(d) for d in deps))


def proposal(*tasks: PlannedTask, version: int = 1, **kw) -> Proposal:
    return Proposal(DesignVersion(version), tasks, **kw)


class RunLoop(Loop[Run]):
    def start(self, limit: int = 3) -> None:
        self(
            StartRun(
                command_id=new_id(),
                issuer=CLI,
                name=NAME,
                instruction=Instruction("キャッシュを足す"),
                repository=Repository("/repo"),
                base=BranchName("main"),
                limit=ParallelLimit(limit),
            )
        )
        self(StartTask(command_id=new_id(), issuer=POLICY, task=PLANNING))
        self(StartTask(command_id=new_id(), issuer=POLICY, task=GIT))

    def settle(self, plan: Proposal) -> list:
        return self(
            RecordSettledPlan(
                command_id=new_id(), issuer=POLICY, proposal=plan, artifacts=ARTIFACTS
            )
        )

    def plan(self, *tasks: PlannedTask) -> list:
        self.settle(proposal(*tasks))
        return self(ApplyPlan(command_id=new_id(), issuer=POLICY, design=DesignVersion(1)))

    def start_ready(self) -> list:
        return self(StartReadyTasks(command_id=new_id(), issuer=POLICY))

    def status(self, task: TaskId, to: TaskStatus, cause: str = "test") -> list:
        return self(
            UpdateTaskStatus(
                command_id=new_id(), issuer=POLICY, task=task, to_status=to, cause=cause
            )
        )

    def fail_integration(self, task: TaskId) -> list:
        """走っている実装タスクを、積んでいる途中で統合に失敗したところまで進める。"""
        self.status(task, S.GATED)
        self.status(task, S.STACKING)
        return self(
            RecordIntegrationFailure(
                command_id=new_id(), issuer=POLICY, task=task, reason="両方は残せない"
            )
        )

    def stack(self, task: TaskId) -> list:
        """走っている実装タスクを、フローを終えて積んだところまで進める。"""
        self.status(task, S.GATED)
        self.status(task, S.STACKING)
        return self(MarkStacked(command_id=new_id(), issuer=POLICY, task=task, pr=PrNumber(10)))

    def escalate(self, task: TaskId, kind: EscalationKind, source: str | None = None) -> EventId:
        issuer = Issuer.task_supervisor(task, RUN_SUPERVISOR.session if task.number else None)
        self(
            EscalateToRun(
                command_id=new_id(),
                issuer=issuer,
                task=task,
                kind=kind,
                reason="自分では解けない",
                pointers=Pointers(),
                source=EventId(source) if source else None,
            )
        )
        return self.aggregate.event_id

    def discard(self, *numbers: int, version: int = 2) -> list:
        """積んだタスクを破棄する再計画を、確定から反映まで進める。"""
        tasks = frozenset(t(n) for n in numbers)
        self.replan()
        self.settle(proposal(version=version, discard=tasks))
        return self.apply_replan(version=version, discard=tasks)

    def return_to_queue(self) -> list:
        return self(ReturnToQueue(command_id=new_id(), issuer=POLICY))

    def replan(self, trigger: EventId | None = None, answer: QuestionId | None = None) -> list:
        return self(
            RequestReplan(
                command_id=new_id(),
                issuer=RUN_SUPERVISOR,
                reason="割り方を変える",
                trigger=trigger,
                answer=answer,
            )
        )

    def apply_replan(self, version: int = 2, **kw) -> list:
        return self(
            ApplyReplan(
                command_id=new_id(), issuer=RUN_SUPERVISOR, design=DesignVersion(version), **kw
            )
        )

    def record_answer(self, question: str, escalation: EventId | None) -> None:
        self(
            RecordAnswer(
                command_id=new_id(),
                issuer=POLICY,
                question=QuestionId(question),
                answer="はい",
                escalation=escalation,
            )
        )

    def statuses(self) -> dict[str, TaskStatus]:
        return {str(task): entry.status for task, entry in self.aggregate.tasks.items()}


@pytest.fixture
def run() -> RunLoop:
    loop = RunLoop(Run(StreamId.run()))
    loop.start()
    return loop


@pytest.fixture
def planned_run(run: RunLoop) -> RunLoop:
    """task1 と、task1 を待つ task2 を計画し、task1 を始めたラン。"""
    run.plan(planned(1), planned(2, 1))
    run.start_ready()
    return run


# --- 始める ---


def test_ランは1回だけ始まる(run: RunLoop):
    with pytest.raises(Rejected, match="もう始まっている"):
        run.start()


def test_計画タスクとgit管理タスクはランに1つずつ(run: RunLoop):
    assert run.statuses() == {"planning": S.RUNNING, "git": S.RUNNING}
    for task in (PLANNING, GIT):
        with pytest.raises(Rejected, match="もう始まっている"):
            run(StartTask(command_id=new_id(), issuer=POLICY, task=task))


def test_実装タスクは依存先が積まれてから始まりブランチと成果物を持つ(planned_run: RunLoop):
    run = planned_run
    assert run.statuses()["task1"] is S.RUNNING
    assert run.statuses()["task2"] is S.PENDING
    started = of_type(run.events, TaskStarted)
    assert started[-1] == TaskStarted(
        t(1),
        started[-1].kind,
        spec=spec("t1"),
        artifacts=ARTIFACTS,
        branch=BranchName("stack/add-cache--task-1"),
    )
    with pytest.raises(Rejected, match="task2 が待つ task1 がまだ積まれていない"):
        run(StartTask(command_id=new_id(), issuer=POLICY, task=t(2)))
    run.stack(t(1))
    assert names(run.start_ready()) == ["TaskStarted"]
    assert run.statuses()["task2"] is S.RUNNING


def test_走っている実装タスクは上限まで():
    run = RunLoop(Run(StreamId.run()))
    run.start(limit=2)
    run.plan(planned(1), planned(2), planned(3))
    assert len(run.start_ready()) == 2
    with pytest.raises(Rejected, match="上限（2）"):
        run(StartTask(command_id=new_id(), issuer=POLICY, task=t(3)))
    # 止まっている（escalated）タスクも上限に数える
    run.status(t(1), S.ESCALATED)
    assert run.start_ready() == []
    run.stack(t(2))
    assert names(run.start_ready()) == ["TaskStarted"]


def test_パニックの後は再開するまでタスクを始めない(run: RunLoop):
    run.plan(planned(1))
    run(Panic(command_id=new_id(), issuer=DRIVER, cause="429"))
    # パニックの間に積み終えたタスクの知らせなどで頼まれても、拒まずに何もしない
    assert run.start_ready() == []
    with pytest.raises(Rejected, match="パニックの後"):
        run(StartTask(command_id=new_id(), issuer=POLICY, task=t(1)))
    with pytest.raises(Rejected, match="もうパニックしている"):
        run(Panic(command_id=new_id(), issuer=DRIVER, cause="429"))
    # 呼び直されたら、終端でないタスク（計画タスク・git 管理タスクも）に再開を頼めるように載せる
    assert run(ResumeRun(command_id=new_id(), issuer=CLI)) == [
        RunResumed((PLANNING, GIT, t(1)), after_panic=True)
    ]
    assert names(run.start_ready()) == ["TaskStarted"]


def test_落ちた後に呼び直されても止まった実行の再開をタスクに頼む(planned_run: RunLoop):
    run = planned_run
    run.stack(t(1))
    assert run(ResumeRun(command_id=new_id(), issuer=CLI)) == [
        RunResumed((PLANNING, GIT, t(2)), after_panic=False)
    ]


# --- 計画の反映 ---


def test_計画を反映すると新しいタスクがまだ始めていない状態で入る(run: RunLoop):
    events = run.plan(planned(1), planned(2, 1))
    assert names(events) == ["TasksPlanned"]
    plan = events[0]
    assert isinstance(plan, TasksPlanned)
    assert not plan.replan
    assert plan.artifacts == ARTIFACTS
    assert run.statuses() == {
        "planning": S.RUNNING,
        "git": S.RUNNING,
        "task1": S.PENDING,
        "task2": S.PENDING,
    }
    assert not run.aggregate.planning


def test_確定した提案が無ければ反映しない(run: RunLoop):
    with pytest.raises(Rejected, match="確定して、まだ反映していない提案が無い"):
        run(ApplyPlan(command_id=new_id(), issuer=POLICY, design=DesignVersion(1)))
    run.settle(proposal(planned(1)))
    with pytest.raises(Rejected, match="版は 1 で、2 ではない"):
        run(ApplyPlan(command_id=new_id(), issuer=POLICY, design=DesignVersion(2)))


@pytest.mark.parametrize(
    ("tasks", "reason"),
    [
        ((planned(1, 2), planned(2, 1)), "輪になっている: task1 → task2 → task1"),
        ((planned(1, 1),), "task1 が自分を待っている"),
        ((planned(1, 5),), "task1 が待つ task5 は計画に無い"),
        ((PlannedTask(GIT, spec("x")),), "計画に書けるのは実装タスクだけ"),
    ],
)
def test_壊れた依存のグラフを拒む(run: RunLoop, tasks, reason):
    run.settle(proposal(*tasks))
    with pytest.raises(Rejected, match=reason):
        run(ApplyPlan(command_id=new_id(), issuer=POLICY, design=DesignVersion(1)))


def test_初めての反映の後はApplyPlanを受けずApplyReplanで反映する(planned_run: RunLoop):
    run = planned_run
    run.replan()
    run.settle(proposal(planned(1), version=2))
    with pytest.raises(Rejected, match="ApplyReplan"):
        run(ApplyPlan(command_id=new_id(), issuer=POLICY, design=DesignVersion(2)))
    fresh = RunLoop(Run(StreamId.run()))
    fresh.start()
    fresh.settle(proposal(planned(1)))
    with pytest.raises(Rejected, match="初めての反映は ApplyPlan"):
        fresh.apply_replan(version=1)


def test_計画が進んでいないのに確定した提案は受けない(planned_run: RunLoop):
    with pytest.raises(Rejected, match="計画が進んでいない"):
        planned_run.settle(proposal(planned(1)))


# --- 再計画 ---


def test_再計画はタスクを積まないまま続けた数が上限に達したら回答を求める(planned_run: RunLoop):
    run = planned_run
    for _ in range(MAX_REPLANS_WITHOUT_STACK):
        run.replan()
        run.settle(proposal(planned(1), planned(2, 1), version=2))
        run.apply_replan()
    assert run.aggregate.replan_streak == MAX_REPLANS_WITHOUT_STACK
    with pytest.raises(Rejected, match="ユーザーに聞いてから"):
        run.replan()
    # 回答の届いていない質問・使い終えた回答は使えない
    with pytest.raises(Rejected, match="まだ届いていない"):
        run.replan(answer=QuestionId("q-more"))
    run.record_answer("q-more", None)
    events = run.replan(answer=QuestionId("q-more"))
    assert names(events) == ["ReplanRequested"]
    assert run.aggregate.replan_streak == 0
    run.settle(proposal(planned(1), planned(2, 1), version=3))
    run.apply_replan(version=3)
    run.replan()
    run.settle(proposal(planned(1), planned(2, 1), version=4))
    run.apply_replan(version=4)
    run.replan()
    run.settle(proposal(planned(1), planned(2, 1), version=5))
    run.apply_replan(version=5)
    with pytest.raises(Rejected, match="もう使った"):
        run.replan(answer=QuestionId("q-more"))


def test_積むと再計画の数は0に戻る(planned_run: RunLoop):
    run = planned_run
    run.replan()
    run.settle(proposal(planned(1), planned(2, 1), version=2))
    run.apply_replan()
    assert run.aggregate.replan_streak == 1
    run.stack(t(1))
    assert run.aggregate.replan_streak == 0


def test_計画が進んでいる間は計画タスクのエスカレーションに応じる再計画だけを受ける(run: RunLoop):
    # 初回の計画の途中（計画ステージの ask）
    with pytest.raises(Rejected, match="計画タスクのエスカレーションに応じる再計画"):
        run.replan()
    ask = run.escalate(PLANNING, E.ASK, source="task/planning#4")
    events = run.replan(trigger=ask)
    # 計画タスクの側を先に閉じる。ReplanRequested を受けた統括が組むフローを、計画タスクが受けられる
    assert names(events) == ["EscalationClosed", "ReplanRequested"]
    requested = events[1]
    assert isinstance(requested, ReplanRequested)
    # まだ一度も設計が確定していないので、Prepare → Plan からやり直す
    assert requested.settled_before is False
    # 計画タスクの側の待っているエスカレーションも閉じられるように、task と source を載せる
    assert events[0] == EscalationClosed(
        ask, "再計画を頼んだ", PLANNING, EventId("task/planning#4")
    )
    assert run.aggregate.escalations == {}


def test_確定した提案が反映を待っている間はそれを退けて再計画を頼める(run: RunLoop):
    run.settle(proposal(planned(1)))
    events = run.replan()
    assert names(events) == ["ReplanRequested"]
    assert run.aggregate.settled is None
    with pytest.raises(Rejected, match="確定して、まだ反映していない提案が無い"):
        run(ApplyPlan(command_id=new_id(), issuer=POLICY, design=DesignVersion(1)))


def test_実装タスクのneeds_replanは反映したときに閉じ範囲の変更を知らせる(planned_run: RunLoop):
    run = planned_run
    run.status(t(1), S.ESCALATED)
    trigger = run.escalate(t(1), E.NEEDS_REPLAN, source="task/task1#9")
    other = run.escalate(GIT, E.INTEGRATION_FAILED)
    run.replan(trigger=trigger)
    with pytest.raises(Rejected, match="計画が進んでいる間"):
        run.replan(trigger=other)
    run.settle(proposal(planned(1), planned(2, 1, title="新しい t2"), version=2))
    events = run.apply_replan()
    assert names(events) == ["TasksPlanned", "EscalationClosed"]
    plan = events[0]
    assert isinstance(plan, TasksPlanned)
    assert plan.replan
    assert plan.triggers == (trigger,)
    # きっかけのエスカレーションを閉じるとき、タスクの側も閉じられるように task と source を載せる
    assert events[1] == EscalationClosed(trigger, "再計画を反映した", t(1), EventId("task/task1#9"))
    # 範囲が変わらなくても、きっかけのタスクには知らせる。task2 は始めていないので知らせない
    assert plan.scope_changed == {t(1)}
    assert plan.still_open == (other,)
    assert set(run.aggregate.escalations) == {other}


def test_計画タスクのエスカレーションへの再計画では反映したときに閉じるきっかけを変えない(
    planned_run: RunLoop,
):
    run = planned_run
    run.status(t(1), S.ESCALATED)
    trigger = run.escalate(t(1), E.NEEDS_REPLAN, source="task/task1#9")
    run.replan(trigger=trigger)
    # 再計画の途中で計画ステージが ask で止まり、ラン統括が replan で応じた
    ask = run.escalate(PLANNING, E.ASK, source="task/planning#4")
    events = run.replan(trigger=ask)
    (requested,) = of_type(events, ReplanRequested)
    assert requested.closes_on_apply == (trigger,)
    assert run.aggregate.replan_triggers == (trigger,)
    run.settle(proposal(planned(1), planned(2, 1), version=2))
    plan = run.apply_replan()[0]
    assert isinstance(plan, TasksPlanned)
    assert plan.triggers == (trigger,)


def test_確定した提案を別のエスカレーションで退けたら前のきっかけも反映のときに閉じる(
    planned_run: RunLoop,
):
    run = planned_run
    first = run.escalate(t(1), E.NEEDS_REPLAN, source="task/task1#9")
    run.replan(trigger=first)
    run.settle(proposal(planned(1), planned(2, 1), version=2))
    # 確定した提案が反映を待っている間に、別のタスクの needs-replan に応じて頼み直した
    second = run.escalate(GIT, E.INTEGRATION_FAILED)
    requested = run.replan(trigger=second)[0]
    assert isinstance(requested, ReplanRequested)
    assert requested.closes_on_apply == (first, second)
    run.settle(proposal(planned(1), planned(2, 1), version=3))
    events = run.apply_replan(version=3)
    plan = events[0]
    assert isinstance(plan, TasksPlanned)
    assert plan.triggers == (first, second)
    assert {e.escalation for e in of_type(events, EscalationClosed)} == {first, second}
    assert run.aggregate.escalations == {}


def test_反映した後も開いているエスカレーションには応えたものを数えない(planned_run: RunLoop):
    run = planned_run
    answered = run.escalate(GIT, E.INTEGRATION_FAILED)
    left = run.escalate(t(1), E.NEEDS_HUMAN)
    run.replan()
    run.settle(proposal(planned(1), planned(2, 1), version=2))
    plan = run.apply_replan(responds_to=answered)[0]
    assert isinstance(plan, TasksPlanned)
    assert plan.still_open == (left,)


def test_再計画を頼んだら計画の間に差し込んだタスクの番号はぶつからない(planned_run: RunLoop):
    run = planned_run
    run.replan()
    run(InsertTask(command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("差し込んだ")))
    run.settle(proposal(planned(1), planned(2, 1), version=2))
    # 確定した提案を退けて頼み直した。今度の提案は差し込んだ task3 を見て書く
    run.replan()
    assert run.aggregate.inserted_while_planning == set()
    run.settle(proposal(planned(1), planned(2, 1), planned(3, title="差し込んだ"), version=3))
    assert names(run.apply_replan(version=3)) == ["TasksPlanned"]


def test_再計画の反映は止める破棄する反映するを1回で行う(planned_run: RunLoop):
    run = planned_run
    run.stack(t(1))
    run.start_ready()  # task2
    run.replan()
    plan = proposal(planned(3), version=2, stop=frozenset({t(2)}), discard=frozenset({t(1)}))
    run.settle(plan)
    events = run.apply_replan(stop=frozenset({t(2)}), discard=frozenset({t(1)}))
    assert names(events) == ["TasksStopped", "TasksDiscarded", "TasksPlanned"]
    assert run.statuses()["task1"] is S.DISCARDED
    assert run.statuses()["task2"] is S.DROPPED
    assert run.statuses()["task3"] is S.PENDING


def test_再計画の反映は空の一覧のイベントを出さない(planned_run: RunLoop):
    run = planned_run
    run.replan()
    run.settle(proposal(planned(1), planned(2, 1), version=2))
    assert names(run.apply_replan()) == ["TasksPlanned"]


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"stop": frozenset({TaskId("task1")})}, "止めると提案していない"),
        ({"discard": frozenset({TaskId("task1")})}, "破棄を提案していない"),
    ],
)
def test_再計画の反映は提案に無い止める破棄するを拒み何も出さない(planned_run: RunLoop, kw, reason):
    run = planned_run
    run.replan()
    run.settle(proposal(planned(1), planned(2, 1), version=2))
    before = list(run.history)
    with pytest.raises(Rejected, match=reason):
        run.apply_replan(**kw)
    assert run.history == before


def test_再計画は止めたタスクに依存するグラフを拒む(planned_run: RunLoop):
    run = planned_run
    run.replan()
    # task1 を止めるのに、task2 が task1 を待ったまま
    run.settle(proposal(planned(2, 1), version=2, stop=frozenset({t(1)})))
    with pytest.raises(Rejected, match="dropped の task1 を待っている"):
        run.apply_replan(stop=frozenset({t(1)}))


def test_再計画は積み済みのタスクの中身を書き換えない(planned_run: RunLoop):
    run = planned_run
    run.stack(t(1))
    run.replan()
    run.settle(proposal(planned(1, title="書き換えた"), version=2))
    with pytest.raises(Rejected, match="stacked で、中身を書き換えない"):
        run.apply_replan()


def test_再計画は止めたタスクを書き換えない(planned_run: RunLoop):
    run = planned_run
    run(StopTasks(command_id=new_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({t(1), t(2)})))
    run.replan()
    # 止めたタスクの番号で書くと、新しいタスクではなく止めたタスクの書き換えになる
    run.settle(proposal(planned(1), version=2))
    with pytest.raises(Rejected, match="dropped で、書き換えない"):
        run.apply_replan()
    run.settle(proposal(planned(3), version=3))
    assert names(run.apply_replan(version=3)) == ["TasksPlanned"]


def test_引き継がれたタスクへの依存は引き継ぎ先に付け替える(planned_run: RunLoop):
    run = planned_run
    run.fail_integration(t(1))
    events = run(
        InsertTask(
            command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("やり直し"), takes_over=t(1)
        )
    )
    assert names(events) == ["TaskInserted", "TaskSuperseded"]
    assert of_type(events, TaskSuperseded) == [TaskSuperseded(t(1), t(3))]
    assert run.aggregate.tasks[t(2)].blocked_by == {t(3)}
    assert run.statuses()["task1"] is S.SUPERSEDED
    # 計画の依存も付け替える
    run.replan()
    run.settle(proposal(planned(2, 1), version=2))
    plan = run.apply_replan()[0]
    assert isinstance(plan, TasksPlanned)
    assert plan.tasks[0].blocked_by == {t(3)}


# --- 差し込み・止める・破棄・積み直す ---


def test_差し込むタスクは使っていない番号を使う(planned_run: RunLoop):
    run = planned_run
    events = run(InsertTask(command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("x")))
    assert [e.task for e in of_type(events, TaskInserted)] == [t(3)]
    run(StopTasks(command_id=new_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({t(3)})))
    # 止めたタスクの番号は使い回さない（捨てたタスクのブランチが残る）
    events = run(InsertTask(command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("y")))
    assert [e.task for e in of_type(events, TaskInserted)] == [t(4)]


def test_引き継げるのは積んでいる途中で統合に失敗したタスクだけ(planned_run: RunLoop):
    run = planned_run
    with pytest.raises(Rejected, match="積んでいる途中で統合に失敗したタスクだけ"):
        run(InsertTask(command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("x"), takes_over=t(1)))
    # 積んでいる途中でも、統合に失敗していなければ引き継がない（積み終えると引き継ぎ元も残る）
    run.status(t(1), S.GATED)
    run.status(t(1), S.STACKING)
    with pytest.raises(Rejected, match="統合に失敗していない"):
        run(InsertTask(command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("x"), takes_over=t(1)))
    run(MarkStacked(command_id=new_id(), issuer=POLICY, task=t(1), pr=PrNumber(10)))
    with pytest.raises(Rejected, match="もう積まれていて"):
        run(InsertTask(command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("x"), takes_over=t(1)))


def test_差し込みで循環や止めたタスクへの依存を作らない(planned_run: RunLoop):
    run = planned_run
    run.fail_integration(t(1))
    # task2 は task1 を待っているので、task1 を引き継ぐタスクが task2 を待つと輪になる
    with pytest.raises(Rejected, match="輪になっている"):
        run(
            InsertTask(
                command_id=new_id(),
                issuer=RUN_SUPERVISOR,
                spec=spec("x"),
                blocked_by=frozenset({t(2)}),
                takes_over=t(1),
            )
        )
    with pytest.raises(Rejected, match="引き継ぎ元の task1 を待てない"):
        run(
            InsertTask(
                command_id=new_id(),
                issuer=RUN_SUPERVISOR,
                spec=spec("x"),
                blocked_by=frozenset({t(1)}),
                takes_over=t(1),
            )
        )


def test_止めるのは積んでいないタスクで待つタスクを残さない(planned_run: RunLoop):
    run = planned_run
    with pytest.raises(Rejected, match="task2 が止める task1 を待っている"):
        run(StopTasks(command_id=new_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({t(1)})))
    run.stack(t(1))
    with pytest.raises(Rejected, match="積み済みで、止められない"):
        run(StopTasks(command_id=new_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({t(1)})))
    with pytest.raises(Rejected, match="実装タスクだけ"):
        run(StopTasks(command_id=new_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({GIT})))


def test_計画が進んでいる間に止めても止めたタスクを待つタスクを残さない(planned_run: RunLoop):
    run = planned_run
    run.replan()
    with pytest.raises(Rejected, match="task2 が止める task1 を待っている"):
        run(StopTasks(command_id=new_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({t(1)})))


def test_止めるとそのタスクのRunのエスカレーションと応えたエスカレーションを閉じる(
    planned_run: RunLoop,
):
    run = planned_run
    from_task = run.escalate(t(1), E.NEEDS_HUMAN)
    from_git = run.escalate(GIT, E.INTEGRATION_FAILED)
    events = run(
        StopTasks(
            command_id=new_id(),
            issuer=RUN_SUPERVISOR,
            tasks=frozenset({t(1), t(2)}),
            responds_to=from_git,
        )
    )
    assert names(events) == [
        "TasksStopped",
        "EscalationClosed",
        "EscalationClosed",
        "AllTasksSettled",
    ]
    closed = {e.escalation: e for e in of_type(events, EscalationClosed)}
    assert set(closed) == {from_task, from_git}
    # タスクの側も閉じられるように、上げてきたタスクを載せる
    assert closed[from_task].task == t(1)
    assert closed[from_git].task == GIT
    assert run.statuses()["task1"] is S.DROPPED


def test_最後の実装タスクがどの道で終端になってもAllTasksSettledを出す(planned_run: RunLoop):
    run = planned_run
    assert of_type(run.stack(t(1)), AllTasksSettled) == []
    run.start_ready()
    assert of_type(run.stack(t(2)), AllTasksSettled) == [AllTasksSettled()]


def test_再計画が進んでいる間はAllTasksSettledを出さない(planned_run: RunLoop):
    run = planned_run
    run.replan()
    events = run(
        StopTasks(command_id=new_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({t(1), t(2)}))
    )
    assert of_type(events, AllTasksSettled) == []
    run.settle(proposal(version=2))
    assert of_type(run.apply_replan(), AllTasksSettled) == [AllTasksSettled()]


def test_破棄は確定した再計画が提案した積んだタスクだけ(planned_run: RunLoop):
    run = planned_run
    run.stack(t(1))
    run.replan()
    run.settle(proposal(planned(2), version=2, discard=frozenset({t(1), t(2)})))
    with pytest.raises(Rejected, match="積んだタスクだけ"):
        run.apply_replan(discard=frozenset({t(2)}))
    discarded = of_type(run.apply_replan(discard=frozenset({t(1)})), TasksDiscarded)
    assert discarded == [TasksDiscarded(frozenset({t(1)}))]


def three_stacked() -> RunLoop:
    run = RunLoop(Run(StreamId.run()))
    run.start()
    run.plan(planned(1), planned(2), planned(3))
    run.start_ready()
    for number in (1, 2, 3):
        run.stack(t(number))
    return run


def test_破棄した所より上の積んだタスクを閉じ終えたら下から全部積む列に戻し新しいブランチで積み直す():
    run = three_stacked()
    discarded = of_type(run.discard(2), TasksDiscarded)
    assert discarded == [TasksDiscarded(frozenset({t(2)}), requeue=(t(3),))]
    # 破棄したタスクは、今積んである順から外す
    assert run.aggregate.stack_order == [t(1), t(3)]
    # 積む列へ戻す一覧の出どころは Run だけ。閉じ終えた（StackCutBack）知らせは一覧を持たない
    events = run.return_to_queue()
    assert events == [TasksReturnedToQueue((t(3),), (BranchName("stack/add-cache--task-3-r1"),))]
    assert run.statuses()["task3"] is S.GATED
    assert run.aggregate.stack_order == [t(1)]
    with pytest.raises(Rejected, match="閉じるのを待っているものが無い"):
        run.return_to_queue()


def test_積む列へ戻すのを待つタスクがあるうちは終端とみなさずランを終えない():
    run = three_stacked()
    events = run.discard(1, 2, 3)
    # 破棄で全部が終端になっても、閉じ終えるまでは仕上げに起こさない
    assert of_type(events, AllTasksSettled) == []
    with pytest.raises(Rejected, match="積む列へ戻し終えていない"):
        run(FinishRun(command_id=new_id(), issuer=RUN_SUPERVISOR, ready_overview=True))
    assert of_type(run.return_to_queue(), AllTasksSettled) == [AllTasksSettled()]
    assert names(
        run(FinishRun(command_id=new_id(), issuer=RUN_SUPERVISOR, ready_overview=True))
    ) == ["RunFinished"]


def test_積む列へ戻すのを待つタスクは積んだものに数えず待つタスクを始めない():
    run = RunLoop(Run(StreamId.run()))
    run.start()
    run.plan(planned(1), planned(2))
    run.start_ready()
    run.stack(t(1))
    run.stack(t(2))
    run.replan()
    run.settle(proposal(planned(3, 2), version=2, discard=frozenset({t(1)})))
    run.apply_replan(discard=frozenset({t(1)}))
    # task2 は閉じる所より上で、積み直すまでスタックに無い。task2 を待つ task3 はまだ始めない
    assert run.start_ready() == []
    run.return_to_queue()
    run.status(t(2), S.STACKING)
    run(MarkStacked(command_id=new_id(), issuer=POLICY, task=t(2), pr=PrNumber(11)))
    assert [e.task for e in of_type(run.start_ready(), TaskStarted)] == [t(3)]


def test_前の破棄で戻すのを待つタスクを次の再計画で破棄したら戻す一覧から除く():
    run = three_stacked()
    run.discard(1)
    assert run.aggregate.requeue == (t(2), t(3))
    discarded = of_type(run.discard(3, version=3), TasksDiscarded)
    assert discarded == [TasksDiscarded(frozenset({t(3)}), requeue=(t(2),))]
    # 2 つ目の破棄も閉じ終えるのを待つ
    assert run.aggregate.cuts_pending == 2
    assert run.return_to_queue()[0] == TasksReturnedToQueue(
        (t(2),), (BranchName("stack/add-cache--task-2-r1"),)
    )
    assert run.return_to_queue() == [TasksReturnedToQueue((), ())]
    assert run.statuses()["task3"] is S.DISCARDED


def test_破棄の後閉じる前に積み終えたタスクも閉じる所より上なので積み直す():
    run = RunLoop(Run(StreamId.run()))
    run.start()
    run.plan(planned(1), planned(2))
    run.start_ready()
    run.stack(t(1))
    run.status(t(2), S.GATED)
    run.status(t(2), S.STACKING)  # 積んでいる途中に、task1 を破棄した
    run.discard(1)
    assert run.aggregate.requeue == ()
    run(MarkStacked(command_id=new_id(), issuer=POLICY, task=t(2), pr=PrNumber(11)))
    assert run.aggregate.requeue == (t(2),)
    assert run.return_to_queue()[0].tasks == (t(2),)


def test_計画の軽い検査と回帰テストはTasksPlannedとRunに同じ値で載る(run: RunLoop):
    ruff, pytest_ = VerifyCommand("ruff"), VerifyCommand("pytest")
    run.settle(proposal(planned(1), quick_checks=(ruff,), regression_tests=(pytest_,)))
    events = run(ApplyPlan(command_id=new_id(), issuer=POLICY, design=DesignVersion(1)))
    plan = events[0]
    assert isinstance(plan, TasksPlanned)
    assert (plan.quick_checks, plan.regression_tests) == ((ruff,), (pytest_,))
    assert (run.aggregate.quick_checks, run.aggregate.regression_tests) == ((ruff,), (pytest_,))


def test_再計画で値を変えたら最後のTasksPlannedの値がランの値になる(run: RunLoop):
    ruff, ty, pytest_ = VerifyCommand("ruff"), VerifyCommand("ty"), VerifyCommand("pytest")
    run.settle(proposal(planned(1), quick_checks=(ruff,), regression_tests=(pytest_,)))
    run(ApplyPlan(command_id=new_id(), issuer=POLICY, design=DesignVersion(1)))
    run.replan()
    run.settle(proposal(planned(1), version=2, quick_checks=(ruff, ty), regression_tests=()))
    plan = run.apply_replan()[0]
    assert isinstance(plan, TasksPlanned)
    assert (plan.quick_checks, plan.regression_tests) == ((ruff, ty), ())
    assert (run.aggregate.quick_checks, run.aggregate.regression_tests) == ((ruff, ty), ())


def test_実装タスクが0件の計画も反映したら終端になったとみなす(run: RunLoop):
    assert run.plan() == [
        TasksPlanned(
            design=DesignVersion(1),
            tasks=(),
            quick_checks=(),
            regression_tests=(),
            artifacts=ARTIFACTS,
            replan=False,
        ),
        AllTasksSettled(),
    ]


def test_積んだ印は積んでいる途中のタスクにだけ付ける(planned_run: RunLoop):
    with pytest.raises(Rejected, match="積んでいる途中ではない"):
        planned_run(MarkStacked(command_id=new_id(), issuer=POLICY, task=t(1), pr=PrNumber(3)))


# --- 状態の知らせ ---


def test_ほかの集約のイベントによる遷移を受ける(planned_run: RunLoop):
    run = planned_run
    for to in (S.ESCALATED, S.RUNNING, S.GATED):
        assert names(run.status(t(1), to)) == ["TaskStatusChanged"]
    # gated から running へは知らせで戻さない（並列の上限を確かめる TaskStarted を通す）
    with pytest.raises(Rejected, match="gated から running へは動かせない"):
        run.status(t(1), S.RUNNING)
    assert names(run.status(t(1), S.STACKING)) == ["TaskStatusChanged"]
    with pytest.raises(Rejected, match="stacking から running へは動かせない"):
        run.status(t(1), S.RUNNING)
    with pytest.raises(Rejected, match="pending から stacked へは動かせない"):
        run.status(t(2), S.STACKED)


def test_同じ状態と終端のタスクへの知らせは何もしない(planned_run: RunLoop):
    run = planned_run
    assert run.status(t(1), S.RUNNING) == []
    run.stack(t(1))
    assert run.status(t(1), S.RUNNING) == []


def test_git管理タスクが終わるのはランが終わった後(planned_run: RunLoop):
    run = planned_run
    with pytest.raises(Rejected, match="ランが終わった後"):
        run.status(GIT, S.FINISHED)
    with pytest.raises(Rejected, match="planning を running から finished へは動かせない"):
        run.status(PLANNING, S.FINISHED)
    with pytest.raises(Rejected, match="終端でない実装タスクがある: task1, task2"):
        run(FinishRun(command_id=new_id(), issuer=RUN_SUPERVISOR, ready_overview=True))
    run(StopTasks(command_id=new_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({t(1), t(2)})))
    assert names(
        run(FinishRun(command_id=new_id(), issuer=RUN_SUPERVISOR, ready_overview=True))
    ) == ["RunFinished"]
    assert run.statuses()["planning"] is S.FINISHED
    assert names(run.status(GIT, S.FINISHED)) == ["TaskStatusChanged"]


# --- エスカレーションと回答 ---


def test_タスクの統括が上げてよい種類だけを受ける(planned_run: RunLoop):
    run = planned_run
    escalation = run.escalate(t(1), E.NEEDS_REPLAN, source="task/task1#5")
    raised = of_type(run.events, EscalationRaised)[-1]
    assert raised.task == t(1)
    assert raised.source == EventId("task/task1#5")
    assert run.aggregate.escalations[escalation].kind is E.NEEDS_REPLAN
    with pytest.raises(Rejected, match="ラン統括へ上げられるのは"):
        run.escalate(t(1), E.STALL)
    run(StopTasks(command_id=new_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({t(1), t(2)})))
    with pytest.raises(Rejected, match="もう上げない"):
        run.escalate(t(1), E.NEEDS_HUMAN)


def answer(escalation: EventId, text: str | None = None, question: str | None = None):
    return AnswerEscalation(
        command_id=new_id(),
        issuer=RUN_SUPERVISOR,
        escalation=escalation,
        answer=text,
        question=QuestionId(question) if question else None,
    )


def test_ラン統括が自分で答えると上げてきたタスクへ下ろして閉じる(planned_run: RunLoop):
    run = planned_run
    escalation = run.escalate(t(1), E.NEEDS_HUMAN, source="task/task1#5")
    events = run(answer(escalation, "A にする"))
    assert events == [
        EscalationAnswered(escalation, t(1), EventId("task/task1#5"), "A にする"),
        # 回答は EscalationAnswered で下ろすので、タスクの側を閉じる source は載せない
        EscalationClosed(escalation, "ラン統括が答えた", t(1)),
    ]
    with pytest.raises(Rejected, match="開いている Run のエスカレーションではない"):
        run(answer(escalation, "もう一度"))


def test_ユーザーの回答は届いた本文を写して1回だけその質問のエスカレーションに使う(
    planned_run: RunLoop,
):
    run = planned_run
    first = run.escalate(t(1), E.NEEDS_HUMAN, source="task/task1#5")
    second = run.escalate(t(1), E.NEEDS_HUMAN, source="task/task1#8")
    with pytest.raises(Rejected, match="まだ届いていない"):
        run(answer(first, question="q-a"))
    run.record_answer("q-a", first)
    with pytest.raises(Rejected, match="もう受け取った"):
        run.record_answer("q-a", first)
    with pytest.raises(Rejected, match="その質問を出したエスカレーション"):
        run(answer(second, question="q-a"))
    with pytest.raises(Rejected, match="どちらか一方"):
        run(answer(first, "自分の答え", question="q-a"))
    with pytest.raises(Rejected, match="どちらか一方"):
        run(answer(first))
    events = run(answer(first, question="q-a"))
    assert events[0] == EscalationAnswered(
        first, t(1), EventId("task/task1#5"), "はい", QuestionId("q-a")
    )
    run.record_answer("q-b", None)
    run(answer(second, question="q-b"))
    third = run.escalate(t(1), E.NEEDS_HUMAN)
    with pytest.raises(Rejected, match="もう使った"):
        run(answer(third, question="q-b"))


def test_回答が届く前に回答以外で閉じたエスカレーションへの回答ではラン統括を起こさない(
    planned_run: RunLoop,
):
    # ユーザーの回答（QuestionAnswered）と、ラン統括の stop-tasks が前後した。質問はもう answered
    # なので取り下げられず、回答は閉じたエスカレーションに届く
    run = planned_run
    escalation = run.escalate(t(1), E.NEEDS_HUMAN, source="task/task1#5")
    run(
        StopTasks(
            command_id=new_id(),
            issuer=RUN_SUPERVISOR,
            tasks=frozenset({t(1), t(2)}),
            responds_to=escalation,
        )
    )
    run.record_answer("q-a", escalation)
    recorded = run.events[-1]
    assert isinstance(recorded, AnswerRecorded)
    # 起こしても answer は拒まれ、差し戻しを使い切ると supervisor-failed → ユーザーに聞く →
    # 同じ知らせで起こし直す、の輪になる
    assert wake_for(recorded, run.aggregate.event_id) is None
    with pytest.raises(Rejected, match="開いている Run のエスカレーションではない"):
        run(answer(escalation, question="q-a"))
    assert recorded.escalation_closed
    assert run.replayed().answers == run.aggregate.answers


def test_Runで開いたことの無いidへの回答は閉じた印を付けずラン統括を起こす(planned_run: RunLoop):
    # ラン統括が ask-user の escalation にタスクの側の id を取り違えて書いた。印を付けて起こさないと、
    # 開いたままのエスカレーションを起こすものが無くなる
    run = planned_run
    run.escalate(t(1), E.NEEDS_HUMAN, source="task/task1#5")
    run.record_answer("q-a", EventId("task/task1#5"))
    recorded = run.events[-1]
    assert isinstance(recorded, AnswerRecorded)
    assert not recorded.escalation_closed
    assert wake_for(recorded, run.aggregate.event_id) is not None
    assert run.replayed().opened_escalations == run.aggregate.opened_escalations


def failed(supervisor: TaskId | None, notice: str) -> ReportSupervisorFailure:
    return ReportSupervisorFailure(
        command_id=new_id(),
        issuer=DRIVER,
        supervisor=supervisor,
        notice=EventId(notice),
        reason="判断を返さなかった",
    )


def test_タスク統括が応じなければラン統括へ上げ答えると知らせを載せて閉じる(planned_run: RunLoop):
    run = planned_run
    notice = EventId("task/git#9")
    (raised,) = run(failed(t(1), "task/git#9"))
    assert raised == EscalationRaised(
        E.SUPERVISOR_FAILED,
        Pointers(),
        task=t(1),
        reason="判断を返さなかった",
        failed_notice=notice,
        failures=1,
    )
    escalation = run.aggregate.event_id
    # 同じ知らせで 2 回上げない（配り直し・呼び直し）
    assert run(failed(t(1), "task/git#9")) == []
    events = run(answer(escalation, "続けてよい"))
    assert events[0] == EscalationAnswered(
        escalation, t(1), None, "続けてよい", failed_notice=notice
    )
    with pytest.raises(Rejected, match="LLM の統括を持つのは実装タスクだけ"):
        run(failed(GIT, "task/git#10"))
    # 終わったタスクの統括は、もう起こし直さない
    run(StopTasks(command_id=new_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({t(1), t(2)})))
    assert run(failed(t(1), "task/git#11")) == []


def test_ラン統括が応じなければユーザーが受けその回答で閉じて知らせから起こし直す(run: RunLoop):
    notice = EventId("run#7")
    (raised,) = of_type(run(failed(None, "run#7")), EscalationRaised)
    escalation = run.aggregate.event_id
    assert (raised.task, raised.failed_notice) == (None, notice)
    assert run.aggregate.escalations[escalation].route.level is SupervisorLevel.USER
    run.record_answer("supervisor-failed-1", escalation)
    assert run.events[-2:] == [
        AnswerRecorded(QuestionId("supervisor-failed-1"), "はい", escalation, notice),
        EscalationClosed(escalation, "ユーザーが答えた", None),
    ]
    assert run.aggregate.escalations == {}


def test_ユーザーが受けるエスカレーションにラン統括は応えず閉じない(planned_run: RunLoop):
    run = planned_run
    run(failed(None, "run#7"))
    mine = run.aggregate.event_id
    other = run.escalate(GIT, E.INTEGRATION_FAILED)
    for command in (
        answer(mine, "自分で答える"),
        InsertTask(command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("x"), responds_to=mine),
        StopTasks(
            command_id=new_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({t(2)}), responds_to=mine
        ),
        RequestReplan(command_id=new_id(), issuer=RUN_SUPERVISOR, reason="r", trigger=mine),
    ):
        with pytest.raises(Rejected, match="ユーザー）が受けるエスカレーション"):
            run(command)
    assert mine in run.aggregate.escalations
    # 再計画を反映した後に開いているものとして、ラン統括に渡さない
    run.replan(trigger=other)
    run.settle(proposal(planned(1), planned(2, 1), version=2))
    left = run.escalate(t(1), E.NEEDS_HUMAN)
    plan = of_type(run.apply_replan(), TasksPlanned)[0]
    assert plan.still_open == (left,)


def test_ランを終えた後もgit管理タスクの上げにラン統括が応じなければユーザーに聞き回答を受ける(
    planned_run: RunLoop,
):
    run = finished(planned_run)
    escalation = run.escalate(GIT, E.STAGE_ERRORS, source="task/git#20")
    (raised,) = of_type(run(failed(None, str(escalation))), EscalationRaised)
    failure = run.aggregate.event_id
    assert raised.failed_notice == escalation
    assert run.aggregate.escalations[failure].route.level is SupervisorLevel.USER
    run.record_answer("supervisor-failed-x", failure)
    assert run.events[-2] == AnswerRecorded(
        QuestionId("supervisor-failed-x"), "はい", failure, escalation
    )
    # 起こし直したラン統括が、その回答の知らせに応じなくても受ける
    retry = EventId.of(StreamId.run(), run.aggregate.version - 1)
    assert of_type(run(failed(None, str(retry))), EscalationRaised)
    # 起こし直したラン統括は、git 管理タスクの上げに答えられる
    (answered,) = of_type(run(answer(escalation, "続けてよい")), EscalationAnswered)
    assert answered.task == GIT
    # git 管理タスクに関わらない知らせなら、ランを終えた後は受けない
    with pytest.raises(Rejected, match="ランはもう終わっている"):
        run(failed(None, "run#3"))


def _fail_past_limit(run: RunLoop) -> tuple[EventId, EventId]:
    """task1 のタスク統括が同じ知らせの輪で上限を超えて応じなかった。その上げと、最後の知らせ。"""
    notice = "task/task1#9"
    for failures in range(1, MAX_SUPERVISOR_FAILURES + 1):
        (raised,) = of_type(run(failed(t(1), notice)), EscalationRaised)
        assert raised.failures == failures
        run(answer(run.aggregate.event_id, "続けて"))
        # 起こし直しの知らせは、ラン統括が答えた EscalationAnswered
        notice = str(EventId.of(StreamId.run(), run.aggregate.version - 1))
    (raised,) = of_type(run(failed(t(1), notice)), EscalationRaised)
    assert raised.failures == MAX_SUPERVISOR_FAILURES + 1
    return run.aggregate.event_id, EventId(notice)


def test_タスク統括が同じ知らせに続けて応じなければ上限を超えたところでラン統括の自前の答えを拒む(
    planned_run: RunLoop,
):
    run = planned_run
    escalation, notice = _fail_past_limit(run)
    # 段を飛ばさない。受けるのはラン統括のまま
    assert run.aggregate.escalations[escalation].route.level is SupervisorLevel.RUN
    with pytest.raises(Rejected, match="ask-user でユーザーに聞いてから"):
        run(answer(escalation, "続けて"))
    # ラン統括がユーザーに聞き、その回答を添えて答える。回答はラン統括を起こすだけで、閉じない
    run.record_answer("t1-stuck", escalation)
    assert run.events[-1] == AnswerRecorded(QuestionId("t1-stuck"), "はい", escalation)
    assert escalation in run.aggregate.escalations
    events = run(answer(escalation, question="t1-stuck"))
    assert events[0] == EscalationAnswered(
        escalation, t(1), None, "はい", QuestionId("t1-stuck"), failed_notice=notice
    )
    # ユーザーの答えで起こし直しても応じなければ、また数え続け、また自前の答えを拒む
    retry = str(EventId.of(StreamId.run(), run.aggregate.version - 1))
    (raised,) = of_type(run(failed(t(1), retry)), EscalationRaised)
    assert raised.failures == MAX_SUPERVISOR_FAILURES + 2
    with pytest.raises(Rejected, match="ask-user でユーザーに聞いてから"):
        run(answer(run.aggregate.event_id, "続けて"))


def test_上限を超えたタスク統括の上げにラン統括はタスクを止めて応えられる(planned_run: RunLoop):
    run = planned_run
    escalation, _ = _fail_past_limit(run)
    events = run(
        StopTasks(
            command_id=new_id(),
            issuer=RUN_SUPERVISOR,
            tasks=frozenset({t(1), t(2)}),
            responds_to=escalation,
        )
    )
    assert escalation in {e.escalation for e in of_type(events, EscalationClosed)}
    assert escalation not in run.aggregate.escalations


def test_応じなかったと受けた知らせは閉じた後も覚えていて2度目は上げない(planned_run: RunLoop):
    run = planned_run
    run(failed(t(1), "task/task1#9"))
    escalation = run.aggregate.event_id
    run(answer(escalation, "続けて"))
    assert run.aggregate.reported_failure(EventId("task/task1#9"))
    assert run(failed(t(1), "task/task1#9")) == []
    assert not run.aggregate.reported_failure(EventId("task/task1#10"))


def test_呼び直された回数を数える(run: RunLoop):
    assert run.aggregate.resumes == 0
    run(ResumeRun(command_id=new_id(), issuer=CLI))
    run(ResumeRun(command_id=new_id(), issuer=CLI))
    assert run.aggregate.resumes == 2


def test_差し込みは応えたエスカレーションを閉じる(planned_run: RunLoop):
    run = planned_run
    escalation = run.escalate(GIT, E.INTEGRATION_FAILED)
    events = run(
        InsertTask(
            command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("x"), responds_to=escalation
        )
    )
    assert names(events) == ["TaskInserted", "EscalationClosed"]
    assert of_type(events, EscalationClosed)[0].task == GIT
    assert run.aggregate.escalations == {}


def finished(run: RunLoop) -> RunLoop:
    run(StopTasks(command_id=new_id(), issuer=RUN_SUPERVISOR, tasks=frozenset({t(1), t(2)})))
    run(FinishRun(command_id=new_id(), issuer=RUN_SUPERVISOR, ready_overview=True))
    return run


def test_ランが終わった後もgit管理タスクのエスカレーションとその回答は受ける(planned_run: RunLoop):
    run = finished(planned_run)
    # 仕上げの並び（RunFinished の後に走る）の途中で上げた
    escalation = run.escalate(GIT, E.STAGE_ERRORS, source="task/git#20")
    run.record_answer("q-ready", escalation)
    events = run(answer(escalation, question="q-ready"))
    assert events[0] == EscalationAnswered(
        escalation, GIT, EventId("task/git#20"), "はい", QuestionId("q-ready")
    )
    # 呼び直しも、git 管理タスクが仕上げを終えるまでは受ける
    assert run(ResumeRun(command_id=new_id(), issuer=CLI)) == [RunResumed((GIT,))]
    run.status(GIT, S.FINISHED)
    with pytest.raises(Rejected, match="ランはもう終わっている"):
        run(ResumeRun(command_id=new_id(), issuer=CLI))


def test_ランが終わった後も走るタスクが残っている間はパニックを受ける(planned_run: RunLoop):
    run = finished(planned_run)
    # 仕上げの途中の git 管理タスクの上げに応じるラン統括が、利用枠に当たった
    run.escalate(GIT, E.STAGE_ERRORS, source="task/git#20")
    assert names(run(Panic(command_id=new_id(), issuer=DRIVER, cause="429"))) == ["RunPanicked"]
    assert run(ResumeRun(command_id=new_id(), issuer=CLI)) == [RunResumed((GIT,), after_panic=True)]
    run.status(GIT, S.FINISHED)
    with pytest.raises(Rejected, match="ランはもう終わっている"):
        run(Panic(command_id=new_id(), issuer=DRIVER, cause="429"))


def test_答え以外では閉じられないエスカレーションは答え以外の判断で閉じない(planned_run: RunLoop):
    """やめるとランが終わらない止めた git の仕事の上げ。閉じると Stack がやめるのを拒んで止まる。"""
    run = planned_run
    run(
        EscalateToRun(
            command_id=new_id(),
            issuer=Issuer.task_supervisor(GIT),
            task=GIT,
            kind=E.STAGE_ERRORS,
            reason="git の仕事が済んでいない",
            pointers=Pointers(),
            source=EventId("task/git#20"),
            answer_only=True,
        )
    )
    escalation = run.aggregate.event_id
    assert run.aggregate.escalations[escalation].answer_only
    closing = [
        InsertTask(
            command_id=new_id(),
            issuer=RUN_SUPERVISOR,
            spec=spec("別の作業"),
            responds_to=escalation,
        ),
        StopTasks(
            command_id=new_id(),
            issuer=RUN_SUPERVISOR,
            tasks=frozenset({t(1), t(2)}),
            responds_to=escalation,
        ),
        RequestReplan(
            command_id=new_id(), issuer=RUN_SUPERVISOR, reason="やり直す", trigger=escalation
        ),
    ]
    for command in closing:
        with pytest.raises(Rejected, match="答え以外では閉じられない"):
            run(command)
    run.replan()
    run.settle(proposal(version=2))
    with pytest.raises(Rejected, match="答え以外では閉じられない"):
        run.apply_replan(version=2, responds_to=escalation)
    assert escalation in run.aggregate.escalations
    events = run(answer(escalation, "続ける"))
    assert names(events) == ["EscalationAnswered", "EscalationClosed"]


def test_ランが終わった後はgit管理タスク以外のエスカレーションを受けない(planned_run: RunLoop):
    run = finished(planned_run)
    with pytest.raises(Rejected, match="ランはもう終わっている"):
        run.escalate(PLANNING, E.STAGE_ERRORS)


def test_統合に失敗したタスクを引き継ぐタスクは衝突したファイルを成果物として持って始まる(
    planned_run: RunLoop,
):
    run = planned_run
    run.status(t(1), S.GATED)
    run.status(t(1), S.STACKING)
    failure = RecordIntegrationFailure(
        command_id=new_id(), issuer=POLICY, task=t(1), reason="両方は残せない", files=("a.py",)
    )
    assert run(failure) == [IntegrationFailureRecorded(t(1), "両方は残せない", ("a.py",))]
    events = run(
        InsertTask(
            command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("やり直し"), takes_over=t(1)
        )
    )
    assert of_type(events, TaskInserted)[0].conflicts == ("a.py",)
    (started,) = of_type(run.start_ready(), TaskStarted)
    assert started.task == t(3)
    assert started.conflicts == ("a.py",)
    assert ArtifactRef(ArtifactKind.CONFLICTS, "task1") in started.artifacts
    # 引き継がれた後に遅れて届いた失敗の知らせは何もしない
    assert run(failure) == []


def test_計画が進んでいる間に差し込んだタスクと提案の番号がぶつかったら拒む(planned_run: RunLoop):
    run = planned_run
    run.replan()
    # 提案は差し込む前の一覧から書いたので、task3 は別の新しいタスクのつもり
    run(InsertTask(command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("差し込んだ")))
    run.settle(proposal(planned(1), planned(2, 1), planned(3), version=2))
    with pytest.raises(Rejected, match="task3 は計画の間に差し込んだタスクの番号"):
        run.apply_replan()
    run.settle(proposal(planned(1), planned(2, 1), planned(4), version=3))
    assert names(run.apply_replan(version=3)) == ["TasksPlanned"]


# --- 積む列にいるタスクの範囲の変更 ---


def test_積む列にいるタスクの範囲が変わったら列から外し上限を見て新しいブランチで始め直す():
    run = RunLoop(Run(StreamId.run()))
    run.start(limit=1)
    run.plan(planned(1), planned(2))
    run.start_ready()  # task1
    run.status(t(1), S.GATED)
    run.start_ready()  # task2（task1 は積む列で待っていて上限に数えない）
    run.replan()
    run.settle(proposal(planned(1, title="広げた"), planned(2), version=2))
    plan = run.apply_replan()[0]
    assert isinstance(plan, TasksPlanned)
    # 積む頼みを外し（WithdrawRequest）、すぐには ChangeScope を送らない
    assert plan.withdraw == {t(1)}
    assert plan.scope_changed == set()
    assert run.statuses()["task1"] is S.PENDING
    # 上限（1）は task2 が使っているので、まだ始め直さない
    assert run.start_ready() == []
    run.stack(t(2))
    (started,) = run.start_ready()
    assert started == TaskStarted(
        t(1),
        started.kind,
        spec=spec("広げた"),
        artifacts=ARTIFACTS,
        # push したかもしれない名前は使わない
        branch=BranchName("stack/add-cache--task-1-r1"),
        reopened=True,
    )


def test_統合の失敗に答えてやり直すなら印を下ろしもう引き継がない(planned_run: RunLoop):
    run = planned_run
    run.fail_integration(t(1))
    clear = ClearIntegrationFailure(command_id=new_id(), issuer=POLICY, task=t(1))
    assert run(clear) == [IntegrationFailureCleared(t(1))]
    assert run(replace(clear, command_id=new_id())) == []
    with pytest.raises(Rejected, match="統合に失敗していない"):
        run(InsertTask(command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("x"), takes_over=t(1)))


def test_積む仕事のフローを捨てて仕事を残したらgatedに戻せる(planned_run: RunLoop):
    run = planned_run
    run.status(t(1), S.GATED)
    run.status(t(1), S.STACKING)
    assert names(run.status(t(1), S.GATED)) == ["TaskStatusChanged"]


def test_初めての計画を反映するまでは差し込まない(run: RunLoop):
    # 提案は差し込む前の一覧から書くので、差し込んだタスクの番号の別のタスクを書いてしまう
    with pytest.raises(Rejected, match="まだ一度も計画を反映していない"):
        run(InsertTask(command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("先に要る")))
    run.plan(planned(1))
    events = run(InsertTask(command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("後で要る")))
    assert [e.task for e in of_type(events, TaskInserted)] == [t(2)]


def test_閉じる前に積み終えたタスクを一緒に閉じるかは積んだときに決める():
    run = RunLoop(Run(StreamId.run()))
    run.start()
    run.plan(planned(1), planned(2), planned(3))
    run.start_ready()
    run.stack(t(1))
    run.stack(t(2))
    run.discard(1)
    (stacked, *_) = run.stack(t(3))
    assert stacked == TaskMarkedStacked(t(3), PrNumber(10), requeue=True)
    assert run.aggregate.requeue == (t(2), t(3))


# --- エスカレーションの上げ元 ---


def test_回答を待っているタスクから上げ元を書かずに上げると回答が下りないので拒む(
    planned_run: RunLoop,
):
    run = planned_run
    # 待っているエスカレーションが無ければ、上げ元を書かずに上げてよい（回答は notes に残る）
    run.escalate(t(1), E.NEEDS_HUMAN)
    run.status(t(1), S.ESCALATED)
    with pytest.raises(Rejected, match="source を書く"):
        run.escalate(t(1), E.NEEDS_HUMAN)
    run.escalate(t(1), E.NEEDS_HUMAN, source="task/task1#7")


def test_タスクの側で閉じたエスカレーションの中継を閉じる(planned_run: RunLoop):
    run = planned_run
    relayed = run.escalate(GIT, E.STAGE_ERRORS, source="task/git#4")
    other = run.escalate(t(1), E.NEEDS_HUMAN, source="task/task1#3")
    close = CloseRelayedEscalation(
        command_id=new_id(), issuer=POLICY, source=EventId("task/git#4"), reason="フローを捨てた"
    )
    # タスクの側はもう閉じたので、タスクの側を閉じる source は載せない
    assert run(close) == [EscalationClosed(relayed, "フローを捨てた", GIT)]
    assert set(run.aggregate.escalations) == {other}
    # 中継が無ければ何もしない
    assert run(replace(close, command_id=new_id())) == []


# --- 再生 ---


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (RunPhase.NOT_STARTED, False),
        (RunPhase.PLANNING, True),
        (RunPhase.RUNNING, True),
        (RunPhase.PANICKED, False),
        (RunPhase.FINISHING, True),
        (RunPhase.FINISHED, False),
    ],
)
def test_driverが走っているはずのフェーズは計画中と走っているときと仕上げ中(
    phase: RunPhase, expected: bool
):
    assert phase.expects_driver is expected


def test_積んだ数と積む数の分母は実装タスクだけを数え外れたタスクを分母から外す():
    numbered = [t(n) for n in range(1, 8)]
    statuses = (S.STACKED, S.STACKED, S.RUNNING, S.DROPPED, S.SUPERSEDED, S.DISCARDED, S.GATED)
    events = [
        RunStarted(
            NAME, Instruction("足す"), Repository("/repo"), BranchName("main"), ParallelLimit(3)
        ),
        TaskStarted(PLANNING, TaskKind.PLANNING),
        TaskStarted(GIT, TaskKind.GIT),
        TasksPlanned(
            DesignVersion(1), tuple(planned(n) for n in range(1, 8)), (), ARTIFACTS, replan=False
        ),
        *(
            TaskStatusChanged(task, S.PENDING, status, "x")
            for task, status in zip(numbered, statuses, strict=True)
        ),
        TaskStatusChanged(PLANNING, S.RUNNING, S.FINISHED, "x"),
    ]
    run = Run.replay(StreamId.run(), [(e, CommandId(f"c{i}")) for i, e in enumerate(events)])
    assert (run.stacked_count, run.stack_target_count) == (2, 4)


def test_イベントの列を再生すると同じ状態になる(planned_run: RunLoop):
    run = planned_run
    run.stack(t(1))
    escalation = run.escalate(t(2), E.NEEDS_HUMAN)
    run.record_answer("q-a", escalation)
    run(answer(escalation, question="q-a"))
    run.replan()
    run(InsertTask(command_id=new_id(), issuer=RUN_SUPERVISOR, spec=spec("x")))
    run.settle(proposal(planned(2), version=2, discard=frozenset({t(1)})))
    run.apply_replan(discard=frozenset({t(1)}))
    assert vars(run.replayed()) == vars(run.aggregate)
