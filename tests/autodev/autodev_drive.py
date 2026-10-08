"""組み立ての根（`app/driving/driver.py`）の検査で使う、偽の実行器と偽の AgentRuntime。

偽の実行器は、test_seams の台本（`Script`）どおりにステージの結果を返す。LLM のステージでは、本物の
プロンプトの組み立て（`Prompts.prompt`）を呼んで、組んだプロンプトを残す。偽の AgentRuntime は、統括の
プロンプトから知らせ（`<通知>` の JSON）を読み、決め打ちの判断の JSON を返す。
"""

from __future__ import annotations

import itertools
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autodev_samples import stage_result
from test_seams import HOLD, Outcome, Script

from autodev.adapters.agent.types import AgentCall, AgentOutcome, Ending
from autodev.app.driving.driver import Driver, DriverParts, StartRequest
from autodev.app.driving.mainloop import Ticket
from autodev.app.stages.executor import StageContext, StagePrompt
from autodev.app.stages.files import write_design
from autodev.domain.aggregates.task import Task
from autodev.domain.commands.run import Panic, StartRun
from autodev.domain.commands.task import BeginStage, ReportStageResult
from autodev.domain.events.base import Event
from autodev.domain.flow.flow import FlowStep
from autodev.domain.stages.catalog import STAGE_SPECS
from autodev.domain.stages.kinds import StageMode
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.artifact_ref import ArtifactRef
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.commit_sha import CommitSha
from autodev.domain.value_objects.design_version import DesignVersion
from autodev.domain.value_objects.evidence import Evidence
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.git_job_kind import GitJobKind
from autodev.domain.value_objects.instruction import Instruction
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.pointers import Pointers
from autodev.domain.value_objects.repository import Repository
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.session_id import SessionId
from autodev.domain.value_objects.stage_exit import StageExit
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.infra.paths import RunPaths

S = StageKind
J = GitJobKind
NAME = RunName("add-cache")
HEAD = CommitSha("a" * 40)
PLANNING = TaskId.planning()
GIT = TaskId.git()

#: 台本がこれを返したら、実行器は結果の代わりに Panic を返す（利用枠の上限に当たった）
PANIC = Outcome(result={"panic": True}, exact=True)


def cut_base(job: int) -> str:
    """仕事 `job` の CutBranch が切った元のコミット（仕事ごとに違う）。"""
    return format(job, "040x")


def rebased_onto(job: int) -> str:
    """仕事 `job` の Rebase が載せ直した先のコミット（仕事ごとに違う）。"""
    return format(0x1000 + job, "040x")


class View:
    """台本に見せる、driver の今の状態（test_seams の World と同じ読み口）。"""

    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def events(self, cls: type[Event]) -> list[Any]:
        return [d.event for d in self._driver.loop.history if isinstance(d.event, cls)]

    def task(self, task: TaskId) -> Task:
        found = self._driver.loop.aggregates.get(StreamId.task(task))
        return found if isinstance(found, Task) else Task(StreamId.task(task))


@dataclass
class Counters:
    """呼び直しをまたいで続く番号（本物では GitHub と design/ が持っている）。"""

    ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    versions: itertools.count = field(default_factory=lambda: itertools.count(1))
    prs: itertools.count = field(default_factory=lambda: itertools.count(100))
    task_prs: dict[TaskId, int] = field(default_factory=dict)


