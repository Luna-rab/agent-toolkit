"""`status --json` の欄（`SECTIONS`）。

どの欄も、再生した集約の値と、ドメインの問い（`Run.phase`・`Task.step_states` など）の答えを
JSON にするだけにする。「この状態ならこう見せる」という判断が要るなら、ここで分岐を書かずに、
集約に名前の付いた問いを足して呼ぶ。集約の欄をそのまま出さないのは、集約の形を
変えても HUD と `/autodev` を壊さないためである。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from ...domain.aggregates.base import Aggregate
from ...domain.aggregates.design import Design
from ...domain.aggregates.questions import Questions
from ...domain.aggregates.review_ledger import ReviewLedger
from ...domain.aggregates.run import Run, TaskEntry
from ...domain.aggregates.stack import Stack
from ...domain.aggregates.task import Execution, ExecutionStatus, Task
from ...domain.events.base import Event
from ...domain.events.run import RunStarted
from ...domain.events.task import (
    ExecutionRestarted,
    GateFailed,
    HandoffFailed,
    StageCancelled,
    StageCompleted,
    StageDeferred,
    StageFailed,
    StageInterrupted,
    StageReported,
    StageSentBack,
    StageStarted,
)
from ...domain.value_objects.decision import Decision
from ...domain.value_objects.git_job import GitJob
from ...domain.value_objects.model_class import ModelClass, ModelClasses
from ...domain.value_objects.stack_entry import StackEntry
from ...domain.value_objects.stream_id import StreamId
from ...domain.value_objects.task_id import TaskId
from ...domain.value_objects.task_spec import TaskSpec
from ..store.eventstore import AggregateFactory, StoredEvent

A = TypeVar("A", bound=Aggregate)

#: 実行の終わりを確定するイベント。HandoffFailed は StageCompleted の後に来るので、後のもので上書きする
_ENDINGS = (
    StageCompleted,
    GateFailed,
    StageFailed,
    StageInterrupted,
    StageDeferred,
    StageCancelled,
    ExecutionRestarted,
    HandoffFailed,
    StageReported,
    StageSentBack,
)


@dataclass(frozen=True)
class DriverFacts:
    """イベントには残らない、driver のプロセスの事実。"""

    #: ランディレクトリの絶対パス
    directory: str
    #: `driver.lock` をほかのプロセスが握っている
    running: bool
    #: 前の driver が起こして、まだ生きている子の pid
    live_children: tuple[int, ...] = ()


@dataclass(frozen=True)
class Replayed:
    """欄を作る材料。集約と、確定したイベントの列と、走っているステージの進み具合と、driver の事実。"""

    aggregates: Mapping[StreamId, Aggregate]
    history: Sequence[tuple[StoredEvent, Event]]
    #: `ExecutionId` の文字列 → 進み具合のファイルの中身
    progress: Mapping[str, Any]
    #: 再生に使ったもの。イベントがまだ無いストリームの集約も、これで作る
    factory: AggregateFactory
    driver: DriverFacts

    def get(self, stream: StreamId, kind: type[A]) -> A:
        """型が違えば TypeError。空の集約で置き換えると、欄が黙って空になる。"""
        found = self.aggregates.get(stream)
        if found is None:
            found = self.factory(stream)
        if not isinstance(found, kind):
            raise TypeError(
                f"{stream} の集約は {type(found).__name__} で、{kind.__name__} ではない"
            )
        return found

    @property
    def run(self) -> Run:
        return self.get(StreamId.run(), Run)

    @property
    def stack(self) -> Stack:
        return self.get(StreamId.stack(), Stack)

    def task(self, task: TaskId) -> Task:
        return self.get(StreamId.task(task), Task)

    def started_at(self) -> dict[str, str]:
        """実行 → 最後に始めた（StageStarted を確定した）時刻。続きから再開したら、再開した時刻になる。"""
        return {
            str(event.execution): stored.at
            for stored, event in self.history
            if isinstance(event, StageStarted)
        }

    def ended_at(self) -> dict[str, str]:
        """実行 → 最後の StageStarted より後に、終わりを確定した時刻。続きから始めたら消える。"""
        ended: dict[str, str] = {}
        for stored, event in self.history:
            if isinstance(event, StageStarted):
                ended.pop(str(event.execution), None)
            elif isinstance(event, _ENDINGS):
                ended[str(event.execution)] = stored.at
        return ended


#: 再生した集約から、外向けの形の 1 つの欄を作る
Section = Callable[[Replayed], Any]


def _value(item: Any) -> Any:
    """値オブジェクト・列挙を JSON の値に。None はそのまま。"""
    return None if item is None else getattr(item, "value", item)


def _ids(items: Any) -> list[str]:
    return sorted(str(item) for item in items)


def run_section(view: Replayed) -> dict[str, Any]:
    run = view.run
    awaiting = view.get(StreamId.questions(), Questions).awaiting_answer
    started = next(
        ((stored, event) for stored, event in view.history if isinstance(event, RunStarted)),
        None,
    )
    return {
        "phase": run.phase.value,
        "awaiting_answer": awaiting,
        "started_at": started[0].at if started else None,
        "repository": started[1].repository.value if started else None,
        "base": started[1].base.value if started else None,
        "limit": run.limit.value,
        "resumes": run.resumes,
        "panic_cause": run.panic_cause,
        "directory": view.driver.directory,
        "driver_running": view.driver.running,
        "live_children": list(view.driver.live_children),
        "driver_stopped": run.driver_stopped(view.driver.running, awaiting),
        "stacked_tasks": run.stacked_count,
        "stack_target_tasks": run.stack_target_count,
        "models": _models(run.models),
    }


def _models(models: ModelClasses) -> dict[str, dict[str, str]]:
    return {
        cls.value: {
            "model": models.of(cls).model.value,
            "effort": models.of(cls).effort.value,
            "agent": models.of(cls).agent.value,
        }
        for cls in ModelClass
    }


def _job(job: GitJob | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "id": job.id,
        "kind": job.kind.value,
        "task": _value(job.task),
        "branch": _value(job.branch),
    }


@dataclass(frozen=True)
class _Times:
    """実行 id の文字列 → 時刻（始めた・終えた）。タスクごとに履歴を読み直さない。"""

    started: Mapping[str, str]
    ended: Mapping[str, str]


def _execution(execution: Execution, view: Replayed, times: _Times) -> dict[str, Any]:
    key = str(execution.id)
    return {
        "id": key,
        "stage": execution.id.stage.value,
        "round": execution.id.round,
        "attempt": execution.id.attempt,
        "flow_version": execution.flow_version,
        "step": execution.step,
        "status": execution.status.value,
        "started_at": times.started.get(key),
        # 実行器が消し損ねた・止めた後に残ったファイルを、走っていない実行に見せない
        "progress": view.progress.get(key) if execution.status is ExecutionStatus.RUNNING else None,
        "ended_at": times.ended.get(key),
        "end_reason": execution.end_reason,
        "interrupted_by": _value(execution.interrupted_by),
        "gate_failures": [
            {"item": failure.item.value, "reason": failure.reason}
            for failure in execution.gate_failures
        ],
    }


def _executions(executions: Sequence[Execution], view: Replayed, times: _Times) -> list[Any]:
    return [_execution(e, view, times) for e in executions]


def _flow(task: Task, view: Replayed, times: _Times) -> dict[str, Any] | None:
    flow = task.flow
    if flow is None:
        return None
    cursor = task.cursor
    steps = []
    for index, (step, state) in enumerate(zip(flow.steps, task.step_states(), strict=True)):
        entry: dict[str, Any] = {
            "stage": step.stage.value,
            "state": state.value,
            "executions": _executions(task.executions_at(index), view, times),
        }
        if index == cursor.step and cursor.inner is not None:
            entry["inner"] = cursor.inner.value
            entry["round"] = cursor.round
        steps.append(entry)
    return {
        "version": flow.version,
        "steps": steps,
        "finished": task.flow_finished,
        "halted": task.halted,
        "job": _job(flow.job),
    }


def _spec(spec: TaskSpec | None) -> dict[str, Any] | None:
    if spec is None:
        return None
    return {"dod": spec.dod, "acceptance": list(spec.acceptance), "scope": list(spec.scope)}


def _note(note: Decision) -> dict[str, Any]:
    return {"text": note.text, "origin": note.origin.value, "question": _value(note.question)}


def _findings(view: Replayed, ledger: StreamId | None) -> list[dict[str, Any]]:
    """台帳に立てた順。台帳が無ければ空。"""
    if ledger is None:
        return []
    return [
        {
            "id": finding.id.value,
            "rating": finding.rating.value,
            "status": finding.status.value,
            "body": finding.body,
            "location": _value(finding.location),
            "fixes": finding.fixes,
            "stalled": finding.stalled,
            "comments": list(finding.comments),
            "design": _value(finding.design),
        }
        for finding in view.get(ledger, ReviewLedger).findings.values()
    ]


def _task(entry: TaskEntry, view: Replayed, times: _Times) -> dict[str, Any]:
    run = view.run
    task = view.task(entry.id)
    stacked = view.stack.entry_of(entry.id)
    spec = entry.spec or task.spec
    return {
        "id": entry.id.value,
        "kind": entry.id.kind.value,
        "title": spec.title if spec is not None else None,
        "status": entry.status.value,
        "terminal": entry.status.is_terminal,
        "blocked_by": _ids(entry.blocked_by),
        "branch": _value(task.branch),
        "pr": stacked.pr.value if stacked is not None else None,
        "superseded_by": _value(entry.superseded_by),
        "takes_over": _value(entry.takes_over),
        "integration_failed": entry.integration_failed,
        "awaiting_requeue": entry.id in run.requeue,
        "spec": _spec(spec),
        "notes": [_note(note) for note in task.notes],
        "flow": _flow(task, view, times),
        "earlier_executions": _executions(task.earlier_executions(), view, times),
        "unplaced_executions": _executions(task.unplaced_executions(), view, times),
        "escalations": [
            {
                "id": e.id.value,
                "kind": e.kind.value,
                "origin": str(e.origin) if e.origin is not None else None,
                "reason": e.reason,
                "question": e.question,
            }
            for e in task.escalations.values()
        ],
        "findings": _findings(view, entry.own_findings),
    }


def tasks_section(view: Replayed) -> list[dict[str, Any]]:
    """計画タスク・git 管理タスク（始めていれば）、実装タスク（番号の順）。"""
    run = view.run
    times = _Times(view.started_at(), view.ended_at())
    others = [run.tasks[t] for t in (TaskId.planning(), TaskId.git()) if t in run.tasks]
    return [_task(entry, view, times) for entry in (*others, *run.implementation_tasks)]


def _entry(entry: StackEntry | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "task": entry.task.value,
        "branch": entry.branch.value,
        "pr": entry.pr.value,
        "base": entry.base.value,
    }


def stack_section(view: Replayed) -> dict[str, Any]:
    stack = view.stack
    return {
        "overview": _entry(stack.overview),
        "entries": [_entry(entry) for entry in stack.entries],
        "top": _value(stack.top),
        "queue": [_job(job) for job in stack.queue],
        "current": _job(stack.current),
        "parked": _job(stack.parked),
        "paused": stack.stacking_paused,
        "cuts_pending": stack.cuts_pending,
    }


def questions_section(view: Replayed) -> list[dict[str, Any]]:
    return [
        {"id": q.id.value, "body": q.body, "escalation": _value(q.escalation)}
        for q in view.get(StreamId.questions(), Questions).open_questions
    ]


def escalations_section(view: Replayed) -> list[dict[str, Any]]:
    """Run の側で開いているエスカレーション（ラン統括か `/autodev` が応える）。"""
    return [
        {
            "id": e.id.value,
            "kind": e.kind.value,
            "task": _value(e.task),
            "source": _value(e.source),
            "for_user": e.for_user,
            "answer_only": e.answer_only,
            "failures": e.failures,
            "reason": e.reason,
            "question": e.question,
        }
        for e in view.run.escalations.values()
    ]


def plan_section(view: Replayed) -> dict[str, Any]:
    run = view.run
    design = view.get(StreamId.design(), Design)
    proposal = design.proposal
    return {
        "planning": run.planning,
        "planned": run.planned_once,
        "applied_design": _value(run.applied_design),
        "replans_without_stack": run.replan_streak,
        "design": {
            "versions": [version.value for version in design.versions],
            "settled": _value(design.settled.design) if design.settled is not None else None,
            "proposal": None
            if proposal is None
            else {
                "version": proposal.design.value,
                "state": _value(design.proposal_state),
                "round": design.round,
                "awaiting": _value(design.awaiting),
            },
        },
        "findings": _findings(view, StreamId.design_review()),
    }


SECTIONS: Mapping[str, Section] = {
    "run": run_section,
    "tasks": tasks_section,
    "stack": stack_section,
    "questions": questions_section,
    "escalations": escalations_section,
    "plan": plan_section,
}
