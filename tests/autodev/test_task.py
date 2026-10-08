"""Task 集約（`domain/aggregates/task.py`）。コマンドとイベントの列だけで、不変条件ごとに通る場合と拒む場合を見る。"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from autodev_harness import CLI, DRIVER, POLICY, SESSION, Loop, names, new_id, of_type
from autodev_samples import stage_result

from autodev.domain.aggregates.base import Rejected
from autodev.domain.aggregates.task import ExecutionStatus, Task
from autodev.domain.commands.task import (
    AbandonFlow,
    AcceptFlow,
    BeginStage,
    ChangeScope,
    CloseEscalation,
    ConcludeDesignRound,
    ConcludeGateRound,
    ConcludeReviewRound,
    ConfirmHandoff,
    Escalate,
    MarkInterrupted,
    OpenTask,
    RecordBase,
    ReportBeginFailure,
    ReportStageResult,
    ResolveEscalation,
    ResumeInterrupted,
    ResumeStage,
    StopTask,
)
from autodev.domain.events.run import EscalationRaised
from autodev.domain.events.task import (
    BaseRecorded,
    BranchRebased,
    EscalationClosed,
    EscalationResolved,
    ExecutionRestarted,
    FlowAbandoned,
    FlowAccepted,
    FlowFinished,
    FlowRejected,
    GateFailed,
    HandoffConfirmed,
    NoteAdded,
    RoundConcluded,
    StageCancelled,
    StageCompleted,
    StageDeferred,
    StageFailed,
    StageInterrupted,
    StageReported,
    StageRequested,
    StageSentBack,
    StageStarted,
    TaskGated,
    TaskOpened,
    WorktreeReady,
)
from autodev.domain.flow.flow import Cursor, Flow, FlowStep, Reviewers
from autodev.domain.flow.standard import git_job_flow
from autodev.domain.services.escalation_router import EscalationRouter, SupervisorLevel
from autodev.domain.stages.catalog import STAGE_SPECS
from autodev.domain.stages.kinds import Handoff, InnerRole, SessionScope, StageMode
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.artifact_ref import ArtifactRef
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.commit_sha import CommitSha
from autodev.domain.value_objects.decision import Decision
from autodev.domain.value_objects.decision_origin import DecisionOrigin
from autodev.domain.value_objects.deferred_call import DeferredCall
from autodev.domain.value_objects.design_version import DesignVersion
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.evidence import Evidence
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.finding_id import FindingId
from autodev.domain.value_objects.finding_route import FindingRoute
from autodev.domain.value_objects.finding_target import FindingTarget
from autodev.domain.value_objects.gate_item import GateItem
from autodev.domain.value_objects.gate_item_result import GateItemResult
from autodev.domain.value_objects.gate_report import GateReport
from autodev.domain.value_objects.git_job import GitJob
from autodev.domain.value_objects.git_job_kind import GitJobKind
from autodev.domain.value_objects.interrupt_cause import InterruptCause
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.location import Location
from autodev.domain.value_objects.pointers import Pointers
from autodev.domain.value_objects.pr_number import PrNumber
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.rating import Rating
from autodev.domain.value_objects.reported_finding import ReportedFinding
from autodev.domain.value_objects.stage_exit import StageExit
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stall_cause import StallCause
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_kind import TaskKind
from autodev.domain.value_objects.task_spec import TaskSpec
from autodev.domain.value_objects.union_file_verdict import UnionFileVerdict
from autodev.domain.value_objects.union_verdict import UnionVerdict
from autodev.domain.value_objects.verify_command import VerifyCommand
from autodev.domain.value_objects.verify_kind import VerifyKind
from autodev.domain.value_objects.verify_result import VerifyResult
from autodev.domain.value_objects.write_scope import WriteScope

S = StageKind
A = ArtifactKind
E = EscalationKind
X = ExecutionStatus
HEAD = CommitSha("a" * 40)
BRANCH = BranchName("stack/add-cache--task-1")
DESIGN = ArtifactRef(A.DESIGN, "1")
BOTH = Reviewers((S.REVIEW, S.ADVERSARIAL_REVIEW), later=(S.REVIEW,))
IMPL_FLOW = (
    FlowStep(S.IMPL),
    FlowStep(S.REVIEW_LOOP, reviewers=BOTH),
    FlowStep(S.GATE),
    FlowStep(S.WRITE_PR_BODY),
)
PASSED = GateReport(tuple(GateItemResult(item, True) for item in GateItem))
#: `--resume` で起こした claude が、init を出さずに自分で終わった証拠。claude 2.1.288 は
#: `error_during_execution`・`num_turns: 0` の result を返して終わる
LOST_SESSION: dict[str, Any] = {"resumed": True, "initialized": False, "num_turns": 0}


def gate(*failed: GateItem) -> GateReport:
    return GateReport(tuple(GateItemResult(item, item not in failed, "x") for item in GateItem))


def requested(events) -> list[ExecutionId]:
    return [event.execution for event in of_type(events, StageRequested)]


class TaskLoop(Loop[Task]):
    def __init__(self, task: TaskId) -> None:
        super().__init__(Task(StreamId.task(task)))
        self.task = task
        session = SESSION if task.kind is TaskKind.IMPLEMENTATION else None
        self.supervisor = Issuer.task_supervisor(task, session)
        #: 提案の版（実行器が design/v<版>.md に書き出す番号）
        self.proposals = 0

    def open(self, artifacts: tuple[ArtifactRef, ...] = (DESIGN,)) -> list:
        kind = self.task.kind
        return self(
            OpenTask(
                command_id=new_id(),
                issuer=POLICY,
                task=self.task,
                kind=kind,
                spec=TaskSpec("キャッシュ") if kind is TaskKind.IMPLEMENTATION else None,
                artifacts=artifacts,
                branch=BRANCH if kind is TaskKind.IMPLEMENTATION else None,
            )
        )

    def flow(
        self, *steps: FlowStep, responds_to: EventId | None = None, job: GitJob | None = None
    ) -> list:
        return self(
            AcceptFlow(
                command_id=new_id(),
                issuer=self.supervisor,
                task=self.task,
                steps=steps,
                responds_to=responds_to,
                job=job,
            )
        )

    def take(self, job: GitJob) -> list:
        """git 管理タスクの統括（プログラム）が、取り出した仕事の決まった並びを組む。"""
        return self.flow(*git_job_flow(job), job=job)

    def begin(self, execution: ExecutionId) -> list:
        llm = STAGE_SPECS[execution.stage].mode is StageMode.LLM
        return self(
            BeginStage(
                command_id=new_id(),
                issuer=Issuer.executor(execution),
                task=self.task,
                execution=execution,
                head=HEAD,
                session=SESSION if llm else None,
            )
        )

    def report(
        self,
        execution: ExecutionId,
        *,
        products: tuple[ArtifactKind, ...] | None = None,
        result: dict | None = None,
        exact: bool = False,
        **evidence,
    ) -> list:
        """`result` は、そのステージが読む欄の既定の値（何も挙げない）に重ねる。`exact` ならそのまま渡す。"""
        made = STAGE_SPECS[execution.stage].produces if products is None else products
        evidence.setdefault("exit", StageExit.OK)
        evidence.setdefault("result_valid", True)
        # 根元から上のコミットがある（Rebase は 0 件・数えられないと落ちる）
        evidence.setdefault("commits", 1)
        if not exact:
            result = stage_result(execution.stage, **(result or {}))
        return self(
            ReportStageResult(
                command_id=new_id(),
                issuer=Issuer.executor(execution),
                task=self.task,
                execution=execution,
                evidence=Evidence(
                    products=tuple(self._product(kind) for kind in sorted(made, key=str)),
                    **evidence,
                ),
                pointers=Pointers(),
                result=result,
            )
        )

    def _product(self, kind: ArtifactKind) -> ArtifactRef:
        if kind is A.PROPOSAL:
            # 実行器は、提案の本文を書き出した design/v<版>.md の版を在りかにする
            self.proposals += 1
            return ArtifactRef(kind, str(self.proposals))
        return ArtifactRef(kind, "x")

    def run(self, execution: ExecutionId, *, confirm: bool = True, **kw) -> list:
        """始めて報告する。結果を渡すステージなら、受け取る側が受けた（ConfirmHandoff）ところまで進める。

        判定は、受けたことを Conclude*Round が知らせるので、ここでは進めない。
        """
        self.begin(execution)
        events = self.report(execution, **kw)
        completed = [e for e in of_type(events, StageCompleted) if e.execution == execution]
        if confirm and completed and completed[0].handoff not in (None, Handoff.JUDGEMENT):
            events += self.confirm(execution)
        return events

    def confirm(self, execution: ExecutionId, refused: str | None = None) -> list:
        return self(
            ConfirmHandoff(
                command_id=new_id(),
                issuer=POLICY,
                task=self.task,
                execution=execution,
                refused=refused,
            )
        )

    def conclude(
        self,
        judge: ExecutionId,
        *unresolved: str,
        stalled: tuple[str, ...] = (),
        targets: dict[str, FindingTarget] | None = None,
    ) -> list:
        """`targets` は指摘ごとの宛先。書かない指摘は routes に載せない（impl として扱われる）。"""
        routes = tuple(FindingRoute(FindingId(f), t) for f, t in (targets or {}).items())
        return self(
            ConcludeReviewRound(
                command_id=new_id(),
                issuer=POLICY,
                task=self.task,
                judge=judge,
                unresolved=tuple(FindingId(f) for f in unresolved),
                stalled=tuple(FindingId(f) for f in stalled),
                cause=StallCause.TESTS if stalled else None,
                routes=routes,
            )
        )

    def resolve(self, escalation: EventId, question: str | None = None) -> list:
        return self(
            ResolveEscalation(
                command_id=new_id(),
                issuer=POLICY,
                task=self.task,
                escalation=escalation,
                answer="こうする",
                question=QuestionId(question) if question else None,
            )
        )

    def escalation_ids(self) -> list[EventId]:
        return list(self.aggregate.escalations)


T1 = TaskId("task1")


def ex(stage: StageKind, round: int = 0, attempt: int = 1, task: TaskId = T1) -> ExecutionId:
    return ExecutionId(task, stage, round, attempt)


def reported(kind: str, reason: str = "自分では解けない") -> dict:
    """報告を返した結果（report と reportReason は組で書く）。"""
    return {"report": kind, "reportReason": reason}


@pytest.fixture
def task() -> TaskLoop:
    loop = TaskLoop(T1)
    loop.open()
    return loop


@pytest.fixture
def reviewing(task: TaskLoop) -> TaskLoop:
    """実装を終え、ReviewLoop の 1 ラウンド目の判定を待っているタスク。"""
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    task.run(ex(S.REVIEW, 1))
    task.run(ex(S.ADVERSARIAL_REVIEW, 1))
    task.run(ex(S.JUDGE, 1))
    return task


# --- 開く ---


def test_タスクのストリームはTaskOpenedから始まり種類はidで決まる():
    loop = TaskLoop(T1)
    with pytest.raises(Rejected, match="まだ開いていない"):
        loop.flow(*IMPL_FLOW)
    assert names(loop.open()) == ["TaskOpened"]
    with pytest.raises(Rejected, match="もう開いている"):
        loop.open()
    with pytest.raises(Rejected, match="種類は planning"):
        TaskLoop(TaskId.planning())(
            OpenTask(
                command_id=new_id(),
                issuer=POLICY,
                task=TaskId.planning(),
                kind=TaskKind.IMPLEMENTATION,
                spec=TaskSpec("x"),
            )
        )
    with pytest.raises(Rejected, match="中身（TaskSpec）を持つのは実装タスクだけ"):
        TaskLoop(TaskId.git())(
            OpenTask(
                command_id=new_id(),
                issuer=POLICY,
                task=TaskId.git(),
                kind=TaskKind.GIT,
                spec=TaskSpec("x"),
            )
        )


# --- フロー ---


def test_検査に落ちたフローはFlowRejectedで理由を返す(task: TaskLoop):
    events = task.flow(FlowStep(S.IMPL))
    assert names(events) == ["FlowRejected"]
    rejected = events[0]
    assert isinstance(rejected, FlowRejected)
    assert "フローの終わりまでに gated, pr-body が作られない" in rejected.reasons
    assert task.aggregate.flow is None


def test_フローを受けると最初のステージを走らせると決める(task: TaskLoop):
    events = task.flow(*IMPL_FLOW)
    assert names(events) == ["FlowAccepted", "StageRequested"]
    assert requested(events) == [ex(S.IMPL)]
    assert task.aggregate.flow is not None
    assert task.aggregate.flow.version == 1


def test_フローの途中では応えるエスカレーションか範囲の変更が無いとフローを受けない(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    with pytest.raises(Rejected, match="フローの途中で"):
        task.flow(*IMPL_FLOW)


def test_待っているエスカレーションがあれば応えるフローだけを受けて閉じる(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL), result=reported("design-gap"))
    (escalation,) = task.escalation_ids()
    with pytest.raises(Rejected, match="responds_to を書く"):
        task.flow(*IMPL_FLOW)
    with pytest.raises(Rejected, match="開いているエスカレーションではない"):
        task.flow(*IMPL_FLOW, responds_to=EventId("task/task1#99"))
    events = task.flow(*IMPL_FLOW, responds_to=escalation)
    assert names(events) == ["FlowAccepted", "StageRequested"]
    assert task.escalation_ids() == []
    # 同じ Impl を新しいフローで走らせても、実行の id は重ならない
    assert requested(events) == [ex(S.IMPL, attempt=2)]


# --- ステージを始める ---


def test_始めるのは走らせると決めた実行だけでセッションはLLMのステージだけが持つ(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    with pytest.raises(Rejected, match="requested でない"):
        task.begin(ex(S.IMPL, attempt=2))
    with pytest.raises(Rejected, match="セッションを持つのは LLM のステージだけ"):
        task(
            BeginStage(
                command_id=new_id(),
                issuer=Issuer.executor(ex(S.IMPL)),
                task=T1,
                execution=ex(S.IMPL),
                head=HEAD,
                session=None,
            )
        )
    assert task.begin(ex(S.IMPL)) == [StageStarted(ex(S.IMPL), HEAD, SESSION)]
    with pytest.raises(Rejected, match="requested でない（running）"):
        task.begin(ex(S.IMPL))


def test_結果を報告できるのは走っている実行だけ(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    with pytest.raises(Rejected, match="running でない（requested）"):
        task.report(ex(S.IMPL))


# --- 結果の判断 ---


def test_完了するとcursorが進み次のステージを決める(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    events = task.run(ex(S.IMPL))
    assert names(events) == ["StageCompleted", "StageRequested", "StageRequested"]
    # tests が無いので Expect は置かず、1 ラウンド目のレビューを並列に走らせる
    assert requested(events) == [ex(S.REVIEW, 1), ex(S.ADVERSARIAL_REVIEW, 1)]
    assert A.IMPL in task.aggregate.artifacts


def test_defer_で止まったら結果の形より先に見てaskで上げる():
    planning = TaskLoop(TaskId.planning())
    planning.open(artifacts=())
    planning.flow(FlowStep(S.PREPARE), FlowStep(S.PLAN), FlowStep(S.DESIGN_LOOP))
    planning.run(ex(S.PREPARE, task=TaskId.planning()))
    plan = ex(S.PLAN, task=TaskId.planning())
    planning.begin(plan)
    events = planning.report(
        plan, products=(), result_valid=False, deferred=DeferredCall("toolu_1", "A と B どちら？")
    )
    assert names(events) == ["StageDeferred", "EscalationRaised"]
    assert of_type(events, StageDeferred) == [StageDeferred(plan, "toolu_1")]
    assert of_type(events, EscalationRaised)[0].kind is E.ASK
    assert planning.aggregate.executions[plan].status is X.DEFERRED


def test_ask_で聞けないステージがdeferで止まったら失敗にする(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.begin(ex(S.IMPL))
    events = task.report(ex(S.IMPL), deferred=DeferredCall("toolu_1", "?"))
    assert names(events)[0] == "StageFailed"


def test_エラーと形の違う結果は1回やり直し2回続けばstage_errorsで上げる(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    events = task.run(ex(S.IMPL), result_valid=False)
    assert names(events) == ["StageFailed", "StageRequested"]
    assert requested(events) == [ex(S.IMPL, attempt=2)]
    events = task.run(ex(S.IMPL, attempt=2), exit=StageExit.ERROR, error="boom")
    assert names(events) == ["StageFailed", "EscalationRaised"]
    assert of_type(events, EscalationRaised)[0].kind is E.STAGE_ERRORS
    # 解けたら同じステージをもう一度、1 回目から数え直して走らせる
    (escalation,) = task.escalation_ids()
    events = task.resolve(escalation)
    assert requested(events) == [ex(S.IMPL, attempt=3)]
    assert names(task.run(ex(S.IMPL, attempt=3), result_valid=False)) == [
        "StageFailed",
        "StageRequested",
    ]


def test_報告は実物を確かめる前に見てcursorを進めない(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    # 報告を返した Impl は何も作っていないが、失敗に数えない
    events = task.run(ex(S.IMPL), products=(), result=reported("design-gap"))
    assert names(events) == ["StageReported", "EscalationRaised"]
    assert task.aggregate.cursor.step == 0
    (escalation,) = task.escalation_ids()
    # 解けたら同じステージをもう一度走らせる
    assert requested(task.resolve(escalation)) == [ex(S.IMPL, attempt=2)]


def test_そのステージが返さない報告は形の誤りにする(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    assert names(task.run(ex(S.IMPL), result=reported("ask")))[0] == "StageFailed"
    assert names(task.run(ex(S.IMPL, attempt=2), result=reported("nope")))[0] == "StageFailed"


def test_実物が無ければ失敗にし宣言していない成果物は増やさない(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    assert names(task.run(ex(S.IMPL), products=()))[0] == "StageFailed"
    events = task.run(ex(S.IMPL, attempt=2), products=(A.IMPL, A.GATED))
    completed = of_type(events, StageCompleted)[0]
    assert [p.kind for p in completed.produced] == [A.IMPL]
    assert A.GATED not in task.aggregate.artifacts


# --- ReviewLoop ---


def test_ReviewLoopはレビューを並列に走らせ台帳が指摘を受けて揃ったらJudgeで判定を待つ(
    task: TaskLoop,
):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    found = {"rating": "must-fix", "body": "境界で落ちる", "location": "src/a.py:3"}
    events = task.run(ex(S.REVIEW, 1), confirm=False, result={"findings": [found]})
    (completed,) = of_type(events, StageCompleted)
    # 結果の中身を値にして載せ、指摘の台帳が受けるまで cursor を進めない
    assert completed.handoff is Handoff.FINDINGS
    assert completed.result is not None
    assert completed.result.findings == (
        ReportedFinding(Rating.MUST_FIX, "境界で落ちる", Location("src/a.py:3")),
    )
    assert completed.cursor is None
    assert names(task.confirm(ex(S.REVIEW, 1))) == ["HandoffConfirmed"]
    assert requested(task.run(ex(S.ADVERSARIAL_REVIEW, 1), confirm=False)) == []
    events = task.confirm(ex(S.ADVERSARIAL_REVIEW, 1))
    # 行き先は handle が決めてイベントに載せる
    assert of_type(events, HandoffConfirmed)[0].cursor == Cursor(1).enter(S.JUDGE, 1)
    assert requested(events) == [ex(S.JUDGE, 1)]
    (judged,) = task.run(ex(S.JUDGE, 1))
    assert isinstance(judged, StageCompleted)
    assert (judged.handoff, judged.cursor) == (Handoff.JUDGEMENT, None)
    assert task.aggregate.cursor == Cursor(1).enter(S.JUDGE, 1)
    # 判定を台帳が受けたことは、判定を締めた知らせ（ConcludeReviewRound）で受ける
    with pytest.raises(Rejected, match="Conclude"):
        task.confirm(ex(S.JUDGE, 1))


def test_結果を渡した先が受けなければresult_refusedで上げ解けたら同じステージを走らせる(
    task: TaskLoop,
):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    task.run(ex(S.REVIEW, 1), confirm=False)
    events = task.confirm(ex(S.REVIEW, 1), refused="本文が空の指摘がある")
    assert names(events) == ["HandoffFailed", "EscalationRaised"]
    raised = of_type(events, EscalationRaised)[0]
    assert (raised.kind, raised.origin, raised.reason) == (
        E.RESULT_REFUSED,
        ex(S.REVIEW, 1),
        "本文が空の指摘がある",
    )
    assert task.aggregate.executions[ex(S.REVIEW, 1)].status is X.REFUSED
    # 並列の見る役が受けられても、受けなかった見る役が済むまで判定へ進まない
    task.run(ex(S.ADVERSARIAL_REVIEW, 1))
    assert task.aggregate.cursor.inner is not S.JUDGE
    (escalation,) = task.escalation_ids()
    assert requested(task.resolve(escalation)) == [ex(S.REVIEW, 1, attempt=2)]
    # 遅れて届いた同じ知らせは何もしない
    assert task.confirm(ex(S.REVIEW, 1), refused="x") == []


def test_openの指摘が残ればFixへ進み次のラウンドはlaterのレビュー(reviewing: TaskLoop):
    task = reviewing
    events = task.conclude(ex(S.JUDGE, 1), "R1")
    assert names(events) == ["RoundConcluded", "StageRequested"]
    assert requested(events) == [ex(S.FIX, 1)]
    events = task.run(ex(S.FIX, 1))
    assert requested(events) == [ex(S.REVIEW, 2)]


def test_openの指摘が無ければ抜けてreviewedを作りGateへ(reviewing: TaskLoop):
    task = reviewing
    events = task.conclude(ex(S.JUDGE, 1))
    assert names(events) == ["RoundConcluded", "StageRequested"]
    concluded = of_type(events, RoundConcluded)[0]
    assert concluded.finished
    assert concluded.produced == (ArtifactRef(A.REVIEWED, str(ex(S.JUDGE, 1))),)
    assert requested(events) == [ex(S.GATE)]
    task.run(ex(S.GATE), gate=PASSED)
    events = task.run(ex(S.WRITE_PR_BODY))
    assert names(events) == ["StageCompleted", "FlowFinished", "TaskGated"]
    assert of_type(events, TaskGated) == [TaskGated(BRANCH)]
    assert of_type(events, FlowFinished) == [FlowFinished(1)]


def test_停滞は判定の後に1回だけ上げ解けたらFixへ(reviewing: TaskLoop):
    task = reviewing
    events = task.conclude(ex(S.JUDGE, 1), "R1", "R2", stalled=("R2",))
    assert names(events) == ["RoundConcluded", "EscalationRaised"]
    raised = of_type(events, EscalationRaised)[0]
    assert raised.kind is E.STALL
    assert raised.hint.finding_ids == (FindingId("R2"),)
    assert raised.hint.stall_cause is StallCause.TESTS
    (escalation,) = task.escalation_ids()
    assert requested(task.resolve(escalation)) == [ex(S.FIX, 1)]


# --- 指摘の宛先ごとの行き先（supervisor → TestFix → Fix） ---

T = FindingTarget


def route_round(task: TaskLoop, round: int = 1, **found: FindingTarget) -> list:
    """`R1=T.TESTS` のように宛先を付けた指摘を open のまま、そのラウンドの判定を締める。"""
    return task.conclude(ex(S.JUDGE, round), *found, targets=found)


def test_tests宛の指摘だけなら同じラウンドのTestFixが頼まれFixは頼まれない(reviewing: TaskLoop):
    task = reviewing
    events = route_round(task, R1=T.TESTS)
    (concluded,) = of_type(events, RoundConcluded)
    assert not concluded.finished
    assert concluded.cursor == Cursor(1).enter(S.TEST_FIX, 1)
    assert concluded.fixers == (S.TEST_FIX,)
    assert task.aggregate.cursor == concluded.cursor
    assert requested(events) == [ex(S.TEST_FIX, 1)]


def test_impl宛の指摘だけなら今どおりFixが頼まれTestFixは頼まれない(reviewing: TaskLoop):
    task = reviewing
    events = route_round(task, R2=T.IMPL)
    (concluded,) = of_type(events, RoundConcluded)
    assert concluded.cursor == Cursor(1).enter(S.FIX, 1)
    assert concluded.fixers == (S.FIX,)
    assert requested(events) == [ex(S.FIX, 1)]
    assert requested(task.run(ex(S.FIX, 1))) == [ex(S.REVIEW, 2)]
    assert S.TEST_FIX not in {e.execution.stage for e in of_type(task.events, StageRequested)}


def test_宛先を渡さない指摘はimplとして扱いFixへ回す(reviewing: TaskLoop):
    events = reviewing.conclude(ex(S.JUDGE, 1), "R1")
    assert requested(events) == [ex(S.FIX, 1)]
    assert of_type(events, RoundConcluded)[0].fixers == (S.FIX,)


def test_testsとimplの指摘が両方あればTestFixが先に走り次にFixが走り次のラウンドの頭へ進む(
    reviewing: TaskLoop,
):
    task = reviewing
    events = route_round(task, R1=T.TESTS, R2=T.IMPL)
    assert of_type(events, RoundConcluded)[0].fixers == (S.TEST_FIX, S.FIX)
    assert requested(events) == [ex(S.TEST_FIX, 1)]
    # TestFix が完了すると、同じラウンドの Fix が頼まれる
    events = task.run(ex(S.TEST_FIX, 1))
    assert requested(events) == [ex(S.FIX, 1)]
    assert task.aggregate.cursor == Cursor(1).enter(S.FIX, 1)
    # Fix が完了すると、次のラウンドの頭（later のレビュー）が頼まれる
    assert requested(task.run(ex(S.FIX, 1))) == [ex(S.REVIEW, 2)]


def test_tests宛の指摘だけならTestFixの完了で次のラウンドの頭が頼まれる(reviewing: TaskLoop):
    task = reviewing
    route_round(task, R1=T.TESTS)
    assert requested(task.run(ex(S.TEST_FIX, 1))) == [ex(S.REVIEW, 2)]
    assert S.FIX not in {e.execution.stage for e in of_type(task.events, StageRequested)}


def test_supervisor宛の指摘があればsupervisor_findingで上げ回答が届くまで直す役を頼まない(
    reviewing: TaskLoop,
):
    task = reviewing
    events = route_round(task, R1=T.TESTS, R2=T.IMPL, R3=T.SUPERVISOR, R4=T.SUPERVISOR)
    assert names(events) == ["RoundConcluded", "EscalationRaised"]
    (raised,) = of_type(events, EscalationRaised)
    assert raised.kind is E.SUPERVISOR_FINDING
    # 統括が決める指摘だけを載せる（tests・impl の指摘は直す役が受ける）
    assert raised.hint.finding_ids == (FindingId("R3"), FindingId("R4"))
    assert requested(events) == []


def test_supervisor宛の指摘があれば同じラウンドに停滞した指摘があってもstallは出さない(
    reviewing: TaskLoop,
):
    task = reviewing
    events = task.conclude(
        ex(S.JUDGE, 1),
        "R1",
        "R3",
        stalled=("R1",),
        targets={"R1": T.TESTS, "R3": T.SUPERVISOR},
    )
    raised = of_type(events, EscalationRaised)
    assert [e.kind for e in raised] == [E.SUPERVISOR_FINDING]
    assert raised[0].hint.finding_ids == (FindingId("R3"),)
    assert not [e for e in of_type(task.events, EscalationRaised) if e.kind is E.STALL]


def test_supervisorの回答の後はtestsがあればTestFixが頼まれsupervisorだけなら次のラウンドの頭が頼まれる(
    reviewing: TaskLoop,
):
    with_tests = reviewing
    route_round(with_tests, R1=T.TESTS, R3=T.SUPERVISOR)
    (escalation,) = with_tests.escalation_ids()
    assert requested(with_tests.resolve(escalation)) == [ex(S.TEST_FIX, 1)]
    assert requested(with_tests.run(ex(S.TEST_FIX, 1))) == [ex(S.REVIEW, 2)]


def test_supervisor宛の指摘だけなら回答の後に次のラウンドの頭が頼まれる(reviewing: TaskLoop):
    task = reviewing
    route_round(task, R3=T.SUPERVISOR)
    (escalation,) = task.escalation_ids()
    events = task.resolve(escalation)
    assert requested(events) == [ex(S.REVIEW, 2)]
    assert S.TEST_FIX not in {e.execution.stage for e in of_type(task.events, StageRequested)}
    assert S.FIX not in {e.execution.stage for e in of_type(task.events, StageRequested)}


def test_supervisorとtestsとimplが揃えば回答の後にTestFixとFixがこの順に走る(
    reviewing: TaskLoop,
):
    task = reviewing
    route_round(task, R1=T.TESTS, R2=T.IMPL, R3=T.SUPERVISOR)
    (escalation,) = task.escalation_ids()
    assert requested(task.resolve(escalation)) == [ex(S.TEST_FIX, 1)]
    assert requested(task.run(ex(S.TEST_FIX, 1))) == [ex(S.FIX, 1)]
    assert requested(task.run(ex(S.FIX, 1))) == [ex(S.REVIEW, 2)]


def test_supervisor_findingを受けるのはタスク統括で実装タスクの中だけで上がる():
    route = EscalationRouter.route(StreamId.task(T1), E.SUPERVISOR_FINDING)
    assert route.level is SupervisorLevel.TASK
    assert EscalationRouter.why_not_raise(TaskKind.IMPLEMENTATION, E.SUPERVISOR_FINDING) is None
    assert EscalationRouter.why_not_raise(TaskKind.PLANNING, E.SUPERVISOR_FINDING) is not None


def test_宛先を持つ直す役は宣言どおりで設計の直す役は宛先を持たない():
    fix, test_fix = STAGE_SPECS[S.FIX], STAGE_SPECS[S.TEST_FIX]
    assert (fix.fixes, test_fix.fixes) == (T.IMPL, T.TESTS)
    assert STAGE_SPECS[S.REVISE].fixes is None
    assert test_fix.guard is not None and test_fix.guard.writes is WriteScope.TESTS_ONLY
    assert (test_fix.session, test_fix.follows) == (SessionScope.FOLLOWS, (S.TEST_GEN,))
    assert (test_fix.parent, test_fix.role) == (S.REVIEW_LOOP, InnerRole.FIXER)
    assert test_fix.checks_after
    assert test_fix.reports == {E.DESIGN_GAP, E.QUICK_CHECK_FAILED}
    assert STAGE_SPECS[S.REVIEW_LOOP].inner[-2:] == (S.TEST_FIX, S.FIX)


def test_fixersの欄が無い古いRoundConcludedを読み直しても落ちず直す役の後は次のラウンドの頭へ進む(
    reviewing: TaskLoop,
):
    task = reviewing
    route_round(task, R2=T.IMPL)
    old = [
        (dataclasses.replace(e, fixers=()) if isinstance(e, RoundConcluded) else e, command)
        for e, command in task.history
    ]
    replayed = TaskLoop(T1)
    replayed.aggregate = Task.replay(task.aggregate.stream, old)
    assert replayed.aggregate.cursor == Cursor(1).enter(S.FIX, 1)
    assert requested(replayed.run(ex(S.FIX, 1))) == [ex(S.REVIEW, 2)]


def test_Gateの指摘の宛先を渡してもG_の指摘はFixへ戻る(gate_failed: TaskLoop):
    events = conclude_gate(gate_failed, ex(S.GATE), "G-verify", routes={"G-verify": T.IMPL})
    assert requested(events) == [ex(S.FIX, 1)]
    assert of_type(events, RoundConcluded)[0].fixers == (S.FIX,)
    assert requested(gate_failed.run(ex(S.FIX, 1))) == [ex(S.REVIEW, 2)]


def test_判定を待っていないときと別のJudgeの判定は受けない(reviewing: TaskLoop):
    other = TaskLoop(T1)
    other.open()
    other.flow(*IMPL_FLOW)
    with pytest.raises(Rejected, match="ReviewLoop の判定を待っていない"):
        other.conclude(ex(S.JUDGE, 1))
    with pytest.raises(Rejected, match="今のラウンドの判定の実行"):
        reviewing.conclude(ex(S.JUDGE, 2))
    with pytest.raises(Rejected, match="open の指摘のうちから"):
        reviewing.conclude(ex(S.JUDGE, 1), "R1", stalled=("R9",))
    with pytest.raises(Rejected, match="DesignLoop の判定を待っていない"):
        reviewing(
            ConcludeDesignRound(command_id=new_id(), issuer=POLICY, task=T1, judge=ex(S.JUDGE, 1))
        )


def with_testgen() -> TaskLoop:
    task = TaskLoop(T1)
    task.open()
    task.flow(FlowStep(S.TEST_GEN), FlowStep(S.CONFIRM_RED), *IMPL_FLOW)
    return task


FAILING = (VerifyResult(VerifyCommand("pytest"), 1),)


def test_Expectは期待値待ちのテストがある間だけラウンドの頭で走り書いたら外す():
    task = with_testgen()
    # TestGen が期待値を空にしたテストを報告した
    task.run(ex(S.TEST_GEN), products=(A.TESTS, A.AWAITING_EXPECTATIONS))
    task.run(ex(S.CONFIRM_RED), verify=FAILING)
    assert requested(task.run(ex(S.IMPL))) == [ex(S.EXPECT, 1)]
    events = task.run(ex(S.EXPECT, 1), products=(A.TESTS,))
    assert of_type(events, StageCompleted)[0].removed == (A.AWAITING_EXPECTATIONS,)
    assert A.AWAITING_EXPECTATIONS not in task.aggregate.artifacts
    assert requested(events) == [ex(S.REVIEW, 1), ex(S.ADVERSARIAL_REVIEW, 1)]
    task.run(ex(S.REVIEW, 1))
    task.run(ex(S.ADVERSARIAL_REVIEW, 1))
    task.run(ex(S.JUDGE, 1))
    task.conclude(ex(S.JUDGE, 1), "R1")
    # 期待値を書き終えたので、次のラウンドの頭に Expect を置かない
    assert requested(task.run(ex(S.FIX, 1))) == [ex(S.REVIEW, 2)]


def test_Expectが書き残したテストがあれば期待値待ちは残り食い違う出力は指摘として渡す():
    task = with_testgen()
    task.run(ex(S.TEST_GEN), products=(A.TESTS, A.AWAITING_EXPECTATIONS))
    task.run(ex(S.CONFIRM_RED), verify=FAILING)
    task.run(ex(S.IMPL))
    defect = {"test": "tests/test_a.py::test_log", "output": "3 件", "acceptance": "2 件を返す"}
    left = [{"test": "tests/test_a.py::test_dump", "command": "pytest -k dump"}]
    events = task.run(
        ex(S.EXPECT, 1),
        products=(A.TESTS, A.AWAITING_EXPECTATIONS),
        result={"defects": [defect], "awaitingExpectations": left},
        confirm=False,
    )
    (expected,) = of_type(events, StageCompleted)
    # まだ書いていないテストがあるので、期待値待ちは外さない（次のラウンドの頭でまた走る）
    assert expected.removed == ()
    assert A.AWAITING_EXPECTATIONS in task.aggregate.artifacts
    # 受入条件と食い違う出力は、Expect を出どころとする must-fix の指摘として台帳へ渡す
    assert expected.handoff is Handoff.FINDINGS
    assert expected.result is not None
    (finding,) = expected.result.findings
    assert finding.rating is Rating.MUST_FIX
    assert finding.location == Location("tests/test_a.py::test_log")
    assert requested(task.confirm(ex(S.EXPECT, 1))) == [
        ex(S.REVIEW, 1),
        ex(S.ADVERSARIAL_REVIEW, 1),
    ]


def test_変えないと返したステージは前に作った成果物を使って実物なしに終える():
    task = with_testgen()
    # まだ tests を作っていないので、変えないとは返せない
    events = task.run(ex(S.TEST_GEN), products=(), result={"unchanged": True})
    assert names(events)[0] == "StageFailed"
    task.run(ex(S.TEST_GEN, attempt=2))
    task.run(ex(S.CONFIRM_RED), verify=FAILING)
    events = task.run(ex(S.IMPL), result={"report": "test-conflict", "reportReason": "仕様と違う"})
    (escalation,) = task.escalation_ids()
    # 統括が TestGen に確かめさせ、テストが正しいと決めて何も作らずに終えた
    steps = (
        FlowStep(S.TEST_GEN, instruction="test-conflict を確かめる。正しければ変えない"),
        FlowStep(S.CONFIRM_RED),
        *IMPL_FLOW,
    )
    task.flow(*steps, responds_to=escalation)
    events = task.run(ex(S.TEST_GEN, attempt=3), products=(), result={"unchanged": True})
    (kept,) = of_type(events, StageCompleted)
    assert kept.produced == ()
    assert kept.result is not None and kept.result.unchanged
    assert requested(events) == [ex(S.CONFIRM_RED, attempt=2)]
    assert task.aggregate.artifacts[A.TESTS] == ArtifactRef(A.TESTS, "x")


def unchanged_evidence(sha: str, dirty: tuple[str, ...] | None = ()) -> dict:
    return {"products": (), "head": CommitSha(sha * 40), "dirty": dirty}


def test_初めてのImplが変えないと返したら汚れていないHEADを実物にして完了する(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    events = task.run(ex(S.IMPL), result={"unchanged": True}, **unchanged_evidence("a"))
    (kept,) = of_type(events, StageCompleted)
    assert kept.produced == (ArtifactRef(A.IMPL, "a" * 40),)
    assert task.aggregate.artifacts[A.IMPL] == ArtifactRef(A.IMPL, "a" * 40)
    assert requested(events) == [ex(S.REVIEW, 1), ex(S.ADVERSARIAL_REVIEW, 1)]


def test_報告の前にコミットしていたImplの後の変えないは完了する():
    task = with_testgen()
    task.run(ex(S.TEST_GEN))
    task.run(ex(S.CONFIRM_RED), verify=FAILING)
    task.run(ex(S.IMPL), result={"report": "test-conflict", "reportReason": "仕様と違う"})
    assert A.IMPL not in task.aggregate.artifacts
    (escalation,) = task.escalation_ids()
    task.flow(
        FlowStep(S.TEST_GEN, instruction="直す"),
        FlowStep(S.CONFIRM_RED),
        *IMPL_FLOW,
        responds_to=escalation,
    )
    task.run(ex(S.TEST_GEN, attempt=2))
    task.run(ex(S.CONFIRM_RED, attempt=2), verify=FAILING)
    events = task.run(ex(S.IMPL, attempt=2), result={"unchanged": True}, **unchanged_evidence("b"))
    assert of_type(events, StageCompleted)
    assert task.aggregate.artifacts[A.IMPL] == ArtifactRef(A.IMPL, "b" * 40)


def test_テストだけのタスクで初めてのImplが変えないと返しても失敗にしない():
    task = with_testgen()
    task.run(ex(S.TEST_GEN))
    task.run(ex(S.CONFIRM_RED), verify=FAILING)
    events = task.run(ex(S.IMPL), result={"unchanged": True}, **unchanged_evidence("c"))
    assert not of_type(events, StageFailed)
    assert of_type(events, StageCompleted)


def test_初めてのTestGenが変えないと返したら汚れていないHEADをtestsの実物にする():
    task = with_testgen()
    events = task.run(ex(S.TEST_GEN), result={"unchanged": True}, **unchanged_evidence("d"))
    assert of_type(events, StageCompleted)
    assert task.aggregate.artifacts[A.TESTS] == ArtifactRef(A.TESTS, "d" * 40)


def test_作業ツリーが汚れていたら変えないと返したImplは失敗にしてファイル名を書く(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    events = task.run(
        ex(S.IMPL), result={"unchanged": True}, **unchanged_evidence("d", ("src/a.py",))
    )
    (failed,) = of_type(events, StageFailed)
    assert "impl の実物が無い" in failed.reason and "src/a.py" in failed.reason
    assert A.IMPL not in task.aggregate.artifacts


def test_汚れを集めていなければ未記録の成果物で変えないと返すと失敗にする(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    events = task.run(ex(S.IMPL), result={"unchanged": True}, **unchanged_evidence("d", None))
    (failed,) = of_type(events, StageFailed)
    assert "の実物が無い" in failed.reason


def test_前に記録した成果物があれば変えないと返しても記録を変えない():
    task = with_testgen()
    events = task.run(ex(S.TEST_GEN))
    assert task.aggregate.artifacts[A.TESTS] == ArtifactRef(A.TESTS, "x")
    task.run(ex(S.CONFIRM_RED), verify=FAILING)
    task.run(ex(S.IMPL), result={"report": "test-conflict", "reportReason": "仕様と違う"})
    (escalation,) = task.escalation_ids()
    task.flow(FlowStep(S.TEST_GEN), FlowStep(S.CONFIRM_RED), *IMPL_FLOW, responds_to=escalation)
    events = task.run(
        ex(S.TEST_GEN, attempt=2), result={"unchanged": True}, **unchanged_evidence("e")
    )
    (kept,) = of_type(events, StageCompleted)
    assert kept.produced == ()
    assert task.aggregate.artifacts[A.TESTS] == ArtifactRef(A.TESTS, "x")


def test_変えないと返したのに実物があればHEADと汚れを渡されても形の誤りにする(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    events = task.run(
        ex(S.IMPL),
        result={"unchanged": True},
        head=CommitSha("d" * 40),
        dirty=(),
    )
    (failed,) = of_type(events, StageFailed)
    assert "unchanged なのに" in failed.reason


def test_結果が宣言した欄を持たなければ形の誤りとして失敗にする(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    events = task.run(ex(S.REVIEW, 1), result={"x": 1}, exact=True)
    failed = of_type(events, StageFailed)[0]
    assert "結果の形が違う" in failed.reason and "findings" in failed.reason


@pytest.mark.parametrize("stage", [S.REVIEW, S.ADVERSARIAL_REVIEW])
def test_指摘の宛先が列挙の外なら結果の形の誤りとして失敗にする(stage: StageKind, task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    found = {"rating": "must-fix", "body": "文書が古い", "location": "README.md", "target": "docs"}
    events = task.run(ex(stage, 1), result={"findings": [found]})
    assert names(events)[0] == "StageFailed"
    assert not of_type(events, StageCompleted)


def test_期待値待ちのテストが無ければtestsがあってもExpectを走らせない():
    task = with_testgen()
    task.run(ex(S.TEST_GEN))
    task.run(ex(S.CONFIRM_RED), verify=FAILING)
    assert requested(task.run(ex(S.IMPL))) == [ex(S.REVIEW, 1), ex(S.ADVERSARIAL_REVIEW, 1)]


def test_ConfirmRedでテストが全部通ったらred_check_failedで上げる():
    task = TaskLoop(T1)
    task.open()
    task.flow(FlowStep(S.TEST_GEN), FlowStep(S.CONFIRM_RED), *IMPL_FLOW)
    task.run(ex(S.TEST_GEN))
    events = task.run(ex(S.CONFIRM_RED), verify=(VerifyResult(VerifyCommand("pytest"), 0),))
    assert names(events) == ["StageReported", "EscalationRaised"]
    assert of_type(events, EscalationRaised)[0].kind is E.RED_CHECK_FAILED


RUFF = VerifyCommand("ruff check .")
TY = VerifyCommand("ty check")
TASK_PYTEST = VerifyCommand("pytest t")


def quick(command: VerifyCommand, code: int = 0, tail: str = "") -> VerifyResult:
    return VerifyResult(command, code, tail, kind=VerifyKind.QUICK_CHECKS)


def task_test(code: int) -> VerifyResult:
    return VerifyResult(TASK_PYTEST, code, kind=VerifyKind.TASK_TESTS)


def test_ConfirmRedはタスクのテストが落ち軽い検査が通れば完了しred_testsを作る():
    task = with_testgen()
    task.run(ex(S.TEST_GEN))
    events = task.run(ex(S.CONFIRM_RED), verify=(task_test(1), quick(RUFF, 0)))
    (done,) = of_type(events, StageCompleted)
    assert {p.kind for p in done.produced} == {A.RED_TESTS}
    assert not of_type(events, EscalationRaised)


def test_ConfirmRedは軽い検査も落ちていたらred_check_failedで上げ理由に落ちた軽い検査を入れる():
    task = with_testgen()
    task.run(ex(S.TEST_GEN))
    events = task.run(
        ex(S.CONFIRM_RED), verify=(task_test(1), quick(RUFF, 1, "E501"), quick(TY, 0))
    )
    assert names(events) == ["StageReported", "EscalationRaised"]
    (reported_event,) = of_type(events, StageReported)
    (raised,) = of_type(events, EscalationRaised)
    assert reported_event.kind is E.RED_CHECK_FAILED and raised.kind is E.RED_CHECK_FAILED
    assert "ruff check ." in reported_event.reason and "ruff check ." in raised.reason
    assert "ty check" not in raised.reason


def test_ConfirmRedはタスクのテストも軽い検査も通ればred_check_failedで上げる():
    task = with_testgen()
    task.run(ex(S.TEST_GEN))
    events = task.run(ex(S.CONFIRM_RED), verify=(task_test(0), quick(RUFF, 0)))
    assert names(events) == ["StageReported", "EscalationRaised"]
    assert of_type(events, EscalationRaised)[0].kind is E.RED_CHECK_FAILED


def test_ConfirmRedはタスクのテストが無く軽い検査が通っただけならred_check_failedで上げる():
    task = with_testgen()
    task.run(ex(S.TEST_GEN))
    events = task.run(ex(S.CONFIRM_RED), verify=(quick(RUFF, 0),))
    assert names(events) == ["StageReported", "EscalationRaised"]
    assert of_type(events, EscalationRaised)[0].kind is E.RED_CHECK_FAILED


# --- 軽い検査の差し戻し（TestGen） ---


def test_TestGenの後の軽い検査が始める前は通っていたのに落ちたら同じ位置を差し戻す():
    task = with_testgen()
    events = task.run(
        ex(S.TEST_GEN),
        verify=(quick(RUFF, 1, "E501 line too long"),),
        baseline=(quick(RUFF, 0),),
    )
    assert names(events)[0] == "StageSentBack"
    assert not of_type(events, EscalationRaised) and not of_type(events, StageCompleted)
    (sent,) = of_type(events, StageSentBack)
    assert sent.execution == ex(S.TEST_GEN)
    assert requested(events) == [ex(S.TEST_GEN, attempt=2)]
    assert A.TESTS not in task.aggregate.artifacts
    assert task.aggregate.executions[ex(S.TEST_GEN)].status is X.SENT_BACK
    assert not task.escalation_ids()


def test_差し戻しの理由には落ちた軽い検査のコマンドと出力の末尾が入る():
    task = with_testgen()
    events = task.run(
        ex(S.TEST_GEN),
        verify=(quick(RUFF, 1, "tests/test_a.py:3:1 F401 unused import"), quick(TY, 0)),
        baseline=(quick(RUFF, 0), quick(TY, 0)),
    )
    (sent,) = of_type(events, StageSentBack)
    assert "ruff check ." in sent.reason
    assert "tests/test_a.py:3:1 F401 unused import" in sent.reason
    assert "ty check" not in sent.reason


def test_始める前から落ちていた軽い検査は差し戻さず1回目からquick_check_failedで上げる():
    task = with_testgen()
    events = task.run(
        ex(S.TEST_GEN),
        verify=(quick(RUFF, 1, "E501"),),
        baseline=(quick(RUFF, 1, "E501"),),
    )
    assert names(events) == ["StageReported", "EscalationRaised"]
    (reported_event,) = of_type(events, StageReported)
    (raised,) = of_type(events, EscalationRaised)
    assert reported_event.kind is E.QUICK_CHECK_FAILED and raised.kind is E.QUICK_CHECK_FAILED
    assert "始める前から落ちていた" in raised.reason and "ruff check ." in raised.reason
    assert not of_type(events, StageSentBack)
    assert A.TESTS not in task.aggregate.artifacts


def test_始める前から落ちていたコマンドが1つでもあれば新しく落ちたコマンドがあっても上げる():
    task = with_testgen()
    events = task.run(
        ex(S.TEST_GEN),
        verify=(quick(RUFF, 1), quick(TY, 1)),
        baseline=(quick(RUFF, 0), quick(TY, 1)),
    )
    assert names(events) == ["StageReported", "EscalationRaised"]
    (raised,) = of_type(events, EscalationRaised)
    assert raised.kind is E.QUICK_CHECK_FAILED and "ty check" in raised.reason


def test_始める前の結果が取れていなければ通っていたとみなして差し戻す():
    task = with_testgen()
    events = task.run(ex(S.TEST_GEN), verify=(quick(RUFF, 1),), baseline=None)
    assert names(events)[0] == "StageSentBack"
    assert requested(events) == [ex(S.TEST_GEN, attempt=2)]


def fail_quick_check(task: TaskLoop, attempt: int) -> list:
    """最初の試みの baseline（通っていた）と、終わった後に落ちた軽い検査を渡す。"""
    return task.run(
        ex(S.TEST_GEN, attempt=attempt),
        verify=(quick(RUFF, 1, f"落ちた {attempt}"),),
        baseline=(quick(RUFF, 0),),
    )


def test_差し戻した後の試みも最初の試みのbaselineで比べ2回目は上げずに差し戻す():
    task = with_testgen()
    assert names(fail_quick_check(task, 1))[0] == "StageSentBack"
    events = fail_quick_check(task, 2)
    assert names(events)[0] == "StageSentBack"
    assert not of_type(events, EscalationRaised)
    assert requested(events) == [ex(S.TEST_GEN, attempt=3)]
    assert "落ちた 2" in of_type(events, StageSentBack)[0].reason


def test_同じ位置で3回続けて落ちたら3回目は差し戻さずquick_check_failedで上げる():
    task = with_testgen()
    fail_quick_check(task, 1)
    fail_quick_check(task, 2)
    events = fail_quick_check(task, 3)
    assert names(events) == ["StageReported", "EscalationRaised"]
    assert of_type(events, EscalationRaised)[0].kind is E.QUICK_CHECK_FAILED
    assert not of_type(events, StageSentBack)


def test_差し戻した後の試みで軽い検査が通れば完了して次の段へ進む():
    task = with_testgen()
    fail_quick_check(task, 1)
    events = task.run(
        ex(S.TEST_GEN, attempt=2), verify=(quick(RUFF, 0),), baseline=(quick(RUFF, 0),)
    )
    (done,) = of_type(events, StageCompleted)
    assert {p.kind for p in done.produced} == {A.TESTS}
    assert requested(events) == [ex(S.CONFIRM_RED)]
    assert A.TESTS in task.aggregate.artifacts


def test_差し戻しの回数はフローを書き直すと1から数え直す():
    task = with_testgen()
    fail_quick_check(task, 1)
    fail_quick_check(task, 2)
    fail_quick_check(task, 3)
    (escalation,) = task.escalation_ids()
    task.flow(FlowStep(S.TEST_GEN), FlowStep(S.CONFIRM_RED), *IMPL_FLOW, responds_to=escalation)
    events = fail_quick_check(task, 4)
    assert names(events)[0] == "StageSentBack"
    assert not of_type(events, EscalationRaised)
    assert requested(events) == [ex(S.TEST_GEN, attempt=5)]


def test_差し戻しは失敗に数えずstage_errorsの数え方と混ぜない():
    task = with_testgen()
    fail_quick_check(task, 1)
    fail_quick_check(task, 2)
    events = task.run(ex(S.TEST_GEN, attempt=3), exit=StageExit.ERROR, error="claude が落ちた")
    assert names(events)[0] == "StageFailed"
    assert not of_type(events, EscalationRaised)


def test_軽い検査が0件ならTestGenは今どおり完了する():
    task = with_testgen()
    events = task.run(ex(S.TEST_GEN), verify=(), baseline=())
    (done,) = of_type(events, StageCompleted)
    assert {p.kind for p in done.produced} == {A.TESTS}
    assert not of_type(events, StageSentBack)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("quick-check-failed", E.QUICK_CHECK_FAILED), ("design-gap", E.DESIGN_GAP)],
)
def test_TestGenが報告を返したら軽い検査の結果を見ずに報告で上げる(kind: str, expected: E):
    task = with_testgen()
    events = task.run(
        ex(S.TEST_GEN),
        products=(),
        result=reported(kind, "テストの外のファイルで落ちる"),
        verify=(quick(RUFF, 1),),
        baseline=(quick(RUFF, 0),),
    )
    assert names(events) == ["StageReported", "EscalationRaised"]
    (raised,) = of_type(events, EscalationRaised)
    assert raised.kind is expected and raised.reason == "テストの外のファイルで落ちる"
    assert not of_type(events, StageSentBack)


def test_軽い検査を前後に流さないLLMのステージは軽い検査が落ちても完了する(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    events = task.run(ex(S.IMPL), verify=(quick(RUFF, 1),), baseline=(quick(RUFF, 0),))
    assert of_type(events, StageCompleted) and not of_type(events, StageSentBack)


# --- baseline_of ---


def test_baseline_ofは同じ位置の最初の試みを返す():
    task = with_testgen()
    fail_quick_check(task, 1)
    assert task.aggregate.baseline_of(ex(S.TEST_GEN)) == ex(S.TEST_GEN)
    assert task.aggregate.baseline_of(ex(S.TEST_GEN, attempt=2)) == ex(S.TEST_GEN)
    with pytest.raises(KeyError):
        task.aggregate.baseline_of(ex(S.CONFIRM_RED))


def test_baseline_ofは失敗のやり直しでも最初の試みを返す():
    task = with_testgen()
    task.run(ex(S.TEST_GEN), exit=StageExit.ERROR, error="claude が落ちた")
    assert task.aggregate.baseline_of(ex(S.TEST_GEN, attempt=2)) == ex(S.TEST_GEN)


def test_baseline_ofはフローを書き直した後の最初の試みにはその試み自身を返す():
    task = with_testgen()
    fail_quick_check(task, 1)
    fail_quick_check(task, 2)
    fail_quick_check(task, 3)
    (escalation,) = task.escalation_ids()
    task.flow(FlowStep(S.TEST_GEN), FlowStep(S.CONFIRM_RED), *IMPL_FLOW, responds_to=escalation)
    assert task.aggregate.baseline_of(ex(S.TEST_GEN, attempt=4)) == ex(S.TEST_GEN, attempt=4)
    fail_quick_check(task, 4)
    assert task.aggregate.baseline_of(ex(S.TEST_GEN, attempt=5)) == ex(S.TEST_GEN, attempt=4)


# --- 軽い検査の差し戻しと baseline（TestFix） ---


def fixing_task() -> TaskLoop:
    """1 ラウンド目の判定が R1（tests）を open のまま残し、TestFix を待っているタスク。"""
    task = TaskLoop(T1)
    task.open()
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    for stage in (S.REVIEW, S.ADVERSARIAL_REVIEW, S.JUDGE):
        task.run(ex(stage, 1))
    route_round(task, R1=T.TESTS)
    return task


def fail_test_fix(task: TaskLoop, round: int, attempt: int, baseline: int = 0) -> list:
    """そのラウンドの最初の試みの baseline（`baseline` は終了コード）と、終わった後に落ちた軽い検査を渡す。"""
    return task.run(
        ex(S.TEST_FIX, round, attempt),
        verify=(quick(RUFF, 1, f"落ちた {round}-{attempt}"),),
        baseline=(quick(RUFF, baseline),),
    )


def test_TestFixの後の軽い検査が始める前は通っていたのに落ちたら同じ位置を差し戻す():
    task = fixing_task()
    events = fail_test_fix(task, 1, 1)
    assert names(events)[0] == "StageSentBack"
    (sent,) = of_type(events, StageSentBack)
    assert sent.execution == ex(S.TEST_FIX, 1)
    assert "落ちた 1-1" in sent.reason and "ruff check" in sent.reason
    assert requested(events) == [ex(S.TEST_FIX, 1, attempt=2)]
    assert not of_type(events, EscalationRaised) and not of_type(events, StageCompleted)
    assert task.aggregate.executions[ex(S.TEST_FIX, 1)].status is X.SENT_BACK
    assert not task.escalation_ids()


def test_TestFixの差し戻した後の試みも同じbaselineで比べ3回目はquick_check_failedで上げる():
    task = fixing_task()
    assert names(fail_test_fix(task, 1, 1))[0] == "StageSentBack"
    events = fail_test_fix(task, 1, 2)
    assert names(events)[0] == "StageSentBack"
    assert requested(events) == [ex(S.TEST_FIX, 1, attempt=3)]
    events = fail_test_fix(task, 1, 3)
    assert names(events) == ["StageReported", "EscalationRaised"]
    assert of_type(events, EscalationRaised)[0].kind is E.QUICK_CHECK_FAILED
    assert not of_type(events, StageSentBack)


def test_TestFixの差し戻した後の試みで軽い検査が通れば完了して次のラウンドの頭へ進む():
    task = fixing_task()
    fail_test_fix(task, 1, 1)
    events = task.run(ex(S.TEST_FIX, 1, 2), verify=(quick(RUFF, 0),), baseline=(quick(RUFF, 0),))
    assert of_type(events, StageCompleted)
    assert requested(events) == [ex(S.REVIEW, 2)]


def test_TestFixの軽い検査がそのラウンドの最初の試みの前から落ちていたら1回目から上げる():
    task = fixing_task()
    events = fail_test_fix(task, 1, 1, baseline=1)
    assert names(events) == ["StageReported", "EscalationRaised"]
    assert of_type(events, EscalationRaised)[0].kind is E.QUICK_CHECK_FAILED
    assert not of_type(events, StageSentBack)


def test_TestFixが報告を返したら軽い検査の結果を見ずに報告で上げる():
    task = fixing_task()
    events = task.run(
        ex(S.TEST_FIX, 1),
        result=reported("quick-check-failed", "テストの外のファイルで落ちる"),
        verify=(quick(RUFF, 1),),
        baseline=(quick(RUFF, 0),),
    )
    assert names(events) == ["StageReported", "EscalationRaised"]
    (raised,) = of_type(events, EscalationRaised)
    assert raised.kind is E.QUICK_CHECK_FAILED
    assert raised.reason == "テストの外のファイルで落ちる"
    assert not of_type(events, StageSentBack)


def test_軽い検査が0件ならTestFixは今どおり完了する():
    task = fixing_task()
    events = task.run(ex(S.TEST_FIX, 1), verify=(), baseline=())
    assert of_type(events, StageCompleted) and not of_type(events, StageSentBack)


def reach_round_2_test_fix(task: TaskLoop) -> None:
    """1 ラウンド目の TestFix を終え、2 ラウンド目の判定が R1（tests）を残して TestFix を待つところまで進める。"""
    task.run(ex(S.TEST_FIX, 1), verify=(quick(RUFF, 0),), baseline=(quick(RUFF, 0),))
    task.run(ex(S.REVIEW, 2))
    task.run(ex(S.JUDGE, 2))
    assert requested(route_round(task, 2, R1=T.TESTS)) == [ex(S.TEST_FIX, 2)]


def test_2ラウンド目のTestFixの最初の試みは1ラウンド目のbaselineを使わず新しく取る():
    task = fixing_task()
    reach_round_2_test_fix(task)
    assert task.aggregate.baseline_of(ex(S.TEST_FIX, 2)) == ex(S.TEST_FIX, 2)
    assert task.aggregate.baseline_of(ex(S.TEST_FIX, 1)) == ex(S.TEST_FIX, 1)
    fail_test_fix(task, 2, 1)
    assert task.aggregate.baseline_of(ex(S.TEST_FIX, 2, 2)) == ex(S.TEST_FIX, 2)


def test_ラウンドが替われば差し戻しの回数も1から数え直し新しいbaselineで比べる():
    task = fixing_task()
    fail_test_fix(task, 1, 1)
    fail_test_fix(task, 1, 2)
    task.run(ex(S.TEST_FIX, 1, 3), verify=(quick(RUFF, 0),), baseline=(quick(RUFF, 0),))
    task.run(ex(S.REVIEW, 2))
    task.run(ex(S.JUDGE, 2))
    route_round(task, 2, R1=T.TESTS)
    assert names(fail_test_fix(task, 2, 1))[0] == "StageSentBack"
    assert names(fail_test_fix(task, 2, 2))[0] == "StageSentBack"
    assert names(fail_test_fix(task, 2, 3)) == ["StageReported", "EscalationRaised"]


def test_2ラウンド目の最初の試みの前から落ちていたら1ラウンド目で通っていても上げる():
    task = fixing_task()
    reach_round_2_test_fix(task)
    events = fail_test_fix(task, 2, 1, baseline=1)
    assert names(events) == ["StageReported", "EscalationRaised"]
    assert not of_type(events, StageSentBack)


def test_TestFixがcommitしたらtestsの成果物がそのコミットに替わる():
    task = TaskLoop(T1)
    task.open()
    task.flow(FlowStep(S.TEST_GEN), FlowStep(S.CONFIRM_RED), *IMPL_FLOW)
    task.run(ex(S.TEST_GEN))
    task.run(ex(S.CONFIRM_RED), verify=FAILING)
    task.run(ex(S.IMPL))
    task.run(ex(S.REVIEW, 1))
    task.run(ex(S.ADVERSARIAL_REVIEW, 1))
    task.run(ex(S.JUDGE, 1))
    route_round(task, R1=T.TESTS)
    before = task.aggregate.artifacts[A.TESTS]
    commit = ArtifactRef(A.TESTS, "b" * 40)
    task.begin(ex(S.TEST_FIX, 1))
    task(
        ReportStageResult(
            command_id=new_id(),
            issuer=Issuer.executor(ex(S.TEST_FIX, 1)),
            task=T1,
            execution=ex(S.TEST_FIX, 1),
            evidence=Evidence(products=(commit,), exit=StageExit.OK, result_valid=True, commits=1),
            pointers=Pointers(),
            result=stage_result(S.TEST_FIX),
        )
    )
    assert task.aggregate.artifacts[A.TESTS] == commit != before


def test_TestFixがcommitしなければtestsの成果物はそのまま():
    task = TaskLoop(T1)
    task.open()
    task.flow(FlowStep(S.TEST_GEN), FlowStep(S.CONFIRM_RED), *IMPL_FLOW)
    task.run(ex(S.TEST_GEN))
    task.run(ex(S.CONFIRM_RED), verify=FAILING)
    task.run(ex(S.IMPL))
    task.run(ex(S.REVIEW, 1))
    task.run(ex(S.ADVERSARIAL_REVIEW, 1))
    task.run(ex(S.JUDGE, 1))
    route_round(task, R1=T.TESTS)
    before = task.aggregate.artifacts[A.TESTS]
    task.run(ex(S.TEST_FIX, 1), products=())
    assert task.aggregate.artifacts[A.TESTS] == before


# --- checks_after の宣言 ---


@pytest.mark.parametrize(
    "stage", [S.IMPL, S.REVIEW, S.GATE, S.CONFIRM_RED], ids=lambda stage: stage.value
)
def test_checks_afterは検証コマンドの行があるLLMのステージだけが持てる(stage: StageKind):
    with pytest.raises(ValueError):
        dataclasses.replace(STAGE_SPECS[stage], checks_after=True)


def test_checks_afterはTestGenに付けられる():
    assert dataclasses.replace(STAGE_SPECS[S.TEST_GEN], checks_after=True).checks_after


def test_ラウンドがr1からやり直しても実行のidを使い回さない(reviewing: TaskLoop):
    task = reviewing
    task.conclude(ex(S.JUDGE, 1), "R1", stalled=("R1",))
    (escalation,) = task.escalation_ids()
    # 統括がフローを書き直すと ReviewLoop は 1 ラウンド目からになる
    task.flow(*IMPL_FLOW[1:], responds_to=escalation)
    assert requested(task.events[-2:]) == [
        ex(S.REVIEW, 1, attempt=2),
        ex(S.ADVERSARIAL_REVIEW, 1, attempt=2),
    ]
    ids = [event.execution for event in of_type(task.events, StageRequested)]
    assert len(ids) == len(set(ids))


# --- Gate ---


def conclude_gate(
    task: TaskLoop,
    gate_run: ExecutionId,
    *unresolved: str,
    stalled=(),
    routes: dict[str, FindingTarget] | None = None,
) -> list:
    return task(
        ConcludeGateRound(
            command_id=new_id(),
            issuer=POLICY,
            task=task.task,
            gate=gate_run,
            unresolved=tuple(FindingId(f) for f in unresolved),
            stalled=tuple(FindingId(f) for f in stalled),
            routes=tuple(FindingRoute(FindingId(f), t) for f, t in (routes or {}).items()),
        )
    )


@pytest.fixture
def gate_failed(reviewing: TaskLoop) -> TaskLoop:
    reviewing.conclude(ex(S.JUDGE, 1))
    reviewing.run(ex(S.GATE), products=(), gate=gate(GateItem.VERIFY))
    return reviewing


def test_Gateの直せる落ちはGateFailedで直前のReviewLoopのFixへ戻り判定を待つ(reviewing: TaskLoop):
    task = reviewing
    task.conclude(ex(S.JUDGE, 1))
    events = task.run(ex(S.GATE), products=(), gate=gate(GateItem.VERIFY))
    # G- の指摘が判定されるまで、Fix を始めない
    assert names(events) == ["GateFailed"]
    failed = events[0]
    assert isinstance(failed, GateFailed)
    # Gate の実行の id が残る（G- の指摘の状態を動かせるのは Gate の実行の結果だけ）
    assert failed.execution == ex(S.GATE)
    assert failed.cursor == Cursor(1).enter(S.FIX, 1)
    assert task.aggregate.executions[ex(S.GATE)].status is X.COMPLETED


def test_G_の指摘が残ればFixへ戻り抜けたらGateをもう一度(gate_failed: TaskLoop):
    task = gate_failed
    with pytest.raises(Rejected, match="落ちて判定を待っている Gate の実行ではない"):
        conclude_gate(task, ex(S.JUDGE, 1), "G-verify")
    events = conclude_gate(task, ex(S.GATE), "G-verify")
    assert requested(events) == [ex(S.FIX, 1)]
    # Fix の後は次のラウンドから回り、抜けたら Gate をもう一度
    task.run(ex(S.FIX, 1))
    task.run(ex(S.REVIEW, 2))
    task.run(ex(S.JUDGE, 2))
    assert requested(task.conclude(ex(S.JUDGE, 2))) == [ex(S.GATE, attempt=2)]
    # Gate が通った後の判定の知らせ（RecordGateResult は通ったときも出す）は何もしない
    task.run(ex(S.GATE, attempt=2), gate=PASSED)
    assert conclude_gate(task, ex(S.GATE, attempt=2)) == []


def test_停滞したG_の指摘はここで上げ解けたらFixへ(gate_failed: TaskLoop):
    task = gate_failed
    events = conclude_gate(task, ex(S.GATE), "G-verify", stalled=("G-verify",))
    assert names(events) == ["RoundConcluded", "EscalationRaised"]
    raised = of_type(events, EscalationRaised)[0]
    assert raised.kind is E.STALL
    assert raised.origin == ex(S.GATE)
    (escalation,) = task.escalation_ids()
    assert requested(task.resolve(escalation)) == [ex(S.FIX, 1)]


def test_残ったG_の指摘が無ければGateをもう一度走らせる(gate_failed: TaskLoop):
    assert requested(conclude_gate(gate_failed, ex(S.GATE))) == [ex(S.GATE, attempt=2)]


def test_Gateのコードで直せない落ちは上げる(reviewing: TaskLoop):
    task = reviewing
    task.conclude(ex(S.JUDGE, 1))
    events = task.run(ex(S.GATE), products=(), gate=gate(GateItem.COMMITS, GateItem.VERIFY))
    assert names(events) == ["StageReported", "EscalationRaised"]
    raised = of_type(events, EscalationRaised)[0]
    assert raised.kind is E.GATE_UNFIXABLE
    assert raised.hint.gate_items == (GateItem.COMMITS,)
    # 解けたら Gate をもう一度走らせる。テスト作成を抜いたのに範囲の外を変えたなら untested-change
    (escalation,) = task.escalation_ids()
    assert requested(task.resolve(escalation)) == [ex(S.GATE, attempt=2)]
    events = task.run(ex(S.GATE, attempt=2), products=(), gate=gate(GateItem.UNTESTED_PATHS))
    assert of_type(events, EscalationRaised)[0].kind is E.UNTESTED_CHANGE


def test_Gateの結果が無ければ失敗にする(reviewing: TaskLoop):
    reviewing.conclude(ex(S.JUDGE, 1))
    assert names(reviewing.run(ex(S.GATE)))[0] == "StageFailed"


# --- エスカレーションと回答 ---


def test_回答は出どころ付きでnotesに残る(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL), result=reported("test-conflict"))
    (first,) = task.escalation_ids()
    events = task.resolve(first, question="q-a")
    assert of_type(events, NoteAdded) == [
        NoteAdded(Decision("こうする", DecisionOrigin.USER, QuestionId("q-a")))
    ]
    task.run(ex(S.IMPL, attempt=2), result=reported("test-conflict"))
    (second,) = task.escalation_ids()
    task.resolve(second)
    assert [note.origin for note in task.aggregate.notes] == [
        DecisionOrigin.USER,
        DecisionOrigin.RUN_SUPERVISOR,
    ]
    with pytest.raises(Rejected, match="未処理のエスカレーションではない"):
        task.resolve(second)


def test_タスクの中で上げてよい種類だけを上げる(task: TaskLoop):
    def escalate(kind: EscalationKind) -> list:
        return task(
            Escalate(command_id=new_id(), issuer=POLICY, task=T1, kind=kind, pointers=Pointers())
        )

    assert names(escalate(E.STALL)) == ["EscalationRaised"]
    with pytest.raises(Rejected, match="implementation のタスクの中では ask を上げない"):
        escalate(E.ASK)


def test_askの回答が届くまで止まった実行は再開せず閉じたら捨てる():
    planning = TaskLoop(TaskId.planning())
    planning.open(artifacts=())
    planning.flow(FlowStep(S.PREPARE), FlowStep(S.PLAN), FlowStep(S.DESIGN_LOOP))
    planning.run(ex(S.PREPARE, task=TaskId.planning()))
    plan = ex(S.PLAN, task=TaskId.planning())
    planning.begin(plan)
    planning.report(plan, products=(), deferred=DeferredCall("toolu_1", "?"))
    (escalation,) = planning.escalation_ids()

    def resume() -> list:
        return planning(
            ResumeStage(command_id=new_id(), issuer=CLI, task=TaskId.planning(), execution=plan)
        )

    with pytest.raises(Rejected, match="回答がまだ届いていない"):
        resume()
    # 回答が届いても、新しい実行は作らない（反応が回答のファイルを書いてから ResumeStage を渡す）
    events = planning.resolve(escalation)
    assert of_type(events, StageRequested) == []
    # 続きから再開する実行と、回答を書くファイルの名前（tool_use_id）は Task が決めて載せる
    assert of_type(events, EscalationResolved) == [
        EscalationResolved(escalation, "こうする", E.ASK, plan, resume=plan, tool_use_id="toolu_1")
    ]
    # 同じセッションを --resume で続ける
    assert resume() == [StageStarted(plan, HEAD, SESSION)]


def test_回答には答えたエスカレーションの種類と起きた実行を載せる(reviewing: TaskLoop):
    task = reviewing
    task.conclude(ex(S.JUDGE, 1), "R1", stalled=("R1",))
    (escalation,) = task.escalation_ids()
    (resolved,) = of_type(task.resolve(escalation, "q-a"), EscalationResolved)
    # defer で止まった実行ではないので、再開する実行は載せない
    assert resolved == EscalationResolved(
        escalation, "こうする", E.STALL, ex(S.JUDGE, 1), QuestionId("q-a")
    )


def test_回答以外で閉じたら止まった実行はabandonedになる():
    planning = TaskLoop(TaskId.planning())
    planning.open(artifacts=())
    planning.flow(FlowStep(S.PREPARE), FlowStep(S.PLAN), FlowStep(S.DESIGN_LOOP))
    planning.run(ex(S.PREPARE, task=TaskId.planning()))
    plan = ex(S.PLAN, task=TaskId.planning())
    planning.begin(plan)
    planning.report(plan, products=(), deferred=DeferredCall("toolu_1", "?"))
    (escalation,) = planning.escalation_ids()
    planning(
        CloseEscalation(
            command_id=new_id(),
            issuer=POLICY,
            task=TaskId.planning(),
            escalation=escalation,
            reason="再計画を頼んだ",
        )
    )
    assert planning.aggregate.executions[plan].status is X.ABANDONED
    with pytest.raises(Rejected, match="interrupted・deferred でない"):
        planning(
            ResumeStage(command_id=new_id(), issuer=CLI, task=TaskId.planning(), execution=plan)
        )


# --- 中断・再開・やり直し ---


def test_起動時に走ったままの実行をinterruptedにして同じ実行を続きから再開する(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.begin(ex(S.IMPL))
    events = task(
        MarkInterrupted(command_id=new_id(), issuer=DRIVER, task=T1, execution=ex(S.IMPL))
    )
    assert names(events) == ["StageInterrupted"]
    with pytest.raises(Rejected, match="running でない"):
        task(MarkInterrupted(command_id=new_id(), issuer=DRIVER, task=T1, execution=ex(S.IMPL)))
    events = task(ResumeStage(command_id=new_id(), issuer=CLI, task=T1, execution=ex(S.IMPL)))
    assert events == [StageStarted(ex(S.IMPL), HEAD, SESSION)]


def test_runningの実行をドメインに聞ける(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    assert task.aggregate.running_executions() == []
    task.begin(ex(S.IMPL))
    assert task.aggregate.running_executions() == [ex(S.IMPL)]


def test_実行を今の版の段ごとと前の版で走っているものと段の外に分けて聞ける():
    done = ex(S.TEST_GEN)
    old = ex(S.IMPL)
    again = ex(S.TEST_GEN, attempt=2)
    placed = ex(S.IMPL, attempt=2)
    stray = ex(S.GATE)
    steps = (FlowStep(S.TEST_GEN), FlowStep(S.IMPL))
    events = [
        TaskOpened(TaskKind.IMPLEMENTATION, TaskSpec("x")),
        FlowAccepted(Flow(steps, 1)),
        StageRequested(done, 0),
        StageStarted(done, HEAD),
        StageCompleted(done, cursor=Cursor(1)),
        StageRequested(old, 1),
        StageStarted(old, HEAD),
        FlowAccepted(Flow(steps, 2)),
        StageRequested(again, 0),
        StageStarted(again, HEAD),
        StageCompleted(again, cursor=Cursor(1)),
        StageRequested(placed, 1),
        StageRequested(stray, 2),
    ]
    aggregate = Task.replay(
        StreamId.task(T1), [(e, CommandId(f"c{i}")) for i, e in enumerate(events)]
    )
    assert [e.id for e in aggregate.executions_at(0)] == [again]
    assert [e.id for e in aggregate.executions_at(1)] == [placed]
    assert [e.id for e in aggregate.earlier_executions()] == [old]
    assert [e.id for e in aggregate.unplaced_executions()] == [stray]


def interrupt(task: TaskLoop, execution: ExecutionId, cause: InterruptCause) -> list:
    return task(
        MarkInterrupted(
            command_id=new_id(), issuer=DRIVER, task=T1, execution=execution, cause=cause
        )
    )


def resume_interrupted(task: TaskLoop) -> list:
    return task(ResumeInterrupted(command_id=new_id(), issuer=POLICY, task=task.task))


@pytest.mark.parametrize("cause", [InterruptCause.STARTUP, InterruptCause.PANIC])
def test_driverの都合で止めた実行は呼び直されたら続きから再開する(
    task: TaskLoop, cause: InterruptCause
):
    task.flow(*IMPL_FLOW)
    task.begin(ex(S.IMPL))
    assert interrupt(task, ex(S.IMPL), cause) == [StageInterrupted(ex(S.IMPL), cause)]
    assert resume_interrupted(task) == [StageStarted(ex(S.IMPL), HEAD, SESSION)]
    # もう走っているので、もう一度頼まれても何もしない
    assert resume_interrupted(task) == []


def test_止めると決めて止めた実行は呼び直されても再開しない(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    task.begin(ex(S.REVIEW, 1))
    task(StopTask(command_id=new_id(), issuer=POLICY, task=T1, reason="止めた"))
    assert resume_interrupted(task) == []
    with pytest.raises(Rejected, match="driver が止める理由は startup か panic"):
        interrupt(task, ex(S.ADVERSARIAL_REVIEW, 1), InterruptCause.STOPPED)


def test_始められなかった実行は走らせて落ちたときと同じくやり直し続けたら上げる(task: TaskLoop):
    task.flow(*IMPL_FLOW)

    def fail(execution: ExecutionId) -> list:
        return task(
            ReportBeginFailure(
                command_id=new_id(),
                issuer=Issuer.executor(execution),
                task=T1,
                execution=execution,
                error="reset が落ちた",
            )
        )

    assert fail(ex(S.IMPL)) == [
        StageFailed(ex(S.IMPL), "始められなかった: reset が落ちた"),
        StageRequested(ex(S.IMPL, attempt=2), 0),
    ]
    events = fail(ex(S.IMPL, attempt=2))
    assert of_type(events, StageFailed)
    assert [e.kind for e in of_type(events, EscalationRaised)] == [E.STAGE_ERRORS]
    # 始めた実行には届かない（BeginStage と取り違えた知らせ）
    with pytest.raises(Rejected, match="requested でない"):
        fail(ex(S.IMPL, attempt=2))


def test_再開に失敗したら始めた時点のコミットから新しい実行を作る(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.begin(ex(S.IMPL))
    task(MarkInterrupted(command_id=new_id(), issuer=DRIVER, task=T1, execution=ex(S.IMPL)))
    task(ResumeStage(command_id=new_id(), issuer=CLI, task=T1, execution=ex(S.IMPL)))
    events = task.report(
        ex(S.IMPL),
        exit=StageExit.ERROR,
        result_valid=False,
        error="resume 失敗",
        **LOST_SESSION,
    )
    assert events == [
        ExecutionRestarted(ex(S.IMPL), "resume 失敗", HEAD),
        StageRequested(ex(S.IMPL, attempt=2), 0),
    ]
    with pytest.raises(Rejected, match="interrupted・deferred でない（restarted）"):
        task(ResumeStage(command_id=new_id(), issuer=CLI, task=T1, execution=ex(S.IMPL)))


@pytest.mark.parametrize(
    "facts",
    [
        # init を出した後に落ちた（続けた後で落ちた。作り直すと続けた仕事を捨てる）
        {**LOST_SESSION, "initialized": True},
        # --resume で起こしていない
        {**LOST_SESSION, "resumed": False},
        # init の前にこちらが kill した・interrupt して result が返った（セッションが在るかは
        # 分からない。作り直すと前の仕事を捨てる）
        {**LOST_SESSION, "num_turns": None, "stopped_by_us": True},
        {**LOST_SESSION, "stopped_by_us": True},
        # defer の再開でもう一度 defer した（止めた呼び出しは init より前に走る）
        {**LOST_SESSION, "deferred": DeferredCall("toolu_1", "?")},
        # result があり、ターンを進めていた
        {**LOST_SESSION, "num_turns": 1},
    ],
)
def test_続けられなかった印が揃わなければ作り直さず失敗に数える(task: TaskLoop, facts: dict):
    task.flow(*IMPL_FLOW)
    task.begin(ex(S.IMPL))
    events = task.report(
        ex(S.IMPL), exit=StageExit.ERROR, result_valid=False, error="落ちた", **facts
    )
    assert of_type(events, StageFailed) and not of_type(events, ExecutionRestarted)


def test_resultが無く自分で終わった続けられなかった証拠なら作り直す(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.begin(ex(S.IMPL))
    facts: dict[str, Any] = {**LOST_SESSION, "num_turns": None}
    events = task.report(
        ex(S.IMPL), exit=StageExit.ERROR, result_valid=False, error="落ちた", **facts
    )
    assert of_type(events, ExecutionRestarted)


# --- 範囲の変更と止める ---


def test_範囲が変わったら待っているエスカレーションを閉じ新しいフローを待つ(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    task.run(ex(S.REVIEW, 1), result={"x": 1}, result_valid=False)
    task.run(ex(S.REVIEW, 1, attempt=2), result_valid=False)  # stage-errors
    task.begin(ex(S.ADVERSARIAL_REVIEW, 1))
    (escalation,) = task.escalation_ids()
    events = task(
        ChangeScope(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            spec=TaskSpec("広げた"),
            artifacts=(ArtifactRef(A.DESIGN, "2"),),
            pointers=Pointers(),
        )
    )
    # 閉じたことはイベントで残す（Run の側の中継もこれを受けて閉じる）
    assert names(events) == ["EscalationClosed", "ScopeChanged"]
    assert events[0] == EscalationClosed(
        escalation, "範囲が変わった", kind=E.STAGE_ERRORS, origin=ex(S.REVIEW, 1, attempt=2)
    )
    assert task.escalation_ids() == []
    assert task.aggregate.flow is None
    assert task.aggregate.artifacts[A.DESIGN] == ArtifactRef(A.DESIGN, "2")
    # 統括が組み直したフローは受けるが、走っているステージが終わるまで次を始めない
    assert names(task.flow(*IMPL_FLOW[1:])) == ["FlowAccepted"]
    # 書き直す前のフローの実行は、成果物を残すだけで、上げもやり直しもしない
    events = task.report(ex(S.ADVERSARIAL_REVIEW, 1))
    assert names(events) == ["StageCompleted", "StageRequested", "StageRequested"]
    assert requested(events) == [
        ex(S.REVIEW, 1, attempt=3),
        ex(S.ADVERSARIAL_REVIEW, 1, attempt=2),
    ]


def test_範囲を変えるのは実装タスクだけ():
    git = TaskLoop(TaskId.git())
    git.open(artifacts=())
    with pytest.raises(Rejected, match="実装タスクだけ"):
        git(
            ChangeScope(
                command_id=new_id(),
                issuer=POLICY,
                task=TaskId.git(),
                spec=TaskSpec("x"),
                artifacts=(),
                pointers=Pointers(),
            )
        )


def test_積む列に入った実装タスクは範囲が変わるまでフローを受けない(reviewing: TaskLoop):
    task = reviewing
    task.conclude(ex(S.JUDGE, 1))
    task.run(ex(S.GATE), gate=PASSED)
    task.run(ex(S.WRITE_PR_BODY))
    with pytest.raises(Rejected, match="積む列に入っている"):
        task.flow(*IMPL_FLOW)


def test_止めたタスクは走っている実行を中断しエスカレーションを閉じ以後始めない(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    task.begin(ex(S.REVIEW, 1))
    task(Escalate(command_id=new_id(), issuer=POLICY, task=T1, kind=E.STALL, pointers=Pointers()))
    events = task(StopTask(command_id=new_id(), issuer=POLICY, task=T1, reason="再計画で止めた"))
    # 中断するのは走っている Review だけ。BeginStage を待つ AdversarialReview は、もう始まらない
    assert events[0] == StageInterrupted(ex(S.REVIEW, 1), InterruptCause.STOPPED)
    assert names(events) == ["StageInterrupted", "EscalationClosed", "TaskStopped"]
    with pytest.raises(Rejected, match="止めたタスク"):
        task.begin(ex(S.ADVERSARIAL_REVIEW, 1))
    with pytest.raises(Rejected, match="止めたタスク"):
        task.flow(*IMPL_FLOW)
    with pytest.raises(Rejected, match="止めたタスク"):
        task(ResumeStage(command_id=new_id(), issuer=CLI, task=T1, execution=ex(S.REVIEW, 1)))
    # もう止めた・始めていない（計画にあるだけの）タスクを止める知らせは、何もしない
    assert task(StopTask(command_id=new_id(), issuer=POLICY, task=T1, reason="x")) == []
    unopened = TaskLoop(TaskId("task7"))
    stop = StopTask(command_id=new_id(), issuer=POLICY, task=TaskId("task7"), reason="x")
    assert unopened(stop) == []


# --- 計画タスク: DesignLoop ---


def test_DesignLoopは判定の後Designの結果でReviseか抜けるかを決める():
    p = TaskId.planning()
    planning = TaskLoop(p)
    planning.open(artifacts=())
    planning.flow(FlowStep(S.PREPARE), FlowStep(S.PLAN), FlowStep(S.DESIGN_LOOP))
    assert requested(planning.run(ex(S.PREPARE, task=p))) == [ex(S.PLAN, task=p)]
    events = planning.run(ex(S.PLAN, task=p), confirm=False)
    (proposed,) = of_type(events, StageCompleted)
    # 提案は Design へ渡し、ラン共通の成果物（brief・codemap）の在りかを添える
    assert proposed.handoff is Handoff.PROPOSAL
    assert proposed.result is not None and proposed.result.proposal is not None
    assert proposed.result.proposal.design == DesignVersion(1)
    assert {a.kind for a in proposed.shared} == {A.BRIEF, A.CODEMAP}
    events = planning.confirm(ex(S.PLAN, task=p))
    assert requested(events) == [ex(S.DESIGN_REVIEW, 1, task=p)]
    assert requested(planning.run(ex(S.DESIGN_REVIEW, 1, task=p))) == [
        ex(S.DESIGN_JUDGE, 1, task=p)
    ]
    (judged,) = planning.run(ex(S.DESIGN_JUDGE, 1, task=p))
    assert isinstance(judged, StageCompleted)
    # 設計の段のステージは、見た提案の版を載せる（設計の台帳の RecordJudgement の design）
    assert (judged.handoff, judged.reviewed, judged.cursor) == (
        Handoff.JUDGEMENT,
        DesignVersion(1),
        None,
    )

    def conclude(judge: ExecutionId, settled: int | None = None) -> list:
        return planning(
            ConcludeDesignRound(
                command_id=new_id(),
                issuer=POLICY,
                task=p,
                judge=judge,
                settled=DesignVersion(settled) if settled else None,
            )
        )

    with pytest.raises(Rejected, match="今のラウンドの判定の実行"):
        conclude(ex(S.DESIGN_JUDGE, 2, task=p))
    assert requested(conclude(ex(S.DESIGN_JUDGE, 1, task=p))) == [ex(S.REVISE, 1, task=p)]
    assert requested(planning.run(ex(S.REVISE, 1, task=p))) == [ex(S.DESIGN_REVIEW, 2, task=p)]
    planning.run(ex(S.DESIGN_REVIEW, 2, task=p))
    planning.run(ex(S.DESIGN_JUDGE, 2, task=p))
    events = conclude(ex(S.DESIGN_JUDGE, 2, task=p), 2)
    # 計画タスクは TaskGated を出さない
    assert names(events) == ["RoundConcluded", "FlowFinished"]
    assert planning.aggregate.artifacts[A.DESIGN] == ArtifactRef(A.DESIGN, "2")
    # フローを終えたので、再計画のフローを受ける
    assert names(planning.flow(FlowStep(S.REPLAN), FlowStep(S.DESIGN_LOOP)))[0] == "FlowAccepted"


# --- git 管理タスク ---


def git_task() -> TaskLoop:
    git = TaskLoop(TaskId.git())
    git.open(artifacts=())
    return git


G = TaskId.git()
OVERVIEW_BRANCH = BranchName("stack/add-cache--task-0")
STACK_JOB = GitJob(3, GitJobKind.STACK, task=T1, branch=BRANCH, base=OVERVIEW_BRANCH)


def test_Rebaseが衝突しなければ衝突したときだけのステージを飛ばす():
    git = git_task()
    git.take(STACK_JOB)
    assert requested(git.run(ex(S.REBASE, task=G))) == [ex(S.INTEGRATION_CHECK, task=G)]


@pytest.mark.parametrize(
    ("commits", "reason"), [(0, "根元から上のコミットが無い"), (None, "数えられない")]
)
def test_載せ直すコミットが無いか数えられないRebaseは失敗に数え根元を変えない(
    commits: int | None, reason: str
):
    """数えられない（根元が無い）のを 0 件と同じく失敗にするが、理由は分ける。"""
    git = git_task()
    git.take(STACK_JOB)
    events = git.run(ex(S.REBASE, task=G), commits=commits, result=None, exact=True)
    (failed,) = of_type(events, StageFailed)
    assert reason in failed.reason
    assert not of_type(events, BranchRebased)
    # 走らせて落ちたときと同じく、1 回はやり直す
    assert requested(events) == [ex(S.REBASE, attempt=2, task=G)]


def test_Rebaseは載せ直した先を相手のタスクの新しい根元として知らせる():
    git = git_task()
    git.take(STACK_JOB)
    events = git.run(ex(S.REBASE, task=G), result={"onto": "e" * 40})
    assert of_type(events, BranchRebased) == [BranchRebased(T1, CommitSha("e" * 40), STACK_JOB)]


def test_衝突で止まったRebaseでは根元を変えず解いて続けたCheckUnionの完了で変える():
    git = git_task()
    git.take(STACK_JOB)
    events = git.run(ex(S.REBASE, task=G), result={"onto": "e" * 40}, conflicts=("a.py",))
    assert not of_type(events, BranchRebased)
    assert not of_type(git.run(ex(S.RESOLVE_CONFLICT, task=G)), BranchRebased)
    union = UnionVerdict((UnionFileVerdict("a.py", kept_both=True),))
    events = git.run(ex(S.CHECK_UNION, task=G), union=union)
    assert of_type(events, BranchRebased) == [BranchRebased(T1, CommitSha("e" * 40), STACK_JOB)]


def test_統合に失敗した道では根元を変えない():
    git = git_task()
    git.take(STACK_JOB)
    git.run(ex(S.REBASE, task=G), result={"onto": "e" * 40}, conflicts=("a.py",))
    git.run(ex(S.RESOLVE_CONFLICT, task=G))
    union = UnionVerdict((UnionFileVerdict("a.py", kept_both=False),))
    events = git.run(ex(S.CHECK_UNION, task=G), union=union)
    assert not of_type(events, BranchRebased)


def test_タスクの根元と読む所は切った所から始まり積み直しで動く(task: TaskLoop):
    assert (task.aggregate.base_commit, task.aggregate.code_tree) == (None, None)

    def record(base: str, tree: str | None = None) -> list:
        return task(
            RecordBase(
                command_id=new_id(),
                issuer=POLICY,
                task=T1,
                base=CommitSha(base * 40),
                tree=tree,
            )
        )

    assert record("1", "trees/task1") == [BaseRecorded(CommitSha("1" * 40), "trees/task1")]
    # 同じ根元をもう一度知らせても何もしない（配り直し）
    assert record("1") == []
    # Rebase で根元だけが動いた。読む所はそのまま
    record("2")
    assert (task.aggregate.base_commit, task.aggregate.code_tree) == (
        CommitSha("2" * 40),
        "trees/task1",
    )
    # 止めたタスクでも覚える（積み直しは積んだタスクのブランチを動かす）
    task(StopTask(command_id=new_id(), issuer=POLICY, task=T1, reason="止めた"))
    record("3")
    assert task.aggregate.base_commit == CommitSha("3" * 40)
    with pytest.raises(Rejected, match="まだ開いていない"):
        TaskLoop(TaskId("task5"))(
            RecordBase(command_id=new_id(), issuer=POLICY, task=TaskId("task5"), base=HEAD)
        )


def test_git管理タスクのフローが扱うタスクは仕事の相手でほかは自分():
    git = git_task()
    assert git.aggregate.working_on == G
    git.take(STACK_JOB)
    assert git.aggregate.working_on == T1
    impl = TaskLoop(T1)
    impl.open()
    assert impl.aggregate.working_on == T1


def test_Rebaseが衝突したら解いて両側を残したか確かめる():
    git = git_task()
    git.take(STACK_JOB)
    events = git.run(ex(S.REBASE, task=G), conflicts=("a.py",))
    (rebased,) = of_type(events, StageCompleted)
    # RecordConflict の相手のタスクは、載せた仕事から引ける
    assert (rebased.conflicts, rebased.job) == (("a.py",), STACK_JOB)
    assert requested(events) == [ex(S.RESOLVE_CONFLICT, task=G)]
    git.run(ex(S.RESOLVE_CONFLICT, task=G))
    events = git.run(
        ex(S.CHECK_UNION, task=G), union=UnionVerdict((UnionFileVerdict("a.py", False),))
    )
    assert names(events) == ["StageReported", "EscalationRaised"]
    (reported,) = of_type(events, StageReported)
    # RejectRequest は、この仕事（相手のタスク）と報告の中身から組める
    assert (reported.kind, reported.job) == (E.INTEGRATION_FAILED, STACK_JOB)
    assert "a.py" in reported.reason
    assert of_type(events, EscalationRaised)[0].reason == reported.reason


def test_ResolveConflictが両方は残せないと返したら理由を載せて上げる():
    git = git_task()
    git.take(STACK_JOB)
    git.run(ex(S.REBASE, task=G), conflicts=("a.py",))
    report = {"report": "integration-failed", "reportReason": "両方の意味が違う"}
    events = git.run(ex(S.RESOLVE_CONFLICT, task=G), result=report)
    assert of_type(events, StageReported)[0].reason == "両方の意味が違う"


def test_積む直前の検証が落ちたら統合の失敗として上げる():
    git = git_task()
    git.take(STACK_JOB)
    git.run(ex(S.REBASE, task=G))
    events = git.run(
        ex(S.INTEGRATION_CHECK, task=G), verify=(VerifyResult(VerifyCommand("make test"), 2),)
    )
    raised = of_type(events, EscalationRaised)[0]
    assert raised.kind is E.INTEGRATION_FAILED
    assert raised.reason == "検証コマンドが落ちた: make test"


def test_StackLinkの結果はPRの番号を持ちStackが受けるまで待ち仕事を載せてフローを終える():
    git = git_task()
    git.take(STACK_JOB)
    git.run(ex(S.REBASE, task=G))
    git.run(ex(S.INTEGRATION_CHECK, task=G))
    git.run(ex(S.PUSH, task=G))
    git.run(ex(S.CREATE_PR, task=G), result={"pr": 12})
    events = git.run(ex(S.STACK_LINK, task=G), result={"pr": 12}, confirm=False)
    (linked,) = of_type(events, StageCompleted)
    assert linked.result is not None
    # AppendEntry は、この仕事（タスク・ブランチ・base）と PR の番号から組める
    assert (linked.handoff, linked.result.pr, linked.job) == (
        Handoff.ENTRY,
        PrNumber(12),
        STACK_JOB,
    )
    assert requested(events) == []
    assert requested(git.confirm(ex(S.STACK_LINK, task=G))) == [ex(S.REFRESH_OVERVIEW, task=G)]
    events = git.run(ex(S.REFRESH_OVERVIEW, task=G))
    # FinishGitJob の番号は、フローの終わりに載せた仕事から引ける
    assert of_type(events, FlowFinished) == [FlowFinished(1, STACK_JOB)]


def test_git管理タスクのフローは取り出した仕事を持つ():
    git = git_task()
    rejected = git.flow(*git_job_flow(STACK_JOB))[0]
    assert isinstance(rejected, FlowRejected)
    assert "取り出した仕事を 1 つ持つ" in rejected.reasons[0]


def test_CutBranchは切ったworktreeを知らせる():
    git = git_task()
    job = GitJob(1, GitJobKind.CUT_OVERVIEW, branch=OVERVIEW_BRANCH, base=BranchName("main"))
    git.take(job)
    events = git.run(
        ex(S.CUT_BRANCH, task=G),
        result={
            "task": "planning",
            "tree": "trees/overview",
            "branch": "stack/add-cache--task-0",
            "base": "b" * 40,
        },
    )
    assert names(events) == ["StageCompleted", "WorktreeReady", "FlowFinished"]
    assert of_type(events, WorktreeReady) == [
        # 計画タスクの統括は、切った仕事の種類（概要ブランチ）で組む並びを決める。切った元の
        # コミットは、その worktree を使うタスクの根元になる
        WorktreeReady(
            TaskId.planning(), "trees/overview", OVERVIEW_BRANCH, job, CommitSha("b" * 40)
        )
    ]
    git.take(GitJob(2, GitJobKind.CUT_TASK, task=T1, branch=BRANCH, base=OVERVIEW_BRANCH))
    events = git.run(ex(S.CUT_BRANCH, attempt=2, task=G), result={"tree": "trees/x"}, exact=True)
    assert names(events) == ["StageFailed", "StageRequested"]


# --- 再生 ---


def test_イベントの列を再生すると同じ状態になる(reviewing: TaskLoop):
    task = reviewing
    task.conclude(ex(S.JUDGE, 1), "R1", stalled=("R1",))
    (escalation,) = task.escalation_ids()
    task.resolve(escalation)
    task.run(ex(S.FIX, 1), result_valid=False)
    assert vars(task.replayed()) == vars(task.aggregate)


def test_Gateの不合格を待つ間も再生すると同じ状態になる(gate_failed: TaskLoop):
    assert vars(gate_failed.replayed()) == vars(gate_failed.aggregate)


# --- 回答以外で閉じたフロー ---


def close(loop: TaskLoop, escalation: EventId, reason: str = "再計画を頼んだ") -> list:
    return loop(
        CloseEscalation(
            command_id=new_id(),
            issuer=POLICY,
            task=loop.task,
            escalation=escalation,
            reason=reason,
        )
    )


def test_回答以外で閉じたらフローは続けず計画タスクの再計画のフローを受ける():
    p = TaskId.planning()
    planning = TaskLoop(p)
    planning.open(artifacts=())
    planning.flow(FlowStep(S.PREPARE), FlowStep(S.PLAN), FlowStep(S.DESIGN_LOOP))
    planning.run(ex(S.PREPARE, task=p))
    planning.begin(ex(S.PLAN, task=p))
    planning.report(ex(S.PLAN, task=p), products=(), deferred=DeferredCall("toolu_1", "?"))
    (escalation,) = planning.escalation_ids()
    assert close(planning, escalation) == [
        # どの上げを閉じたかを、種類と起きた実行で見分けられる
        EscalationClosed(escalation, "再計画を頼んだ", kind=E.ASK, origin=ex(S.PLAN, task=p)),
        FlowAbandoned(1, "再計画を頼んだ"),
    ]
    # 止まった位置から続けない
    assert planning.aggregate._moves() == []
    # 応えるエスカレーションが無くても、置き換えのフローを受ける
    events = planning.flow(FlowStep(S.PREPARE), FlowStep(S.PLAN), FlowStep(S.DESIGN_LOOP))
    assert names(events) == ["FlowAccepted", "StageRequested"]


def test_git管理タスクは統合の失敗を閉じた後に仕事を捨て次の仕事のフローを受ける():
    git = git_task()
    git.take(STACK_JOB)
    git.run(ex(S.REBASE, task=G))
    git.run(ex(S.INTEGRATION_CHECK, task=G), verify=(VerifyResult(VerifyCommand("make test"), 2),))
    (escalation,) = git.escalation_ids()
    events = close(git, escalation, "タスクを差し込んだ")
    # 捨てた仕事を載せる（FinishGitJob で次の仕事を取り出せるようにする）
    assert events[-1] == FlowAbandoned(1, "タスクを差し込んだ", STACK_JOB)
    next_job = GitJob(4, GitJobKind.CUT_TASK, task=T1, branch=BRANCH, base=OVERVIEW_BRANCH)
    assert names(git.take(next_job))[0] == "FlowAccepted"


def test_処理中の仕事を外したらフローを捨て走っている実行を止める():
    git = git_task()
    git.take(STACK_JOB)
    git.begin(ex(S.REBASE, task=G))
    events = git(AbandonFlow(command_id=new_id(), issuer=POLICY, task=G, reason="タスクを止めた"))
    assert events == [
        StageInterrupted(ex(S.REBASE, task=G), InterruptCause.REQUESTED),
        FlowAbandoned(1, "タスクを止めた", STACK_JOB),
    ]
    # もう捨てたフローは捨て直さない
    assert git(AbandonFlow(command_id=new_id(), issuer=POLICY, task=G, reason="x")) == []


def test_捨てたフローで上げたエスカレーションはフローと一緒に閉じる():
    git = git_task()
    git.take(STACK_JOB)
    git.run(ex(S.REBASE, task=G))
    git.run(ex(S.INTEGRATION_CHECK, task=G), verify=(VerifyResult(VerifyCommand("make test"), 2),))
    (escalation,) = git.escalation_ids()
    events = git(AbandonFlow(command_id=new_id(), issuer=POLICY, task=G, reason="タスクを止めた"))
    assert events == [
        EscalationClosed(
            escalation,
            "タスクを止めた",
            kind=E.INTEGRATION_FAILED,
            origin=ex(S.INTEGRATION_CHECK, task=G),
        ),
        FlowAbandoned(1, "タスクを止めた", STACK_JOB),
    ]
    # 閉じたので、次の仕事のフローを応える先なしで受ける
    assert git.escalation_ids() == []
    next_job = GitJob(4, GitJobKind.CUT_TASK, task=T1, branch=BRANCH, base=OVERVIEW_BRANCH)
    assert names(git.take(next_job))[0] == "FlowAccepted"


def test_回答以外で閉じてフローを捨てるときも走っている実行を止めほかの上げも閉じる(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.begin(ex(S.IMPL))
    task(Escalate(command_id=new_id(), issuer=POLICY, task=T1, kind=E.STALL, pointers=Pointers()))
    task(Escalate(command_id=new_id(), issuer=POLICY, task=T1, kind=E.STALL, pointers=Pointers()))
    first, second = task.escalation_ids()
    assert close(task, first, "再計画を頼んだ") == [
        EscalationClosed(first, "再計画を頼んだ", kind=E.STALL),
        StageInterrupted(ex(S.IMPL), InterruptCause.REQUESTED),
        EscalationClosed(second, "再計画を頼んだ", kind=E.STALL),
        FlowAbandoned(1, "再計画を頼んだ"),
    ]


def test_捨てたフローの実行は渡した先の答えでも呼び直しでも進めない(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    task.begin(ex(S.REVIEW, round=1))
    task.begin(ex(S.ADVERSARIAL_REVIEW, round=1))
    task.report(ex(S.REVIEW, round=1))
    task(
        MarkInterrupted(
            command_id=new_id(), issuer=DRIVER, task=T1, execution=ex(S.ADVERSARIAL_REVIEW, round=1)
        )
    )
    task(AbandonFlow(command_id=new_id(), issuer=POLICY, task=T1, reason="捨てた"))
    # 遅れて届いた受け渡しの答えは、上げも cursor の移動もしない
    assert task.confirm(ex(S.REVIEW, round=1), refused="受けない") == []
    assert task.confirm(ex(S.REVIEW, round=1)) == []
    # driver の都合で止めた実行も、捨てたフローのものは続きから再開しない
    assert task(ResumeInterrupted(command_id=new_id(), issuer=POLICY, task=T1)) == []


def test_捨てたフローで走らせると決めた実行はやめ始めも再開もやり直しもしない(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    review, adversarial = ex(S.REVIEW, round=1), ex(S.ADVERSARIAL_REVIEW, round=1)
    task.begin(review)
    task(MarkInterrupted(command_id=new_id(), issuer=DRIVER, task=T1, execution=review))
    events = task(AbandonFlow(command_id=new_id(), issuer=POLICY, task=T1, reason="捨てた"))
    assert StageCancelled(adversarial, "捨てた") in events
    assert task.aggregate.executions[adversarial].status is X.ABANDONED
    with pytest.raises(Rejected, match="requested でない"):
        task.begin(adversarial)
    with pytest.raises(Rejected, match="捨てたフローの実行で、再開しない"):
        task(ResumeStage(command_id=new_id(), issuer=CLI, task=T1, execution=review))


def test_Gateの停滞の上げにはGateの結果に添えた調べる先を載せる(reviewing: TaskLoop):
    task = reviewing
    task.conclude(ex(S.JUDGE, 1))
    task.begin(ex(S.GATE))
    where = Pointers(result="tasks/task1/gate.json")
    (failed,) = task(
        ReportStageResult(
            command_id=new_id(),
            issuer=Issuer.executor(ex(S.GATE)),
            task=T1,
            execution=ex(S.GATE),
            evidence=Evidence(exit=StageExit.OK, result_valid=True, gate=gate(GateItem.VERIFY)),
            pointers=where,
            result=stage_result(S.GATE),
        )
    )
    assert isinstance(failed, GateFailed)
    events = task(
        ConcludeGateRound(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            gate=failed.execution,
            unresolved=(FindingId("G-verify"),),
            stalled=(FindingId("G-verify"),),
        )
    )
    (stall,) = of_type(events, EscalationRaised)
    assert stall.pointers == where


def test_始めていないタスクへの呼び直しの再開は何もしない():
    unopened = TaskLoop(TaskId("task9"))
    assert (
        unopened(ResumeInterrupted(command_id=new_id(), issuer=POLICY, task=TaskId("task9"))) == []
    )


def test_変えないと返したのに実物を作ったら形の誤りにする(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    events = task.run(ex(S.IMPL), result={"unchanged": True})
    (failed,) = of_type(events, StageFailed)
    assert "unchanged なのに" in failed.reason


def test_停滞の上げには判定した実行の調べる先と理由を載せる(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    for looker in (S.REVIEW, S.ADVERSARIAL_REVIEW):
        task.run(ex(looker, round=1))
    judge = ex(S.JUDGE, round=1)
    where = Pointers(result="tasks/task1/judge.json")
    task.begin(judge)
    task(
        ReportStageResult(
            command_id=new_id(),
            issuer=Issuer.executor(judge),
            task=T1,
            execution=judge,
            evidence=Evidence(exit=StageExit.OK, result_valid=True),
            pointers=where,
            result=stage_result(S.JUDGE, stallCause="tests", stallReason="テストが条件と違う"),
        )
    )
    events = task.conclude(judge, "R1", stalled=("R1",))
    (stall,) = of_type(events, EscalationRaised)
    assert (stall.pointers, stall.reason) == (where, "テストが条件と違う")


def test_もう閉じたエスカレーションを閉じる知らせは何もしない(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL), result=reported("design-gap"))
    (escalation,) = task.escalation_ids()
    task.flow(*IMPL_FLOW, responds_to=escalation)
    assert close(task, escalation) == []
    # 応えたフローは止めない
    assert not task.aggregate.halted


def test_書き直す前のフローで中断した実行は新しいフローを止めず再開もしない(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.begin(ex(S.IMPL))
    task(MarkInterrupted(command_id=new_id(), issuer=DRIVER, task=T1, execution=ex(S.IMPL)))
    task(Escalate(command_id=new_id(), issuer=POLICY, task=T1, kind=E.STALL, pointers=Pointers()))
    (escalation,) = task.escalation_ids()
    events = task.flow(*IMPL_FLOW, responds_to=escalation)
    assert requested(events) == [ex(S.IMPL, attempt=2)]
    with pytest.raises(Rejected, match="書き直す前か捨てたフローの実行で、再開しない"):
        task(ResumeStage(command_id=new_id(), issuer=CLI, task=T1, execution=ex(S.IMPL)))


def test_書き直す前のフローの完了は今のフローの失敗の数を消さない(task: TaskLoop):
    task.flow(*IMPL_FLOW)
    task.run(ex(S.IMPL))
    task.begin(ex(S.REVIEW, 1))
    task.run(ex(S.ADVERSARIAL_REVIEW, 1), result_valid=False)
    task.run(ex(S.ADVERSARIAL_REVIEW, 1, attempt=2), result_valid=False)  # stage-errors
    (escalation,) = task.escalation_ids()
    task.flow(*IMPL_FLOW[1:], responds_to=escalation)
    # 新しいフローで 1 回落ちた後に、古いフローの Review が終わる
    task.report(ex(S.REVIEW, 1))
    task.run(ex(S.ADVERSARIAL_REVIEW, 1, attempt=3), result_valid=False)
    position = (0, S.ADVERSARIAL_REVIEW, 1)
    assert task.aggregate.failures == {position: 1}


def test_積む列から外して始め直すときは新しいブランチで積む(reviewing: TaskLoop):
    task = reviewing
    task.conclude(ex(S.JUDGE, 1))
    task.run(ex(S.GATE), gate=PASSED)
    task.run(ex(S.WRITE_PR_BODY))
    renamed = BranchName("stack/add-cache--task-1-r1")
    task(
        ChangeScope(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            spec=TaskSpec("広げた"),
            artifacts=(),
            pointers=Pointers(),
            branch=renamed,
        )
    )
    task.flow(FlowStep(S.REVIEW_LOOP, reviewers=BOTH), FlowStep(S.GATE), FlowStep(S.WRITE_PR_BODY))
    task.run(ex(S.REVIEW, 1, attempt=2))
    task.run(ex(S.ADVERSARIAL_REVIEW, 1, attempt=2))
    task.run(ex(S.JUDGE, 1, attempt=2))
    task.conclude(ex(S.JUDGE, 1, attempt=2))
    task.run(ex(S.GATE, attempt=2), gate=PASSED)
    assert of_type(task.run(ex(S.WRITE_PR_BODY, attempt=2)), TaskGated) == [TaskGated(renamed)]


# --- ステージの宣言と、エスカレーションの経路の表 ---


def test_ステージの報告と期待する証拠から上がる種類はEscalationRouterの表と食い違わない():
    for spec in STAGE_SPECS.values():
        for task_kind in spec.task_kinds:
            for kind in spec.raises:
                assert EscalationRouter.why_not_raise(task_kind, kind) is None, (spec.kind, kind)
    for item in GateItem:
        if item.escalation is not None:
            assert EscalationRouter.why_not_raise(TaskKind.IMPLEMENTATION, item.escalation) is None


def test_合成ステージは中の役を1つずつ持つ():
    for spec in STAGE_SPECS.values():
        if spec.mode is not StageMode.COMPOSITE:
            continue
        roles = [STAGE_SPECS[kind].role for kind in spec.inner]
        assert roles.count(InnerRole.JUDGE) == 1, spec.kind
        fixers = [STAGE_SPECS[kind] for kind in spec.inner if STAGE_SPECS[kind].fixes is not None]
        if spec.kind is S.REVIEW_LOOP:
            # 宛先ごとに直す役が 1 つずつ（tests が先、impl が後）
            assert [f.fixes for f in fixers] == [T.TESTS, T.IMPL]
            assert roles.count(InnerRole.FIXER) == 2
        else:
            assert roles.count(InnerRole.FIXER) == 1, spec.kind
            assert not fixers, spec.kind
        assert InnerRole.LOOKER in roles, spec.kind
        assert all(STAGE_SPECS[kind].parent is spec.kind for kind in spec.inner)
