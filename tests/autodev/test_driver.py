"""組み立ての根（`app/driving/driver.py`）から、本物の集約・ポリシー・反応・メインループ・イベントストアで流れを通す。

外から入るものだけを偽物にする: ステージの実行（偽の実行器が test_seams の台本どおりに結果を返す）と
`claude -p` の統括（偽の AgentRuntime が決め打ちの判断の JSON を返す）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from autodev_drive import (
    PANIC,
    PLANNING,
    Rig,
    cut_base,
    is_task_supervisor,
    notice_of,
    rebased_onto,
    run_flow,
    supervisors,
)
from test_seams import (
    STUCK_PREFIX,
    Outcome,
    always_failing_close_script,
    main_script,
    planned,
    replan_script,
    stall_script,
)

from autodev.adapters.agent.types import AgentOutcome, Ending
from autodev.app.driving.driver import ExitCode, exit_code
from autodev.app.driving.mainloop import LoopExit
from autodev.app.driving.mainloop import Outcome as Outcome_
from autodev.app.stages.files import APPENDIX_MARKER
from autodev.app.stages.prompts.assets import asset_name, contract_inputs
from autodev.app.supervision.supervisors import MAX_ATTEMPTS, MAX_CORRECTIONS
from autodev.domain.aggregates.questions import Questions
from autodev.domain.aggregates.run import Run
from autodev.domain.aggregates.stack import Stack
from autodev.domain.aggregates.task import Task
from autodev.domain.commands.questions import AnswerQuestion
from autodev.domain.events.design import DesignSettled
from autodev.domain.events.questions import QuestionPosted
from autodev.domain.events.run import (
    EscalationAnswered,
    EscalationRaised,
    RunPanicked,
    RunResumed,
)
from autodev.domain.events.stack import GitJobDropped, GitJobRetried
from autodev.domain.events.task import (
    BaseRecorded,
    BranchRebased,
    EscalationResolved,
    WorktreeReady,
)
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.commit_sha import CommitSha
from autodev.domain.value_objects.deferred_call import DeferredCall
from autodev.domain.value_objects.design_version import DesignVersion
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.git_job_kind import GitJobKind
from autodev.domain.value_objects.interrupt_cause import InterruptCause
from autodev.domain.value_objects.limits import MAX_SUPERVISOR_FAILURES
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_status import TaskStatus
from autodev.infra.store.requests import RequestBox

S = StageKind
T1 = TaskId("task1")
T2 = TaskId("task2")
GIT = TaskId.git()


def aggregate(rig: Rig, stream: StreamId, cls: type[Any]) -> Any:
    assert rig.driver is not None
    found = rig.driver.loop.aggregates[stream]
    assert isinstance(found, cls)
    return found


def run_of(rig: Rig) -> Run:
    return aggregate(rig, StreamId.run(), Run)


def task_of(rig: Rig, task: TaskId) -> Task:
    return aggregate(rig, StreamId.task(task), Task)


def stacked(rig: Rig) -> list[TaskId]:
    return [entry.task for entry in aggregate(rig, StreamId.stack(), Stack).entries]


def events_of(rig: Rig, cls: type[Any]) -> list[Any]:
    assert rig.driver is not None
    return [d.event for d in rig.driver.loop.history if isinstance(d.event, cls)]


def notices(rig: Rig) -> list[tuple[str, str]]:
    """統括を起こした順に（誰か・知らせ）。"""
    return [
        ("task" if is_task_supervisor(call) else "run", notice["notice"])
        for call, notice in rig.runtime.calls
    ]


def assert_filled(name: str, text: str) -> None:
    """指示書の「入力」の表のプレースホルダが、どれも表に載っていて、埋め残しが無い。"""
    for placeholder in contract_inputs(name):
        assert f"| `<{placeholder}>` |" in text, (name, placeholder)
    assert "${" not in text


def test_主な流れはランの開始から仕上げまで組み立ての根から進み終了コード0で終える(tmp_path: Path):
    rig = Rig(tmp_path, main_script)
    assert rig.drive(start=True) == 0
    assert run_of(rig).complete
    assert stacked(rig) == [T1, T2]
    # must-fix 以外の設計の指摘を、確定した版の設計ファイルの末尾に書き足した
    settled: DesignVersion = events_of(rig, DesignSettled)[-1].proposal.design
    text = rig.paths.design(settled).read_text(encoding="utf-8")
    assert APPENDIX_MARKER in text
    assert "D2 [nit]" in text
    # LLM のステージのプロンプトは、指示書の入力をすべて埋めた
    stages = {execution.stage for execution, _ in rig.executor.prompts}
    assert {S.PLAN, S.DESIGN_REVIEW, S.DESIGN_JUDGE, S.REVISE, S.IMPL, S.REVIEW, S.JUDGE} <= stages
    for execution, prompt in rig.executor.prompts:
        assert_filled(asset_name(execution.stage), prompt.text)
        assert prompt.system_append is not None
    # 統括は、タスク 2 本の始まりと、全部が終わったときだけ起こされた
    assert sorted(notices(rig)) == [
        ("run", "all-settled"),
        ("task", "started"),
        ("task", "started"),
    ]
    for call, _ in rig.runtime.calls:
        name = "supervisor-task" if is_task_supervisor(call) else "supervisor-run"
        assert_filled(name, call.prompt or "")
        assert call.cwd == str(rig.paths.root)
        assert call.settings == str(rig.paths.guard)
    assert rig.paths.guard.is_file()


def ask_script(view, execution: ExecutionId) -> Outcome | None:
    """計画ステージが ask で止まり、ユーザーの回答で続きから再開する。"""
    if execution.stage is S.PLAN and not view.events(EscalationResolved):
        return Outcome(
            products=(),
            result={},
            exact=True,
            evidence={"result_valid": False, "deferred": DeferredCall("toolu_1", "A か B か")},
        )
    if execution.stage is S.PLAN:
        return Outcome(result={"tasks": [planned(1)]})
    return None


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_質問を出したら終了コード4で終え回答を置いて呼び直すと止まった実行を続きから終える(
    tmp_path: Path,
):
    rig = Rig(tmp_path, ask_script)
    assert rig.drive(start=True) == 4
    question = read_json(rig.paths.question(QuestionId("q-scope")))
    assert (question["body"], question["status"]) == ("A か B か", "open")
    with RequestBox.open(rig.paths.events_db) as box:
        box.put(AnswerQuestion, {"question": QuestionId("q-scope"), "answer": "A にする"})
    assert rig.drive() == 0
    # 回答のファイルを書いてから、止まった実行を続きから再開した
    assert read_json(rig.paths.answer("toolu_1")) == {"answer": "A にする"}
    question = read_json(rig.paths.question(QuestionId("q-scope")))
    assert (question["status"], question["answer"]) == ("answered", "A にする")
    assert task_of(rig, PLANNING).notes[0].is_human
    assert run_of(rig).complete
    # ラン統括は、控えたセッションの続きで回答を受けた
    run_calls = [call for call, _ in rig.runtime.calls if not is_task_supervisor(call)]
    assert [n for n in notices(rig) if n[0] == "run"] == [
        ("run", "escalation"),
        ("run", "answer"),
        ("run", "all-settled"),
    ]
    assert [call.resume for call in run_calls] == [False, True, True]
    assert len({call.session for call in run_calls}) == 1


def test_利用枠の上限に当たったら終了コード3で終え呼び直すと止めた実行を続ける(tmp_path: Path):
    hit: list[bool] = []

    def script(view, execution: ExecutionId) -> Outcome | None:
        if execution.stage is S.IMPL and not hit:
            hit.append(True)
            return PANIC
        return main_script(view, execution)

    rig = Rig(tmp_path, script)
    assert rig.drive(start=True) == 3
    assert run_of(rig).panicked
    impl = ExecutionId(T1, S.IMPL, 0, 1)
    assert task_of(rig, T1).executions[impl].interrupted_by is InterruptCause.PANIC
    # 走っていた実行を止めるよう、実行器に頼んだ
    assert rig.executor.interrupted == [impl]
    assert rig.drive() == 0
    assert events_of(rig, RunResumed)
    assert run_of(rig).complete
    assert stacked(rig) == [T1, T2]


def test_統括が利用枠の上限に当たったら知らせを控えに残し呼び直すと起こし直す(tmp_path: Path):
    limited: list[bool] = []

    def decide(call, notice):
        if notice["notice"] == "all-settled" and not limited:
            limited.append(True)
            return AgentOutcome(
                ending=Ending.RESULT,
                exit_code=1,
                session=call.session,
                is_error=True,
                rate_limited=True,
            )
        return supervisors(call, notice)

    rig = Rig(tmp_path, main_script, decide)
    assert rig.drive(start=True) == 3
    assert not run_of(rig).finished
    # パニックの間に BeginStage を受け取り損ねた実行も、呼び直した driver が始め直す
    assert rig.drive() == 0
    assert run_of(rig).complete
    assert [n for n in notices(rig) if n[0] == "run"] == [("run", "all-settled")] * 2


def test_呼び直した後に同じ知らせでまた利用枠に当たったらもう一度パニックにして終了コード3(
    tmp_path: Path,
):
    limited: list[bool] = []

    def decide(call, notice):
        if notice["notice"] == "all-settled" and len(limited) < 2:
            limited.append(True)
            return AgentOutcome(
                ending=Ending.RESULT,
                exit_code=1,
                session=call.session,
                is_error=True,
                rate_limited=True,
            )
        return supervisors(call, notice)

    rig = Rig(tmp_path, main_script, decide)
    assert rig.drive(start=True) == 3
    assert rig.drive() == 3
    assert len(events_of(rig, RunPanicked)) == 2
    assert rig.drive() == 0
    assert run_of(rig).complete


def test_タスク統括が応じずラン統括が続けてと答え続けたら上限を超えたところでラン統括がユーザーに聞いて答える(
    tmp_path: Path,
):
    stuck: list[str] = []

    def decide(call, notice):
        if is_task_supervisor(call) and "task-task1" in call.log_path:
            if notice["notice"] == "retry" and notice["answer"] == "直した":
                # 起こし直しの入れ子は 1 段だけで、最初の知らせが載る
                assert notice["retry"]["notice"] == "started"
                assert "retry" not in notice["retry"]
                return run_flow()
            return AgentOutcome(ending=Ending.NO_RESULT, exit_code=1, session=call.session)
        if not is_task_supervisor(call):
            if notice.get("kind") == "supervisor-failed":
                stuck.append(notice["id"])
                return {
                    "decision": "answer",
                    "answer": {"escalation": notice["id"], "answer": "続けて", "question": None},
                }
            if notice["notice"] == "decision-rejected" and "ask-user" in notice["reason"]:
                body = {"question": "t1-stuck", "body": "続けるか", "escalation": stuck[-1]}
                return {"decision": "ask-user", "askUser": body}
            if notice["notice"] == "answer":
                answer = {
                    "escalation": notice["escalation"],
                    "answer": None,
                    "question": "t1-stuck",
                }
                return {"decision": "answer", "answer": answer}
        return supervisors(call, notice)

    rig = Rig(tmp_path, main_script, decide)
    assert rig.drive(start=True) == 4
    failed = [
        e for e in events_of(rig, EscalationRaised) if e.kind is EscalationKind.SUPERVISOR_FAILED
    ]
    assert [e.failures for e in failed] == list(range(1, MAX_SUPERVISOR_FAILURES + 2))
    # 上限を超えた上げも、ラン統括が受けた（段を飛ばさない）。質問はラン統括が出したもの
    run_failed = [n for c, n in rig.runtime.calls if not is_task_supervisor(c)]
    assert sum(n.get("kind") == "supervisor-failed" for n in run_failed) == len(failed)
    assert not list(rig.paths.questions.glob("supervisor-failed-*.json"))
    assert read_json(rig.paths.questions / "t1-stuck.json")["escalation"] == stuck[-1]
    with RequestBox.open(rig.paths.events_db) as box:
        box.put(AnswerQuestion, {"question": QuestionId("t1-stuck"), "answer": "直した"})
    assert rig.drive() == 0
    assert stacked(rig) == [T1, T2]
    (answered,) = [e for e in events_of(rig, EscalationAnswered) if e.question is not None]
    assert (answered.answer, answered.task) == ("直した", T1)


def test_形の合わない判断と拒まれた判断は理由を付けて同じセッションに差し戻す(tmp_path: Path):
    answers = iter(
        [
            # decision と中身の欄が合わない
            {"decision": "run-flow", "escalate": {"kind": "needs-human"}},
            # 形は合うが、Task が拒む（開いていないエスカレーションに応える）
            run_flow(responds_to="task/task1#99"),
        ]
    )

    def decide(call, notice):
        if "task-task1" in call.log_path:
            return next(answers, None) or supervisors(call, notice)
        return supervisors(call, notice)

    rig = Rig(tmp_path, main_script, decide)
    assert rig.drive(start=True) == 0
    task1 = [(call, n) for call, n in rig.runtime.calls if "task-task1" in call.log_path]
    assert [n["notice"] for _, n in task1] == ["started", "decision-rejected", "decision-rejected"]
    assert "中身の欄 runFlow が無い" in task1[1][1]["reason"]
    assert "開いているエスカレーションではない" in task1[2][1]["reason"]
    assert [call.resume for call, _ in task1] == [False, True, True]
    assert len({call.session for call, _ in task1}) == 1
    assert stacked(rig) == [T1, T2]


def test_進められないのに回答も待っていなければ終了コード1で終える():
    idle = Outcome_(LoopExit.IDLE)
    assert exit_code(idle, {}) == ExitCode.FAILED
    questions = Questions(StreamId.questions())
    questions.apply(QuestionPosted(QuestionId("q1"), "A か B か"), CommandId("c1"))
    assert exit_code(idle, {StreamId.questions(): questions}) == ExitCode.AWAITING_ANSWER


def test_ラン統括が応じなければユーザーに聞き回答を置いて呼び直すと新しいセッションで同じ知らせから起こし直す(
    tmp_path: Path,
):
    def decide(call, notice):
        if is_task_supervisor(call):
            return supervisors(call, notice)
        if notice["notice"] == "retry":
            # 元の知らせと、ユーザーの回答が載る
            assert notice["retry"]["notice"] == "all-settled"
            assert notice["answer"] == "枠を増やした"
            return {"decision": "finish", "finish": {"readyOverview": True}}
        return {"decision": "nope"}

    rig = Rig(tmp_path, main_script, decide)
    assert rig.drive(start=True) == 4
    run_calls = [call for call, _ in rig.runtime.calls if not is_task_supervisor(call)]
    assert len(run_calls) == 1 + MAX_CORRECTIONS
    (path,) = rig.paths.questions.glob("supervisor-failed-*.json")
    question = read_json(path)
    assert question["status"] == "open"
    assert "応じなかった" in question["body"]
    with RequestBox.open(rig.paths.events_db) as box:
        box.put(AnswerQuestion, {"question": QuestionId(path.stem), "answer": "枠を増やした"})
    assert rig.drive() == 0
    assert run_of(rig).complete
    run_calls = [(c, n) for c, n in rig.runtime.calls if not is_task_supervisor(c)]
    retry, notice = run_calls[-1]
    assert notice["notice"] == "retry"
    # 起こし直すのは新しいセッションで、続きからではない
    assert not retry.resume
    assert retry.session not in {c.session for c, _ in run_calls[:-1]}
    assert run_of(rig).escalations == {}


def test_タスク統括が判断を返さずに終わり続けたらラン統括へ上げ答えると新しいセッションで起こし直す(
    tmp_path: Path,
):
    def decide(call, notice):
        if is_task_supervisor(call) and "task-task1" in call.log_path:
            if notice["notice"] == "retry":
                assert notice["retry"]["notice"] == "started"
                assert notice["answer"] == "フローを組み直して"
                return run_flow()
            # StructuredOutput を返さずに普通に終わった。落ちたのではなく、差し戻す
            return AgentOutcome(ending=Ending.RESULT, exit_code=0, session=call.session)
        if not is_task_supervisor(call) and notice.get("kind") == "supervisor-failed":
            assert notice["task"] == "task1"
            return {
                "decision": "answer",
                "answer": {
                    "escalation": notice["id"],
                    "answer": "フローを組み直して",
                    "question": None,
                },
            }
        return supervisors(call, notice)

    rig = Rig(tmp_path, main_script, decide)
    assert rig.drive(start=True) == 0
    task1 = [(c, n) for c, n in rig.runtime.calls if "task-task1" in c.log_path]
    assert [n["notice"] for _, n in task1] == [
        "started",
        *["decision-rejected"] * MAX_CORRECTIONS,
        "retry",
    ]
    assert "判断が返っていない" in task1[1][1]["reason"]
    # 差し戻しは同じセッションの続き、起こし直しは新しいセッション
    assert len({c.session for c, _ in task1[:-1]}) == 1
    assert task1[-1][0].session != task1[0][0].session
    assert not task1[-1][0].resume
    # ラン統括の言葉は起こし直す知らせに載り、受入条件の判断（notes）には残らない
    assert task_of(rig, T1).notes == []
    assert stacked(rig) == [T1, T2]


def test_タスク統括が新しいセッションでも落ちたらラン統括へ上げる(tmp_path: Path):
    def decide(call, notice):
        if is_task_supervisor(call) and "task-task1" in call.log_path:
            if notice["notice"] == "retry":
                return run_flow()
            return AgentOutcome(ending=Ending.NO_RESULT, exit_code=1, session=call.session)
        if not is_task_supervisor(call) and notice.get("kind") == "supervisor-failed":
            assert "落ち続けた" in notice["reason"]
            return {
                "decision": "answer",
                "answer": {"escalation": notice["id"], "answer": "続けて", "question": None},
            }
        return supervisors(call, notice)

    rig = Rig(tmp_path, main_script, decide)
    assert rig.drive(start=True) == 0
    task1 = [c for c, _ in rig.runtime.calls if "task-task1" in c.log_path]
    # 続けたセッションで 2 回、新しいセッションでもう 1 回。上げて答えを受けたら、また新しいセッション
    assert len(task1) == MAX_ATTEMPTS + 1 + 1
    assert task1[0].session == task1[1].session
    assert len({c.session for c in task1}) == 3
    # 新しいセッションでは、差し戻しではなく知らせから起こし直す
    assert notice_of(task1[2].prompt)["notice"] == "started"
    assert stacked(rig) == [T1, T2]


def table_value(prompt: str, placeholder: str) -> str:
    found = re.search(rf"\| `<{placeholder}>` \| `([^`]*)` \|", prompt)
    assert found is not None, (placeholder, prompt)
    return found.group(1)


def test_プロンプトに埋める根元と読む所と設計の版はドメインの問いから取る(tmp_path: Path):
    rig = Rig(tmp_path, main_script)
    assert rig.drive(start=True) == 0
    prompts = {e: p.text for e, p in rig.executor.prompts}
    cut_jobs = {
        e.job.task: e.job.id
        for e in events_of(rig, WorktreeReady)
        if e.job is not None and e.job.kind is GitJobKind.CUT_TASK
    }
    for execution, text in prompts.items():
        if execution.stage in (S.IMPL, S.REVIEW, S.JUDGE, S.WRITE_PR_BODY):
            # タスクのブランチを切った元のコミット（CutBranch の結果）
            expected = cut_base(cut_jobs[execution.task])
            assert table_value(text, "基準のコミット") == expected, execution
    overview = str(rig.paths.overview_tree)
    reviews = [t for e, t in prompts.items() if e.stage is S.DESIGN_REVIEW]
    # 初回の計画は、計画タスクのために切った概要の worktree を読む
    assert table_value(reviews[0], "コードの置き場") == overview
    # 設計の段のステージは確定前の提案の版を（1 つ目は Plan の提案、2 つ目は Revise の提案）、
    # 実装のステージは確定した設計の版を読む
    assert [table_value(t, "設計") for t in reviews] == [
        str(rig.paths.design(DesignVersion(n))) for n in range(1, len(reviews) + 1)
    ]
    impl = next(t for e, t in prompts.items() if e.stage is S.IMPL)
    design = task_of(rig, T1).artifacts[ArtifactKind.DESIGN]
    assert table_value(impl, "設計") == str(rig.paths.design(DesignVersion(int(design.at))))
    assert design.at == str(len(reviews))
    # タスクの一覧には、実装タスクだけが番号の順に載る
    overview_prompt = next(t for e, t in prompts.items() if e.stage is S.WRITE_OVERVIEW)
    assert [entry["id"] for entry in section_json(overview_prompt, "タスクの一覧")] == [
        "task1",
        "task2",
    ]
    # git 管理タスクのステージが扱うタスクは、処理している仕事の相手
    resolve = next(t for e, t in prompts.items() if e.stage is S.RESOLVE_CONFLICT and e.task == GIT)
    assert section_json(resolve, "タスク")["id"] == "task2"


def section_json(prompt: str, placeholder: str) -> Any:
    found = re.search(rf"## `<{placeholder}>`\s*```json\n(.*?)\n```", prompt, re.DOTALL)
    assert found is not None, (placeholder, prompt)
    return json.loads(found.group(1))


def insert_responding(escalation: str) -> dict[str, Any]:
    """ラン統括の、エスカレーションに差し込みで応える判断（答え以外で閉じる）。"""
    spec = {
        "title": "別の作業",
        "dod": "",
        "acceptance": [],
        "scope": [],
        "entryPoints": [],
        "boundary": "",
        "taskTests": [],
    }
    return {
        "decision": "insert-task",
        "insertTask": {"spec": spec, "blockedBy": [], "takesOver": None, "respondsTo": escalation},
    }


def test_やめられない止めた仕事を答え以外で閉じたら拒まれて差し戻され答えると続ける(tmp_path: Path):
    """破棄の仕事が落ち続けて止まった。ラン統括が差し込みで閉じようとすると Run が拒み、理由を添えて
    同じセッションに差し戻す。答え直すと止めた仕事を続け（RetryGitJob）、ランが終わる。"""
    replanned: list[bool] = []
    stuck: list[str] = []
    rejected: list[str] = []

    def decide(call, notice):
        if is_task_supervisor(call):
            return supervisors(call, notice)
        kind = notice["notice"]
        if kind == "all-settled" and not replanned:
            replanned.append(True)
            return {
                "decision": "replan",
                "replan": {"reason": "task1 を作り直す", "trigger": None, "answer": None},
            }
        if kind == "escalation" and notice.get("task") == "git":
            if notice["reason"].startswith(STUCK_PREFIX):
                assert notice["answerOnly"] is True
                stuck.append(notice["id"])
            # 落ちたステージの上げも止めた仕事の上げも、まず差し込みで（答え以外で）閉じようとする
            return insert_responding(notice["id"])
        if kind == "decision-rejected" and stuck:
            rejected.append(notice["reason"])
            return {
                "decision": "answer",
                "answer": {"escalation": stuck[-1], "answer": "続ける", "question": None},
            }
        return supervisors(call, notice)

    rig = Rig(tmp_path, always_failing_close_script, decide)
    assert rig.drive(start=True) == 0
    assert run_of(rig).complete
    assert len(stuck) == 1
    assert len(rejected) == 1 and "答え以外では閉じられない" in rejected[0]
    (retried,) = events_of(rig, GitJobRetried)
    assert retried.job.kind is GitJobKind.DISCARD
    assert events_of(rig, GitJobDropped) == []


def test_積み直しで根元が動いたらタスクの根元は載せ直した先になる(tmp_path: Path):
    asked: list[bool] = []

    def decide(call, notice):
        if notice["notice"] == "all-settled" and not asked:
            asked.append(True)
            return {
                "decision": "replan",
                "replan": {"reason": "task1 を作り直す", "trigger": None, "answer": None},
            }
        return supervisors(call, notice)

    rig = Rig(tmp_path, replan_script, decide)
    assert rig.drive(start=True) == 0
    rebased = [e for e in events_of(rig, BranchRebased) if e.task == T2]
    assert len(rebased) >= 2
    assert task_of(rig, T2).base_commit == rebased[-1].onto
    assert task_of(rig, T2).base_commit == CommitSha(rebased_onto(rebased[-1].job.id))
    # 積み直しの CutBranch が切った元（前に積んだブランチの先端）は、根元にしない
    restacked = [
        e for e in events_of(rig, WorktreeReady) if e.job is not None and e.job.previous is not None
    ]
    assert restacked
    recorded = {e.base for e in events_of(rig, BaseRecorded)}
    assert not {e.base for e in restacked} & recorded


def test_再計画を頼んだら確定した設計でラン統括を起こしapply_planで反映する(tmp_path: Path):
    asked: list[bool] = []

    def decide(call, notice):
        if notice["notice"] == "all-settled" and not asked:
            asked.append(True)
            return {
                "decision": "replan",
                "replan": {"reason": "task1 を作り直す", "trigger": None, "answer": None},
            }
        return supervisors(call, notice)

    rig = Rig(tmp_path, replan_script, decide)
    assert rig.drive(start=True) == 0
    assert run_of(rig).tasks[T1].status is TaskStatus.DISCARDED
    assert stacked(rig) == [T2]
    assert [n for n in notices(rig) if n[0] == "run"] == [
        ("run", "all-settled"),
        ("run", "settled-replan"),
        ("run", "all-settled"),
    ]
    # Replan のプロンプトに、ラン統括が書いた再計画の理由と、stack-top の置き場が入った
    (replan,) = [p for e, p in rig.executor.prompts if e.stage is S.REPLAN]
    assert "task1 を作り直す" in replan.text
    assert str(rig.paths.stack_top_tree) in replan.text


def test_停滞はタスク統括が上げラン統括が答えるとタスクへ下りて続く(tmp_path: Path):
    def decide(call, notice):
        if notice["notice"] != "escalation":
            return supervisors(call, notice)
        if is_task_supervisor(call):
            return {
                "decision": "escalate",
                "escalate": {
                    "kind": "needs-human",
                    "reason": notice["reason"] or "直らない",
                    "pointers": notice["pointers"],
                    "hint": notice["hint"],
                    "source": notice["id"],
                },
            }
        return {
            "decision": "answer",
            "answer": {
                "escalation": notice["id"],
                "answer": "テストを直してよい",
                "question": None,
            },
        }

    rig = Rig(tmp_path, stall_script, decide)
    assert rig.drive(start=True) == 0
    assert notices(rig) == [
        ("task", "started"),
        ("task", "escalation"),
        ("run", "escalation"),
        ("run", "all-settled"),
    ]
    assert [note.text for note in task_of(rig, T1).notes] == ["テストを直してよい"]
    # ラン統括へ上げた知らせには、Judge の停滞の分類と理由が載った
    raised = next(n for c, n in rig.runtime.calls if not is_task_supervisor(c))
    assert raised["hint"]["stallCause"] == "tests"
    assert raised["reason"] == "テストが受入条件と合わない"


def test_新しいラン名に指示が無いか既にあるラン名に指示を足したら終了コード1(tmp_path: Path):
    rig = Rig(tmp_path, main_script)
    assert rig.drive() == 1
    assert rig.drive(start=True) == 0
    assert rig.drive(start=True) == 1


def test_終えたランを呼び直しても終了コード0のまま(tmp_path: Path):
    rig = Rig(tmp_path, main_script)
    assert rig.drive(start=True) == 0
    calls = len(rig.runtime.calls)
    assert rig.drive() == 0
    assert len(rig.runtime.calls) == calls