class FakeExecutor:
    """台本どおりに BeginStage と ReportStageResult を返す実行器。"""

    def __init__(
        self, parts: DriverParts, script: Script, view: Callable[[], View], counters: Counters
    ) -> None:
        self.parts = parts
        self.script = script
        self.view = view
        #: 組んだプロンプト（実行・プロンプト）
        self.prompts: list[tuple[ExecutionId, StagePrompt]] = []
        self.interrupted: list[ExecutionId] = []
        self.restarted: list[ExecutionId] = []
        self.aborted: list[TaskId] = []
        self.held: dict[ExecutionId, Ticket] = {}
        self._ids = counters.ids
        self._versions = counters.versions
        self._prs = counters.prs
        self._task_prs = counters.task_prs

    # --- 受け口 ---

    def begin(self, execution: ExecutionId, ticket: Ticket) -> None:
        llm = STAGE_SPECS[execution.stage].mode is StageMode.LLM
        session = SessionId(f"{next(self._ids):08x}-0000-4000-8000-000000000000") if llm else None
        ticket.submit(
            BeginStage(
                command_id=CommandId(f"begin/{execution}"),
                issuer=Issuer.executor(execution),
                task=execution.task,
                execution=execution,
                head=HEAD,
                session=session,
            )
        )

    def run(self, execution: ExecutionId, ticket: Ticket) -> None:
        task = self.view().task(execution.task)
        if STAGE_SPECS[execution.stage].mode is StageMode.LLM:
            context = StageContext(execution, self._step(task, execution), self._tree(execution))
            prompt = self.parts.prompts.prompt(context, self.parts.aggregates())
            self.prompts.append((execution, prompt))
        outcome = self.script(self.view(), execution)  # ty: ignore[invalid-argument-type]
        if outcome is HOLD:
            self.held[execution] = ticket
            return
        if outcome is PANIC:
            ticket.submit(
                Panic(
                    command_id=CommandId(f"panic/{next(self._ids)}"),
                    issuer=Issuer.driver(),
                    cause="429",
                )
            )
            return
        ticket.submit(self._report(task, execution, outcome or Outcome()))

    def interrupt(self, execution: ExecutionId) -> None:
        self.interrupted.append(execution)
        if (ticket := self.held.pop(execution, None)) is not None:
            ticket.cancel()

    def restart(self, execution: ExecutionId, start_commit: CommitSha | None) -> None:
        self.restarted.append(execution)

    def abort_rebase(self, task: TaskId) -> None:
        self.aborted.append(task)

    def join(self, timeout: float = 30.0) -> None:
        """子プロセスを起こさないので、待つものが無い。"""

    # --- 中身 ---

    @staticmethod
    def _step(task: Task, execution: ExecutionId) -> FlowStep:
        found = task.executions[execution]
        flow = task.flow
        if flow is None or found.step >= len(flow.steps):
            return FlowStep(execution.stage)
        return flow.steps[found.step]

    def _tree(self, execution: ExecutionId) -> Path:
        paths = self.parts.paths
        if execution.task.kind is not TaskId.numbered(1).kind:
            return paths.overview_tree
        return paths.task_tree(execution.task)

    def _report(self, task: Task, execution: ExecutionId, outcome: Outcome) -> ReportStageResult:
        spec = STAGE_SPECS[execution.stage]
        made = spec.produces if outcome.products is None else outcome.products
        result = (
            outcome.result
            if outcome.exact
            else stage_result(
                execution.stage, **{**self._result(task, execution.stage), **outcome.result}
            )
        )
        products = tuple(self._product(kind, result) for kind in sorted(made, key=str))
        # 根元から上のコミットがある（Rebase は 0 件・数えられないと落ちる）
        evidence: dict[str, Any] = {"exit": StageExit.OK, "result_valid": True, "commits": 1}
        evidence |= self._evidence(execution)
        evidence |= outcome.evidence
        return ReportStageResult(
            command_id=CommandId(f"report/{execution}/{next(self._ids)}"),
            issuer=Issuer.executor(execution),
            task=execution.task,
            execution=execution,
            evidence=Evidence(products=products, **evidence),
            pointers=Pointers(),
            result=result,
        )

    def _product(self, kind: ArtifactKind, result: dict[str, Any]) -> ArtifactRef:
        if kind is ArtifactKind.PROPOSAL:
            # 実行器は提案の本文を design/v<版>.md に書き出し、その版を在りかにする
            version = DesignVersion(next(self._versions))
            write_design(self.parts.paths, version, str(result.get("design") or "設計"))
            return ArtifactRef(kind, str(version.value))
        return ArtifactRef(kind, "x")

    @staticmethod
    def _evidence(execution: ExecutionId) -> dict[str, Any]:
        from autodev.domain.value_objects.gate_item import GateItem  # noqa: PLC0415
        from autodev.domain.value_objects.gate_item_result import GateItemResult  # noqa: PLC0415
        from autodev.domain.value_objects.gate_report import GateReport  # noqa: PLC0415
        from autodev.domain.value_objects.union_verdict import UnionVerdict  # noqa: PLC0415
        from autodev.domain.value_objects.verify_command import VerifyCommand  # noqa: PLC0415
        from autodev.domain.value_objects.verify_result import VerifyResult  # noqa: PLC0415

        stage = execution.stage
        if stage is S.GATE:
            return {"gate": GateReport(tuple(GateItemResult(i, True) for i in GateItem))}
        if stage is S.CHECK_UNION:
            return {"union": UnionVerdict(())}
        if stage is S.CONFIRM_RED:
            return {"verify": (VerifyResult(VerifyCommand("pytest"), 1),)}
        return {}

    def _result(self, task: Task, stage: StageKind) -> dict[str, Any]:
        """実行器が組む決定的なステージの結果（切った worktree・PR の番号）。"""
        flow = task.flow
        job = flow.job if flow is not None else None
        if stage is S.CUT_BRANCH and job is not None:
            user = job.task or PLANNING
            tree = {J.CUT_OVERVIEW: "trees/overview", J.CUT_STACK_TOP: "trees/stack-top"}.get(
                job.kind, f"trees/{user}"
            )
            branch = None if job.kind is J.CUT_STACK_TOP else job.branch
            return {
                "task": str(user),
                "tree": tree,
                "branch": str(branch) if branch else None,
                "base": cut_base(job.id),
            }
        if stage is S.REBASE and job is not None:
            return {"onto": rebased_onto(job.id)}
        if stage is S.CREATE_OVERVIEW_PR:
            return {"pr": next(self._prs)}
        if stage in (S.CREATE_PR, S.STACK_LINK) and job is not None and job.task is not None:
            if stage is S.CREATE_PR:
                self._task_prs[job.task] = next(self._prs)
            return {"pr": self._task_prs[job.task]}
        return {}


