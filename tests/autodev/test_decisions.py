"""統括の判断の JSON をコマンドに置き換える表（`app/supervision/decisions.py`）。"""

from __future__ import annotations

from typing import Any

import pytest

from autodev.app.supervision.decisions import (
    RUN_DECISIONS,
    TASK_DECISIONS,
    DecisionError,
    payload_key,
    to_command,
)
from autodev.domain.commands.questions import PostQuestion
from autodev.domain.commands.run import (
    AnswerEscalation,
    ApplyReplan,
    EscalateToRun,
    FinishRun,
    InsertTask,
    RequestReplan,
    StopTasks,
)
from autodev.domain.commands.task import AcceptFlow
from autodev.domain.flow.flow import FlowStep, Reviewers
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.design_version import DesignVersion
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.session_id import SessionId
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stall_cause import StallCause
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_spec import TaskSpec
from autodev.domain.value_objects.verify_command import VerifyCommand

SESSION = SessionId("0f8fad5b-d9cb-469f-a165-70867728950e")
T1 = TaskId("task1")
ID = CommandId("decision/1")


def run_command(raw: dict[str, Any]):
    return to_command(RUN_DECISIONS, raw, command_id=ID, issuer=Issuer.run_supervisor(SESSION))


def task_command(raw: dict[str, Any]):
    issuer = Issuer.task_supervisor(T1, SESSION)
    return to_command(TASK_DECISIONS, raw, command_id=ID, issuer=issuer, task=T1)


def test_中身の欄の名前はdecisionをcamelCaseにしたもの():
    assert payload_key("insert-task") == "insertTask"
    assert payload_key("finish") == "finish"


def test_タスク統括のフローはAcceptFlowに統括するタスクを埋めて置き換える():
    command = task_command(
        {
            "decision": "run-flow",
            "runFlow": {
                "steps": [
                    {"stage": "Impl", "freshSession": True, "instruction": "境界を確かめる"},
                    {
                        "stage": "ReviewLoop",
                        "reviewers": {"first": ["Review", "AdversarialReview"]},
                    },
                ],
                "respondsTo": "task/task1#4",
            },
            "escalate": None,
        }
    )
    assert command == AcceptFlow(
        command_id=ID,
        issuer=Issuer.task_supervisor(T1, SESSION),
        task=T1,
        steps=(
            FlowStep(StageKind.IMPL, fresh_session=True, instruction="境界を確かめる"),
            FlowStep(
                StageKind.REVIEW_LOOP,
                reviewers=Reviewers((StageKind.REVIEW, StageKind.ADVERSARIAL_REVIEW)),
            ),
        ),
        responds_to=EventId("task/task1#4"),
    )


def test_タスク統括の上げはEscalateToRunになる():
    command = task_command(
        {
            "decision": "escalate",
            "escalate": {
                "kind": "needs-replan",
                "reason": "範囲の外を変える",
                "pointers": {
                    "taskDir": "tasks/task1",
                    "result": None,
                    "log": None,
                    "tree": "trees/task1",
                    "session": None,
                },
                "hint": {
                    "findingIds": ["R1"],
                    "stallCause": "scope",
                    "designCause": None,
                    "gateItems": [],
                },
                "source": "task/task1#9",
            },
        }
    )
    assert isinstance(command, EscalateToRun)
    assert (command.task, command.kind, command.source) == (
        T1,
        EscalationKind.NEEDS_REPLAN,
        EventId("task/task1#9"),
    )
    assert command.hint.stall_cause is StallCause.SCOPE
    assert command.pointers.task_dir == "tasks/task1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {
                "decision": "replan",
                "replan": {"reason": "割り直す", "trigger": None, "answer": "q1"},
            },
            RequestReplan,
        ),
        (
            {
                "decision": "insert-task",
                "insertTask": {
                    "spec": {
                        "title": "引き継ぐ",
                        "dod": "",
                        "acceptance": [],
                        "scope": [],
                        "entryPoints": ["src/a.py"],
                        "boundary": "",
                        "taskTests": [],
                    },
                    "blockedBy": ["task1"],
                    "takesOver": "task2",
                    "respondsTo": "run#5",
                },
            },
            InsertTask,
        ),
        (
            {"decision": "stop-tasks", "stopTasks": {"tasks": ["task1"], "respondsTo": None}},
            StopTasks,
        ),
        (
            {
                "decision": "apply-plan",
                "applyPlan": {"design": 3, "stop": [], "discard": ["task1"], "respondsTo": None},
            },
            ApplyReplan,
        ),
        (
            {
                "decision": "answer",
                "answer": {"escalation": "run#3", "answer": "A", "question": None},
            },
            AnswerEscalation,
        ),
        (
            {
                "decision": "ask-user",
                "askUser": {"question": "ttl-default", "body": "A か B か", "escalation": "run#3"},
            },
            PostQuestion,
        ),
        ({"decision": "finish", "finish": {"readyOverview": False}}, FinishRun),
    ],
)
def test_ラン統括の判断はどれもそのコマンドになる(raw: dict[str, Any], expected: type):
    command = run_command(raw)
    assert type(command) is expected
    assert command.command_id == ID
    assert command.issuer == Issuer.run_supervisor(SESSION)


def test_中身はドメインの値に読み替える():
    insert = run_command(
        {
            "decision": "insert-task",
            "insertTask": {
                "spec": {"title": "引き継ぐ", "entryPoints": ["src/a.py"]},
                "blockedBy": ["task1"],
                "takesOver": "task2",
                "respondsTo": None,
            },
        }
    )
    assert isinstance(insert, InsertTask)
    assert insert.spec == TaskSpec("引き継ぐ", entry_points=("src/a.py",))
    assert insert.blocked_by == frozenset({T1})
    tested = run_command(
        {
            "decision": "insert-task",
            "insertTask": {
                "spec": {"title": "引き継ぐ", "taskTests": ["pytest a"]},
                "blockedBy": [],
                "takesOver": None,
                "respondsTo": None,
            },
        }
    )
    assert isinstance(tested, InsertTask)
    assert tested.spec.task_tests == (VerifyCommand("pytest a"),)
    plan = run_command(
        {
            "decision": "apply-plan",
            "applyPlan": {"design": 3, "stop": [], "discard": [], "respondsTo": None},
        }
    )
    assert isinstance(plan, ApplyReplan)
    assert plan.design == DesignVersion(3)
    replan = run_command(
        {"decision": "replan", "replan": {"reason": "x", "trigger": "run#2", "answer": "q1"}}
    )
    assert isinstance(replan, RequestReplan)
    assert (replan.trigger, replan.answer) == (EventId("run#2"), QuestionId("q1"))


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (None, "判断が返っていない"),
        ({"decision": "merge"}, "decision は"),
        ({"decision": "finish"}, "中身の欄 finish が無い"),
        (
            {"decision": "finish", "finish": {"readyOverview": True}, "replan": {"reason": "x"}},
            "ほかの中身の欄を書いた: replan",
        ),
        ({"decision": "finish", "finish": {"readyOverview": "yes"}}, "finish の中身が読めない"),
        (
            {"decision": "finish", "finish": {"readyOverview": True, "issuer": {}}},
            "driver が埋める",
        ),
        (
            {"decision": "stop-tasks", "stopTasks": {"tasks": ["Task1"], "respondsTo": None}},
            "stopTasks の中身が読めない",
        ),
    ],
)
def test_形の合わない判断は理由を付けて返す(raw: dict[str, Any] | None, reason: str):
    with pytest.raises(DecisionError, match=reason):
        run_command(raw)  # ty: ignore[invalid-argument-type]