# --- 偽の AgentRuntime ---

_NOTICE = re.compile(r"## `<通知>`\s*```json\n(.*?)\n```", re.DOTALL)


def notice_of(prompt: str | None) -> dict[str, Any]:
    found = _NOTICE.search(prompt or "")
    assert found is not None, f"知らせが無い: {prompt!r}"
    return json.loads(found.group(1))


#: 統括の判断を決める関数。判断の JSON か、そのまま返す AgentOutcome
Decide = Callable[[AgentCall, dict[str, Any]], "dict[str, Any] | AgentOutcome"]


@dataclass
class FakeRuntime:
    decide: Decide
    calls: list[tuple[AgentCall, dict[str, Any]]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self, call: AgentCall) -> FakeProcess:
        return FakeProcess(self, call)


@dataclass
class FakeProcess:
    runtime: FakeRuntime
    call: AgentCall

    def wait(self) -> AgentOutcome:
        notice = notice_of(self.call.prompt)
        with self.runtime._lock:
            self.runtime.calls.append((self.call, notice))
        decided = self.runtime.decide(self.call, notice)
        if isinstance(decided, AgentOutcome):
            return decided
        # 本物はセッションを開けたら init を出す。受けずに --resume から終わると続けられなかったと数える
        return AgentOutcome(
            ending=Ending.RESULT,
            exit_code=0,
            session=self.call.session,
            structured=decided,
            initialized=True,
        )

    def interrupt(self, reason: str) -> None:
        pass


def is_task_supervisor(call: AgentCall) -> bool:
    return "run-flow" in (call.json_schema or "")


IMPL_FLOW = [
    {"stage": "Impl"},
    {"stage": "ReviewLoop", "reviewers": {"first": ["Review"]}},
    {"stage": "Gate"},
    {"stage": "WritePrBody"},
]


def run_flow(responds_to: str | None = None) -> dict[str, Any]:
    return {"decision": "run-flow", "runFlow": {"steps": IMPL_FLOW, "respondsTo": responds_to}}


def supervisors(call: AgentCall, notice: dict[str, Any]) -> dict[str, Any]:
    """いつもの統括: タスク統括はいつものフローを返し、ラン統括は聞いて・渡して・反映して・仕上げる。"""
    kind = notice["notice"]
    if is_task_supervisor(call):
        assert kind in ("started", "scope-changed", "decision-rejected"), notice
        return run_flow()
    if kind == "settled-replan":
        proposal = notice["proposal"]
        return {
            "decision": "apply-plan",
            "applyPlan": {
                "design": proposal["design"],
                "stop": proposal["stop"],
                "discard": proposal["discard"],
                "respondsTo": None,
            },
        }
    if kind == "escalation":
        return {
            "decision": "ask-user",
            "askUser": {
                "question": "q-scope",
                "body": notice["reason"],
                "escalation": notice["id"],
            },
        }
    if kind == "answer":
        return {
            "decision": "answer",
            "answer": {
                "escalation": notice["escalation"],
                "answer": None,
                "question": notice["question"],
            },
        }
    assert kind == "all-settled", notice
    return {"decision": "finish", "finish": {"readyOverview": True}}


# --- 組み立て ---


@dataclass
class Rig:
    """検査 1 つぶんの driver と偽物。`drive` のたびに driver を組み直す（呼び直しと同じ）。"""

    root: Path
    script: Script
    decide: Decide = supervisors
    runtime: FakeRuntime = field(init=False)
    executors: list[FakeExecutor] = field(default_factory=list)
    driver: Driver | None = None
    counters: Counters = field(default_factory=Counters)

    def __post_init__(self) -> None:
        self.runtime = FakeRuntime(self.decide)

    @property
    def paths(self) -> RunPaths:
        return RunPaths(NAME, self.root / NAME.value)

    @property
    def executor(self) -> FakeExecutor:
        return self.executors[-1]

    def drive(self, *, start: bool = False) -> int:
        holder: list[Driver] = []

        def make(parts: DriverParts) -> FakeExecutor:
            executor = FakeExecutor(parts, self.script, lambda: View(holder[0]), self.counters)
            self.executors.append(executor)
            return executor

        driver = Driver(
            self.paths,
            executor=make,
            runtime=self.runtime,
            status_command="autodev status --json",
            poll_interval=0.005,
        )
        holder.append(driver)
        self.driver = driver
        request = None
        if start:
            request = StartRequest(
                StartRun(
                    command_id=CommandId(f"start/{NAME}"),
                    issuer=Issuer.cli(),
                    name=NAME,
                    instruction=Instruction("キャッシュを足す"),
                    repository=Repository(str(self.root / "repo")),
                    base=BranchName("main"),
                )
            )
        return int(driver.drive(request))
