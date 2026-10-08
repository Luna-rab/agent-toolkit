"""実行器（`app/stages/executor.py`）。偽の AgentRuntime と、tmp_path の本物の git リポジトリで確かめる。

実行器が返したコマンドを本物の Task 集約に通し、集めた証拠から Task が決めた結果（完了・失敗・
エスカレーション）までを見る。実行器は判断しないので、結果を変えるのは証拠だけである。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from autodev_harness import POLICY, SESSION, new_id, of_type
from executor_fakes import Env, FakeProcess, commit, make_env, outcome, sh

from autodev.adapters.agent.types import AgentCall, DeferredToolUse, Ending, Progress
from autodev.adapters.claude.guard import GUARD_ENV
from autodev.app.stages import executor as executor_module
from autodev.app.stages.executor import SHAPE_CORRECTIONS
from autodev.app.stages.prompts.render import Prompts
from autodev.app.stages.stage_context import ResumeMode, StageContext
from autodev.domain.commands.run import (
    ApplyPlan,
    Panic,
    RecordSettledPlan,
    StartRun,
    StartTask,
)
from autodev.domain.commands.task import (
    AcceptFlow,
    ConcludeReviewRound,
    ConfirmHandoff,
    InterruptStage,
    OpenTask,
    RecordBase,
    ReportBeginFailure,
    ReportStageResult,
    ResolveEscalation,
    ResumeStage,
)
from autodev.domain.events.run import EscalationRaised
from autodev.domain.events.task import (
    ExecutionRestarted,
    GateFailed,
    StageCompleted,
    StageDeferred,
    StageFailed,
    StageRequested,
    StageStarted,
)
from autodev.domain.flow.flow import FlowStep, Reviewers
from autodev.domain.flow.standard import git_job_flow, planning_flow
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.artifact_ref import ArtifactRef
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.commit_sha import CommitSha
from autodev.domain.value_objects.design_version import DesignVersion
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.gate_item import GateItem
from autodev.domain.value_objects.git_job import GitJob
from autodev.domain.value_objects.git_job_kind import GitJobKind
from autodev.domain.value_objects.glob_pattern import GlobPattern
from autodev.domain.value_objects.instruction import Instruction
from autodev.domain.value_objects.interrupt_cause import InterruptCause
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.parallel_limit import ParallelLimit
from autodev.domain.value_objects.proposal import Proposal
from autodev.domain.value_objects.repository import Repository
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.stage_exit import StageExit
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_kind import TaskKind
from autodev.domain.value_objects.task_spec import TaskSpec
from autodev.domain.value_objects.verify_command import VerifyCommand
from autodev.domain.value_objects.verify_kind import VerifyKind
from conftest import AUTODEV_PACKAGE

S = StageKind
A = ArtifactKind
T1 = TaskId("task1")
BRANCH = BranchName("stack/r--task-1")
OVERVIEW = BranchName("stack/r--task-0")
PLANNING = TaskId.planning()
IMPL_FLOW = (
    FlowStep(S.IMPL),
    FlowStep(S.REVIEW_LOOP, reviewers=Reviewers((S.REVIEW,))),
    FlowStep(S.GATE),
    FlowStep(S.WRITE_PR_BODY),
)
IMPL_RESULT = {
    "report": None,
    "reportReason": None,
    "unchanged": False,
    "comments": [],
    "notes": "",
}
PLAN_RESULT = {
    "design": "# 設計\n",
    "codemap": "# コードマップ\n",
    "tasks": [],
    "quickChecks": [],
    "regressionTests": [],
    "decisions": [],
    "deferrals": [],
}


def ex(stage: StageKind, round: int = 0, attempt: int = 1, task: TaskId = T1) -> ExecutionId:
    return ExecutionId(task, stage, round, attempt)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return make_env(tmp_path, monkeypatch)


def impl_task(
    env: Env,
    *steps: FlowStep,
    verify: tuple[str, ...] = (),
    artifacts: tuple[ArtifactRef, ...] = (),
) -> list:
    """trees/task1 を切り、実装タスクを開いてフローを受け入れる。"""
    env.git.create_branch(BRANCH, "main")
    env.git.add_worktree(env.paths.task_tree(T1), BRANCH)
    env.world(
        OpenTask(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            kind=TaskKind.IMPLEMENTATION,
            spec=TaskSpec("キャッシュ", task_tests=tuple(VerifyCommand(v) for v in verify)),
            artifacts=(ArtifactRef(A.DESIGN, "1"), *artifacts),
            branch=BRANCH,
        )
    )
    return env.world(
        AcceptFlow(
            command_id=new_id(),
            issuer=Issuer.task_supervisor(T1, SESSION),
            task=T1,
            steps=steps or IMPL_FLOW,
        )
    )


def planning_task(env: Env) -> list:
    env.git.create_branch(OVERVIEW, "origin/main")
    env.git.add_worktree(env.paths.overview_tree, OVERVIEW)
    env.world(OpenTask(command_id=new_id(), issuer=POLICY, task=PLANNING, kind=TaskKind.PLANNING))
    return env.world(
        AcceptFlow(
            command_id=new_id(),
            issuer=Issuer.task_supervisor(PLANNING),
            task=PLANNING,
            steps=planning_flow(settled_before=False),
        )
    )


def commits_and_returns(result: dict, path: str = "src/cache.py"):
    def behave(call: AgentCall, process: FakeProcess):
        commit(Path(call.cwd), path, f"{time.time()}\n")
        return outcome(call, result)

    return behave


def reports(env: Env) -> list[ReportStageResult]:
    return [c for c in env.world.submitted() if isinstance(c, ReportStageResult)]


# --- LLM のステージ ---


def test_Implはworktreeで起動しコミットを実物にして完了する(env: Env):
    events = impl_task(env)
    assert [e.execution for e in of_type(events, StageRequested)] == [ex(S.IMPL)]
    head = sh(env.paths.task_tree(T1), "rev-parse", "HEAD").strip()
    started = of_type(env.begin(ex(S.IMPL)), StageStarted)
    assert started[0].session == env.sessions[0]
    assert str(started[0].start_commit) == head

    env.runtime.behaviors.append(commits_and_returns(IMPL_RESULT))
    events = env.run(ex(S.IMPL))

    (completed,) = of_type(events, StageCompleted)
    new_head = sh(env.paths.task_tree(T1), "rev-parse", "HEAD").strip()
    assert completed.produced == (ArtifactRef(A.IMPL, new_head),)
    (call,) = env.runtime.calls
    assert call.cwd == str(env.paths.task_tree(T1))
    assert call.resume is False and call.session == env.sessions[0]
    assert call.prompt == f"プロンプト {ex(S.IMPL)}" and call.system_append == "必須ルール"
    assert json.loads(call.json_schema or "") == json.loads(
        (AUTODEV_PACKAGE / "schemas" / "impl.json").read_text(encoding="utf-8")
    )
    assert call.settings == str(env.paths.guard) and env.paths.guard.is_file()
    guard = json.loads(call.env[GUARD_ENV])
    assert guard["guard"]["writes"] == "non-tests"
    assert guard["context"]["tree"] == str(env.paths.task_tree(T1))
    # Impl は implement のクラス。設定を渡さないランは既定の sonnet / medium
    assert (call.model, call.effort) == ("sonnet", "medium")
    # 結果の JSON は results/ に残り、進み具合は消える
    assert (env.paths.task_results(T1) / f"{ex(S.IMPL)}.json").is_file()
    assert not any(env.paths.progress.glob("*.json"))


def test_コミットしなかったImplは実物が無いので失敗になる(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    env.runtime.behaviors.append(lambda call, p: outcome(call, IMPL_RESULT))
    (failed,) = of_type(env.run(ex(S.IMPL)), StageFailed)
    assert "impl の実物が無い" in failed.reason


def keeps_and_returns(path: str | None = None):
    def behave(call: AgentCall, process: FakeProcess):
        if path is not None:
            target = Path(call.cwd) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x\n", encoding="utf-8")
        return outcome(call, {**IMPL_RESULT, "unchanged": True})

    return behave


def test_初めてのImplが変えないと返し作業ツリーが汚れていなければHEADを実物にして完了する(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    env.runtime.behaviors.append(keeps_and_returns())
    head = sh(env.paths.task_tree(T1), "rev-parse", "HEAD").strip()
    (completed,) = of_type(env.run(ex(S.IMPL)), StageCompleted)
    assert completed.produced == (ArtifactRef(A.IMPL, head),)


def test_初めてのImplが変えないと返しても未追跡のファイルがあれば失敗にする(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    env.runtime.behaviors.append(keeps_and_returns("src/new.py"))
    (failed,) = of_type(env.run(ex(S.IMPL)), StageFailed)
    assert "impl の実物が無い" in failed.reason
    assert "コミットしていない変更" in failed.reason and "src/new.py" in failed.reason


def test_初めてのTestGenが変えないと返しても軽い検査が作業ツリーを汚せば失敗にする(env: Env):
    check = QuickCheck(env)
    check.command = "echo x > generated.txt"
    open_testgen_task(env, check)
    first = ex(S.TEST_GEN)
    env.begin(first)
    env.runtime.behaviors.append(
        lambda call, p: outcome(call, {**TESTGEN_RESULT, "unchanged": True})
    )
    (failed,) = of_type(env.run(first), StageFailed)
    assert "tests の実物が無い" in failed.reason
    assert "generated.txt" in failed.reason


def test_初めてのTestGenが変えないと返し軽い検査も作業ツリーを汚さなければHEADを実物にして完了する(
    env: Env,
):
    check = QuickCheck(env)
    open_testgen_task(env, check)
    first = ex(S.TEST_GEN)
    env.begin(first)
    env.runtime.behaviors.append(
        lambda call, p: outcome(call, {**TESTGEN_RESULT, "unchanged": True})
    )
    head = sh(env.paths.task_tree(T1), "rev-parse", "HEAD").strip()
    (completed,) = of_type(env.run(first), StageCompleted)
    assert completed.produced == (ArtifactRef(A.TESTS, head),)


def test_報告の欄の文字列のnullはnullに直してから形を確かめる(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    env.runtime.behaviors.append(
        commits_and_returns({**IMPL_RESULT, "report": "null", "reportReason": "none"})
    )
    assert of_type(env.run(ex(S.IMPL)), StageCompleted)


def test_返し直させても形が合わない結果は形の誤りとして渡す(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    broken = {k: v for k, v in IMPL_RESULT.items() if k != "notes"}
    env.runtime.behaviors.append(commits_and_returns(broken))
    for _ in range(SHAPE_CORRECTIONS):
        env.runtime.behaviors.append(lambda call, p: outcome(call, broken))
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    env.executor.join()
    (report,) = reports(env)
    assert report.evidence.result_valid is False and report.result is None
    assert "notes が無い" in (report.evidence.error or "")
    assert len(env.runtime.calls) == 1 + SHAPE_CORRECTIONS
    # 形が違っても、調べる先として結果は残す
    assert (env.paths.task_results(T1) / f"{ex(S.IMPL)}.json").is_file()


def test_形だけが違う結果は同じセッションに違いを伝えて返し直させ直れば完了する(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    broken = {k: v for k, v in IMPL_RESULT.items() if k != "notes"}
    env.runtime.behaviors.append(commits_and_returns(broken))
    env.runtime.behaviors.append(lambda call, p: outcome(call, IMPL_RESULT))
    events = env.run(ex(S.IMPL))

    (completed,) = of_type(events, StageCompleted)
    head = sh(env.paths.task_tree(T1), "rev-parse", "HEAD").strip()
    assert completed.produced == (ArtifactRef(A.IMPL, head),)
    first, again = env.runtime.calls
    assert (again.session, again.resume) == (first.session, True)
    assert again.json_schema == first.json_schema
    assert "notes が無い" in (again.prompt or "")


@pytest.mark.parametrize(
    "fields",
    [
        {"is_error": True, "text": "落ちた"},
        {"ending": Ending.NO_RESULT, "initialized": False},
    ],
    ids=["エラーで終わる", "続けられない"],
)
def test_返し直しが正しく終わらなければ最初の結果で判断する(env: Env, fields: dict):
    impl_task(env)
    env.begin(ex(S.IMPL))
    broken = {k: v for k, v in IMPL_RESULT.items() if k != "notes"}
    env.runtime.behaviors.append(commits_and_returns(broken))
    env.runtime.behaviors.append(lambda call, p: outcome(call, None, **fields))
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    env.executor.join()
    (report,) = reports(env)
    assert "notes が無い" in (report.evidence.error or "")
    # 最初の起動は新しいセッションだったので、続けられなかったと数えて作り直さない
    assert report.evidence.resumed is False and report.evidence.initialized is True
    assert len(env.runtime.calls) == 2


def test_返し直しの途中でaskしたら最初の結果より先に質問を渡す(env: Env):
    planning_task(env)
    env.begin(ex(S.PREPARE, task=PLANNING))
    env.run(ex(S.PREPARE, task=PLANNING))
    plan = ex(S.PLAN, task=PLANNING)
    env.begin(plan)
    ask = DeferredToolUse(
        "toolu_01", "Bash", {"command": "/s/bin/autodev ask --question 'どちら？'"}
    )
    env.runtime.behaviors.append(lambda call, p: outcome(call, None))
    env.runtime.behaviors.append(
        lambda call, p: outcome(call, None, stop_reason="tool_deferred", deferred=ask)
    )
    events = env.run(plan)
    (deferred,) = of_type(events, StageDeferred)
    assert deferred.tool_use_id == "toolu_01"
    (raised,) = of_type(events, EscalationRaised)
    assert raised.kind is EscalationKind.ASK and raised.reason == "どちら？"


def test_返し直しのwaitが例外を投げたら最初の結果で判断する(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    broken = {k: v for k, v in IMPL_RESULT.items() if k != "notes"}
    env.runtime.behaviors.append(commits_and_returns(broken))

    def breaks(call: AgentCall, process: FakeProcess):
        raise OSError("ログに書けない")

    env.runtime.behaviors.append(breaks)
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    env.executor.join()
    (report,) = reports(env)
    assert "notes が無い" in (report.evidence.error or "")


def test_返し直したときフックに拒まれた数は足さず多いほうを渡す(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    broken = {k: v for k, v in IMPL_RESULT.items() if k != "notes"}

    def first(call: AgentCall, process: FakeProcess):
        commit(Path(call.cwd), "src/cache.py", f"{time.time()}\n")
        return outcome(call, broken, hook_denials=3)

    env.runtime.behaviors.append(first)
    env.runtime.behaviors.append(lambda call, p: outcome(call, IMPL_RESULT, hook_denials=2))
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    env.executor.join()
    (report,) = reports(env)
    assert report.evidence.result_valid is True
    assert report.evidence.hook_denials == 3


def test_返し直しで利用枠の上限に当たったらPanicを返す(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    env.runtime.behaviors.append(lambda call, p: outcome(call, None))
    env.runtime.behaviors.append(
        lambda call, p: outcome(call, None, is_error=True, rate_limited=True, text="usage limit")
    )
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    env.executor.join()
    assert any(isinstance(c, Panic) for c in env.world.submitted())
    assert reports(env) == []


@pytest.mark.parametrize(
    "fields",
    [{"is_error": True, "text": "落ちた"}, {"initialized": False}],
    ids=["エラーで終わった", "initを受けていない"],
)
def test_形の違い以外で終わった結果は返し直させない(env: Env, fields: dict):
    impl_task(env)
    env.begin(ex(S.IMPL))
    env.runtime.behaviors.append(lambda call, p: outcome(call, None, **fields))
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    env.executor.join()
    assert len(env.runtime.calls) == 1


def test_利用枠の上限に当たったら証拠ではなくPanicを返す(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    env.runtime.behaviors.append(
        lambda call, p: outcome(call, None, is_error=True, rate_limited=True, text="usage limit")
    )
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    env.executor.join()
    (panic,) = env.world.submitted()
    assert isinstance(panic, Panic) and "利用枠の上限" in panic.cause


def test_フックに止められ続けたら打ち切ってエラーの証拠にする(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))

    def behave(call: AgentCall, process: FakeProcess):
        assert process.on_progress is not None
        process.on_progress(Progress(turns=3, last_tool="Bash", hook_denials=11, events=40))
        return outcome(
            call,
            None,
            is_error=True,
            subtype="error_during_execution",
            interrupted=process.interrupted,
            hook_denials=11,
        )

    env.runtime.behaviors.append(behave)
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    env.executor.join()
    (report,) = reports(env)
    assert report.evidence.exit is StageExit.ERROR
    assert report.evidence.hook_denials == 11
    assert "フックに 11 回止められた" in (report.evidence.error or "")


def rewrite_overview(env: Env) -> ExecutionId:
    """概要 PR を書き直す仕事を git 管理タスクに渡し、WriteOverview を始める。"""
    env.git.create_branch(OVERVIEW, "origin/main")
    env.git.add_worktree(env.paths.overview_tree, OVERVIEW)
    git = TaskId.git()
    job = GitJob(1, GitJobKind.REWRITE_OVERVIEW, branch=OVERVIEW, base=BranchName("main"))
    env.world(OpenTask(command_id=new_id(), issuer=POLICY, task=git, kind=TaskKind.GIT))
    env.world(
        AcceptFlow(
            command_id=new_id(),
            issuer=Issuer.task_supervisor(git),
            task=git,
            steps=git_job_flow(job),
            job=job,
        )
    )
    execution = ex(S.WRITE_OVERVIEW, task=git)
    env.begin(execution)
    return execution


def test_WriteOverviewのタイトルと本文を書き出す(env: Env):
    execution = rewrite_overview(env)
    env.runtime.behaviors.append(
        lambda call, p: outcome(call, {"title": "キャッシュを足す", "body": "本文"})
    )
    assert of_type(env.run(execution), StageCompleted)
    assert env.paths.overview_title.read_text(encoding="utf-8") == "キャッシュを足す"
    assert env.paths.overview_body.read_text(encoding="utf-8") == "本文"


@pytest.mark.parametrize("title", ["[autodev]", "   "])
def test_印を除くと空になるタイトルは形の誤りにして書き出さない(env: Env, title: str):
    execution = rewrite_overview(env)
    for _ in range(1 + SHAPE_CORRECTIONS):
        env.runtime.behaviors.append(
            lambda call, p: outcome(call, {"title": title, "body": "本文"})
        )
    env.executor.run(execution, env.world.inbox.expect(execution))
    env.executor.join()
    (report,) = reports(env)
    assert report.evidence.result_valid is False
    assert "$.title: 概要 PR のタイトル が空" in (report.evidence.error or "")
    assert not env.paths.overview_title.exists()


def test_根元が無いタスクのコミットの数は0件ではなく数えられないとして渡す(env: Env):
    planning_task(env)
    prepare = ex(S.PREPARE, task=PLANNING)
    env.begin(prepare)
    env.executor.run(prepare, env.world.inbox.expect(prepare))
    env.executor.join()
    (report,) = reports(env)
    assert report.evidence.commits is None


# --- 計画ステージの ask（defer）と再開 ---


def test_deferで止まった計画ステージは回答の後にプロンプト無しで続ける(env: Env):
    planning_task(env)
    env.begin(ex(S.PREPARE, task=PLANNING))
    events = env.run(ex(S.PREPARE, task=PLANNING))
    assert of_type(events, StageCompleted)[0].produced == (ArtifactRef(A.BRIEF, "brief.md"),)
    assert "キャッシュを足す" in env.paths.brief.read_text(encoding="utf-8")
    plan = ex(S.PLAN, task=PLANNING)
    env.begin(plan)

    ask = DeferredToolUse(
        "toolu_01", "Bash", {"command": "/s/bin/autodev ask --question 'どちら？'"}
    )
    env.runtime.behaviors.append(
        lambda call, p: outcome(call, None, stop_reason="tool_deferred", deferred=ask)
    )
    events = env.run(plan)
    (deferred,) = of_type(events, StageDeferred)
    assert deferred.tool_use_id == "toolu_01"
    (raised,) = of_type(events, EscalationRaised)
    assert raised.kind is EscalationKind.ASK and raised.reason == "どちら？"

    escalation = next(iter(env.world.task(StreamId.task(PLANNING)).escalations))
    env.world(
        ResolveEscalation(
            command_id=new_id(), issuer=POLICY, task=PLANNING, escalation=escalation, answer="A"
        )
    )
    env.world(
        ResumeStage(
            command_id=new_id(),
            issuer=Issuer.reaction("r", escalation),
            task=PLANNING,
            execution=plan,
        )
    )
    env.runtime.behaviors.append(lambda call, p: outcome(call, PLAN_RESULT))
    events = env.run(plan)

    first, second = env.runtime.calls
    assert first.resume is False and first.prompt is not None
    assert second.resume is True and second.prompt is None and second.session == first.session
    assert first.cwd == str(env.paths.overview_tree)
    (completed,) = of_type(events, StageCompleted)
    assert set(completed.produced) == {
        ArtifactRef(A.PROPOSAL, "1"),
        ArtifactRef(A.CODEMAP, "codemap.md"),
    }
    assert (env.paths.root / "design" / "v1.md").read_text(encoding="utf-8") == "# 設計\n"
    assert env.paths.codemap.read_text(encoding="utf-8") == "# コードマップ\n"
    # 書く範囲が無い計画ステージにも ask の許しが渡る
    guard = json.loads(second.env[GUARD_ENV])["guard"]
    assert guard == {"writes": "none", "judge": False, "readsDesign": False, "canAsk": True}


def test_止めた実行は結果を返さず再開すると続きの指示を渡す(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    running = threading.Event()

    def blocks(call: AgentCall, process: FakeProcess):
        running.set()
        process.stop.wait(10)
        return outcome(call, None, is_error=True, interrupted=process.interrupted)

    env.runtime.behaviors.append(blocks)
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    assert running.wait(10)
    env.world(
        InterruptStage(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            execution=ex(S.IMPL),
            cause=InterruptCause.PANIC,
        )
    )
    env.executor.interrupt(ex(S.IMPL))
    env.executor.join()
    assert env.runtime.processes[0].interrupted == "driver が止めた"
    assert env.world.submitted() == []
    assert env.world.inbox.outstanding == 0

    env.world(
        ResumeStage(command_id=new_id(), issuer=Issuer.driver(), task=T1, execution=ex(S.IMPL))
    )
    env.runtime.behaviors.append(commits_and_returns(IMPL_RESULT))
    assert of_type(env.run(ex(S.IMPL)), StageCompleted)
    resumed = env.runtime.calls[1]
    assert resumed.resume is True and resumed.prompt == "止めたところから続けて"
    assert resumed.session == env.runtime.calls[0].session
    assert env.prompts.calls[-1][1].resume is ResumeMode.INTERRUPTED


def test_再開に失敗したら作り直しを頼みworktreeを始めた時点に戻す(env: Env):
    impl_task(env)
    (started,) = of_type(env.begin(ex(S.IMPL)), StageStarted)
    tree = env.paths.task_tree(T1)
    env.world(
        InterruptStage(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            execution=ex(S.IMPL),
            cause=InterruptCause.PANIC,
        )
    )
    env.world(
        ResumeStage(command_id=new_id(), issuer=Issuer.driver(), task=T1, execution=ex(S.IMPL))
    )

    def lost(call: AgentCall, process: FakeProcess):
        commit(tree, "half.py", "途中\n")
        (tree / "junk.txt").write_text("x", encoding="utf-8")
        return lost_session(call, process)

    env.runtime.behaviors.append(lost)
    events = env.run(ex(S.IMPL))
    (restarted,) = of_type(events, ExecutionRestarted)
    assert restarted.start_commit == started.start_commit
    assert [e.execution for e in of_type(events, StageRequested)] == [ex(S.IMPL, attempt=2)]

    env.executor.restart(ex(S.IMPL), restarted.start_commit)
    (again,) = of_type(env.begin(ex(S.IMPL, attempt=2)), StageStarted)
    assert again.start_commit == started.start_commit
    assert not (tree / "junk.txt").exists() and not (tree / "half.py").exists()
    # 続けられなかったセッションは使わない
    assert again.session == env.sessions[1]


def lost_session(call: AgentCall, process: FakeProcess):
    """見つからないセッションの `--resume`。claude 2.1.288 は init を出さず、この result を返して
    終了コード 1 で終わる（段 6 の実測）。"""
    message = f"No conversation found with session ID: {call.session}"
    return outcome(
        call,
        None,
        subtype="error_during_execution",
        is_error=True,
        num_turns=0,
        text=message,
        exit_code=1,
        stderr=message,
        initialized=False,
    )


@pytest.mark.parametrize(
    "stopped",
    [
        # result が来ないので kill した
        {"ending": Ending.KILLED, "exit_code": -9},
        # interrupt を送り、result が返った
        {
            "subtype": "error_during_execution",
            "is_error": True,
            "num_turns": 0,
            "exit_code": 1,
            "interrupted": "制限時間を超えた",
        },
    ],
)
def test_続けたセッションでinitの前にこちらが止めたら続けられなかったとは数えず失敗にする(
    env: Env, stopped: dict
):
    """init の前に止めたのでは、セッションが在ったかは分からない。作り直すと前の仕事を捨てる。"""
    impl_task(env)
    env.begin(ex(S.IMPL))
    env.world(
        InterruptStage(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            execution=ex(S.IMPL),
            cause=InterruptCause.PANIC,
        )
    )
    env.world(
        ResumeStage(command_id=new_id(), issuer=Issuer.driver(), task=T1, execution=ex(S.IMPL))
    )
    env.runtime.behaviors.append(lambda call, p: outcome(call, None, initialized=False, **stopped))
    events = env.run(ex(S.IMPL))
    assert env.runtime.calls[-1].resume is True
    assert of_type(events, StageFailed) and not of_type(events, ExecutionRestarted)


def test_続けたセッションで始めた後に落ちたら続けられなかったとは数えず失敗にする(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    env.world(
        InterruptStage(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            execution=ex(S.IMPL),
            cause=InterruptCause.PANIC,
        )
    )
    env.world(
        ResumeStage(command_id=new_id(), issuer=Issuer.driver(), task=T1, execution=ex(S.IMPL))
    )
    env.runtime.behaviors.append(
        lambda call, p: outcome(
            call, None, ending=Ending.NO_RESULT, exit_code=1, stderr="落ちた", initialized=True
        )
    )
    events = env.run(ex(S.IMPL))
    assert env.runtime.calls[-1].resume is True
    assert of_type(events, StageFailed) and not of_type(events, ExecutionRestarted)


def test_作り直しの反応が戻す前に落ちても次の試みは始める前に戻す(env: Env):
    impl_task(env)
    (started,) = of_type(env.begin(ex(S.IMPL)), StageStarted)
    tree = env.paths.task_tree(T1)
    env.stage_log(ex(S.IMPL)).parent.mkdir(parents=True)
    env.stage_log(ex(S.IMPL)).write_text("{}\n", encoding="utf-8")

    def lost(call: AgentCall, process: FakeProcess):
        commit(tree, "half.py", "途中\n")
        return lost_session(call, process)

    env.runtime.behaviors.append(lost)
    assert of_type(env.run(ex(S.IMPL)), ExecutionRestarted)
    # restart を呼ばない（反応が戻す前に driver が落ちた）
    (again,) = of_type(env.begin(ex(S.IMPL, attempt=2)), StageStarted)
    assert again.start_commit == started.start_commit
    assert not (tree / "half.py").exists()


def test_もう起こした跡がある初めての実行は続きとして起こし続けられなければ作り直す(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    # 前の driver がこの実行の claude をもう起こしていた（配り直した StageStarted で走らせ直す）
    log_path = env.stage_log(ex(S.IMPL))
    log_path.parent.mkdir(parents=True)
    log_path.write_text("{}\n", encoding="utf-8")
    env.runtime.behaviors.append(lost_session)
    events = env.run(ex(S.IMPL))
    (call,) = env.runtime.calls
    assert call.resume is True and call.prompt == "止めたところから続けて"
    # Task の resumed_from は無いが、走ったままにせず作り直す
    assert of_type(events, ExecutionRestarted)
    assert [e.execution for e in of_type(events, StageRequested)] == [ex(S.IMPL, attempt=2)]


def test_前のセッションを続ける初めての実行が続けられなければ作り直す(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    env.runtime.behaviors.append(lambda call, p: outcome(call, {"broken": True}))
    assert of_type(env.run(ex(S.IMPL)), StageFailed)
    (started,) = of_type(env.begin(ex(S.IMPL, attempt=2)), StageStarted)
    assert started.session == env.sessions[0]
    env.runtime.behaviors.append(lost_session)
    events = env.run(ex(S.IMPL, attempt=2))
    assert env.runtime.calls[1].resume is True and env.runtime.calls[1].prompt is not None
    assert of_type(events, ExecutionRestarted)
    (third,) = of_type(env.begin(ex(S.IMPL, attempt=3)), StageStarted)
    assert third.session == env.sessions[1]


def test_プロンプトを組めなくても札を返し失敗はTaskが決める(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))

    def broken(context, aggregates):
        raise ValueError("設計ファイルが読めない")

    env.prompts.prompt = broken  # ty: ignore[invalid-assignment]
    events = env.run(ex(S.IMPL))
    (failed,) = of_type(events, StageFailed)
    assert "設計ファイルが読めない" in failed.reason
    assert env.world.inbox.outstanding == 0
    assert env.executor._live == {}
    assert env.runtime.calls == []


def test_事実を写し取れなければ札を返す(env: Env, monkeypatch: pytest.MonkeyPatch):
    impl_task(env)

    def broken(*args, **kwargs):
        raise RuntimeError("壊れた集約")

    monkeypatch.setattr("autodev.app.stages.executor.snapshot", broken)
    ticket = env.world.inbox.expect(ex(S.IMPL))
    env.executor.begin(ex(S.IMPL), ticket)
    assert ticket.used and env.world.submitted() == []
    ticket = env.world.inbox.expect(ex(S.IMPL))
    env.executor.run(ex(S.IMPL), ticket)
    assert ticket.used and env.world.submitted() == []
    assert env.world.inbox.outstanding == 0


def test_止めた決定的なステージは検証コマンドまで止め結果を返さない(env: Env):
    tree = env.paths.task_tree(T1)
    impl_task(
        env,
        FlowStep(S.CONFIRM_RED),
        *IMPL_FLOW,
        verify=("touch started; sleep 30",),
        artifacts=(ArtifactRef(A.TESTS, "a" * 40),),
    )
    env.begin(ex(S.CONFIRM_RED))
    begun = time.monotonic()
    env.executor.run(ex(S.CONFIRM_RED), env.world.inbox.expect(ex(S.CONFIRM_RED)))
    while not (tree / "started").exists():
        assert time.monotonic() - begun < 10
        time.sleep(0.01)
    env.world(
        InterruptStage(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            execution=ex(S.CONFIRM_RED),
            cause=InterruptCause.REQUESTED,
        )
    )
    env.executor.interrupt(ex(S.CONFIRM_RED))
    env.executor.join(15)
    assert time.monotonic() - begun < 15
    assert env.world.submitted() == [] and env.world.inbox.outstanding == 0
    assert env.executor._live == {}


def test_同じworktreeの次のbeginは止めた実行が終わるまで待つ(env: Env):
    impl_task(env)
    tree = env.paths.task_tree(T1)
    running = threading.Event()
    finished: list[float] = []

    def blocks(call: AgentCall, process: FakeProcess):
        running.set()
        process.stop.wait(10)
        time.sleep(0.3)  # 止めてから終わるまでに間がある
        finished.append(time.monotonic())
        return outcome(call, None, is_error=True, interrupted=process.interrupted)

    env.begin(ex(S.IMPL))
    env.runtime.behaviors.append(blocks)
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    assert running.wait(10)
    env.executor.interrupt(ex(S.IMPL))
    env.executor._wait_stopped(tree)
    waited = time.monotonic()
    assert finished and finished[0] <= waited
    assert ex(S.IMPL) not in env.executor._live


def stuck_stop(env: Env, tree: Path) -> executor_module._Live:
    """`tree` で止めたことにした、いつまでも終わらない実行。"""
    live = executor_module._Live(tree, stopped=True)
    env.executor._stopping.append(live)
    return live


def test_走り出した直後に止めた実行は自分の終わりを待たず同じworktreeの次の仕事も待たせない(
    env: Env, monkeypatch: pytest.MonkeyPatch
):
    """run のスレッドが止めた実行を待つ前に止めると、その走りは止めている実行の一覧に入っている。"""
    impl_task(env)
    monkeypatch.setattr(executor_module, "STOP_WAIT_SECONDS", 5.0)
    env.begin(ex(S.IMPL))
    entered, release = threading.Event(), threading.Event()
    progress = env.executor._progress

    def held(context, body):
        entered.set()
        release.wait(10)
        progress(context, body)

    monkeypatch.setattr(env.executor, "_progress", held)
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    assert entered.wait(10)
    env.executor.interrupt(ex(S.IMPL))
    tree = env.paths.task_tree(T1)
    lock = tree / sh(tree, "rev-parse", "--git-path", "index.lock").strip()
    lock.write_text("", encoding="utf-8")
    begun = time.monotonic()
    release.set()
    env.executor.join(10)
    assert time.monotonic() - begun < 2
    assert env.executor._stopping == [] and env.world.submitted() == []
    # 止めた走りは何も走らせない（claude を起こさず、起こした跡も残さない）
    assert env.runtime.calls == []
    assert not env.paths.stage_log(ex(S.IMPL)).is_file()
    # 止めた当の走りは lock を片付けない。片付けるのは次の仕事
    assert lock.exists() and tree in env.executor._unswept
    begun = time.monotonic()
    env.executor.abort_rebase(T1)
    env.executor.join(10)
    assert time.monotonic() - begun < 2
    assert not lock.exists()


def test_止めた実行を待つのはそのworktreeだけでほかのworktreeのbeginは待たせない(
    env: Env, monkeypatch: pytest.MonkeyPatch
):
    impl_task(env)
    monkeypatch.setattr(executor_module, "STOP_WAIT_SECONDS", 5.0)
    stuck_stop(env, env.paths.task_tree(TaskId("task2")))
    begun = time.monotonic()
    assert of_type(env.begin(ex(S.IMPL)), StageStarted)
    assert time.monotonic() - begun < 2


def test_止めた実行が終わらなければbeginは始めずに待ち直し使い切ったら始められなかったと渡す(
    env: Env, monkeypatch: pytest.MonkeyPatch
):
    impl_task(env)
    monkeypatch.setattr(executor_module, "STOP_WAIT_SECONDS", 0.05)
    stuck_stop(env, env.paths.task_tree(T1))
    ticket = env.world.inbox.expect(ex(S.IMPL))
    env.executor.begin(ex(S.IMPL), ticket)
    deadline = time.monotonic() + 5
    while not ticket.used:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    (command,) = env.world.submitted()
    assert isinstance(command, ReportBeginFailure)
    assert "止めた実行" in command.error
    events = env.world(command)
    assert not of_type(events, StageStarted)
    (failed,) = of_type(events, StageFailed)
    assert failed.reason.startswith("始められなかった")
    assert [e.execution for e in of_type(events, StageRequested)] == [ex(S.IMPL, attempt=2)]


def test_止めた実行が残したindex_lockは走り終えた後の次の仕事が消し一度だけ消す(env: Env):
    """止めた走りは終わるとすぐ止めている実行の一覧から外れる。外れた後に来た次の仕事でも消す。"""
    impl_task(env)
    tree = env.paths.task_tree(T1)
    lock = tree / sh(tree, "rev-parse", "--git-path", "index.lock").strip()
    running = threading.Event()

    def killed_mid_git(call: AgentCall, process: FakeProcess):
        lock.write_text("", encoding="utf-8")  # SIGKILL で止めた git が片付けずに終わった
        running.set()
        process.stop.wait(10)
        return outcome(call, None, is_error=True, interrupted=process.interrupted)

    env.begin(ex(S.IMPL))
    env.runtime.behaviors.append(killed_mid_git)
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    assert running.wait(10)
    env.executor.interrupt(ex(S.IMPL))
    env.executor.join()
    assert env.executor._stopping == [] and lock.exists()
    env.executor.abort_rebase(T1)
    env.executor.join()
    assert not lock.exists()
    # 片付けた後に現れた lock は、止めた実行のものではないので消さない
    lock.write_text("", encoding="utf-8")
    env.executor.abort_rebase(T1)
    env.executor.join()
    assert lock.exists()


def test_止めた実行が無ければindex_lockを消さない(env: Env):
    impl_task(env)
    tree = env.paths.task_tree(T1)
    lock = tree / sh(tree, "rev-parse", "--git-path", "index.lock").strip()
    lock.write_text("", encoding="utf-8")
    assert of_type(env.begin(ex(S.IMPL)), StageStarted)
    assert lock.exists()


def test_beginでHEADを取れなければ始められなかったとTaskに渡しTaskがやり直す(
    env: Env, monkeypatch: pytest.MonkeyPatch
):
    impl_task(env)

    def broken(context):
        raise RuntimeError("worktree が壊れた")

    monkeypatch.setattr(env.executor, "_head", broken)
    events = env.begin(ex(S.IMPL))
    (failed,) = of_type(events, StageFailed)
    assert "worktree が壊れた" in failed.reason
    assert [e.execution for e in of_type(events, StageRequested)] == [ex(S.IMPL, attempt=2)]
    assert env.world.inbox.outstanding == 0


def test_止めた実行が走り終える前に同じ実行を再開しても走らせる(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    running = threading.Event()
    ended: list[float] = []

    def blocks(call: AgentCall, process: FakeProcess):
        running.set()
        process.stop.wait(10)
        time.sleep(0.2)  # 止めてから終わるまでに間がある
        ended.append(time.monotonic())
        return outcome(call, None, is_error=True, interrupted=process.interrupted)

    env.runtime.behaviors.append(blocks)
    env.executor.run(ex(S.IMPL), env.world.inbox.expect(ex(S.IMPL)))
    assert running.wait(10)
    env.world(
        InterruptStage(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            execution=ex(S.IMPL),
            cause=InterruptCause.PANIC,
        )
    )
    env.executor.interrupt(ex(S.IMPL))
    # 止めた走りが終わるのを待たずに再開する
    env.world(
        ResumeStage(command_id=new_id(), issuer=Issuer.driver(), task=T1, execution=ex(S.IMPL))
    )
    started: list[float] = []

    def resumed(call: AgentCall, process: FakeProcess):
        started.append(time.monotonic())
        return commits_and_returns(IMPL_RESULT)(call, process)

    env.runtime.behaviors.append(resumed)
    events = env.run(ex(S.IMPL))
    assert of_type(events, StageCompleted)
    # 同じ worktree で並べて走らせない
    assert ended and started and ended[0] <= started[0]


def test_配り直しで済んだ実行をもう一度頼まれても走らせない(env: Env):
    impl_task(env)
    env.begin(ex(S.IMPL))
    ticket = env.world.inbox.expect(ex(S.IMPL))
    env.executor.begin(ex(S.IMPL), ticket)
    env.executor.join()
    assert ticket.used and env.world.submitted() == []


def plan_run(env: Env, *, quick_checks: tuple[str, ...], regression_tests: tuple[str, ...]) -> None:
    """ランを始め、軽い検査と回帰テストを載せた計画を反映する。"""
    env.world(
        StartRun(
            command_id=new_id(),
            issuer=Issuer.cli(),
            name=RunName("r"),
            instruction=Instruction("キャッシュを足す"),
            repository=Repository(str(env.repo)),
            base=BranchName("main"),
            limit=ParallelLimit(3),
        )
    )
    for task in (TaskId.planning(), TaskId.git()):
        env.world(StartTask(command_id=new_id(), issuer=POLICY, task=task))
    proposal = Proposal(
        DesignVersion(1),
        (),
        quick_checks=tuple(VerifyCommand(c) for c in quick_checks),
        regression_tests=tuple(VerifyCommand(c) for c in regression_tests),
    )
    env.world(
        RecordSettledPlan(
            command_id=new_id(),
            issuer=POLICY,
            proposal=proposal,
            artifacts=(ArtifactRef(A.DESIGN, "1"),),
        )
    )
    env.world(ApplyPlan(command_id=new_id(), issuer=POLICY, design=DesignVersion(1)))


@pytest.mark.parametrize(
    ("task_tests", "quick_check", "expected"),
    [
        # タスクのテストが落ち、軽い検査が通る: ConfirmRed は完了する
        ("exit 1", "true", StageCompleted),
        # 軽い検査も落ちている: テストの外の落ちを赤と取り違えず、上げる
        ("exit 1", "exit 2", EscalationRaised),
        # タスクのテストが通る: 今までどおり上げる
        ("true", "true", EscalationRaised),
    ],
)
def test_ConfirmRedはタスクのテストと軽い検査の両方を流し回帰テストは流さない(
    env: Env,
    task_tests: str,
    quick_check: str,
    expected: type[StageCompleted] | type[EscalationRaised],
):
    # 回帰テスト（exit 1）が流れていれば、完了する組でも落ちた扱いになってしまう
    plan_run(env, quick_checks=(quick_check,), regression_tests=("exit 1",))
    env.git.create_branch(BRANCH, "main")
    env.git.add_worktree(env.paths.task_tree(T1), BRANCH)
    env.world(
        OpenTask(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            kind=TaskKind.IMPLEMENTATION,
            spec=TaskSpec("x", task_tests=(VerifyCommand(task_tests),)),
            artifacts=(ArtifactRef(A.DESIGN, "1"), ArtifactRef(A.TESTS, "a" * 40)),
            branch=BRANCH,
        )
    )
    env.world(
        AcceptFlow(
            command_id=new_id(),
            issuer=Issuer.task_supervisor(T1, SESSION),
            task=T1,
            steps=(FlowStep(S.CONFIRM_RED), *IMPL_FLOW),
        )
    )
    execution = ex(S.CONFIRM_RED)
    env.begin(execution)
    events = env.run(execution)
    assert of_type(events, expected), events
    if expected is EscalationRaised:
        assert of_type(events, EscalationRaised)[0].kind is EscalationKind.RED_CHECK_FAILED


def test_ConfirmRedはタスクのテストが落ちれば完了し全部通ればred_check_failedを上げる(env: Env):
    steps = (FlowStep(S.CONFIRM_RED), *IMPL_FLOW)
    env.git.create_branch(BRANCH, "main")
    env.git.add_worktree(env.paths.task_tree(T1), BRANCH)
    for verify, expected in (("exit 1", StageCompleted), ("true", EscalationRaised)):
        task = TaskId("task1") if verify == "exit 1" else TaskId("task2")
        if task != T1:
            env.git.create_branch(BranchName("stack/r--task-2"), "main")
            env.git.add_worktree(env.paths.task_tree(task), BranchName("stack/r--task-2"))
        env.world(
            OpenTask(
                command_id=new_id(),
                issuer=POLICY,
                task=task,
                kind=TaskKind.IMPLEMENTATION,
                spec=TaskSpec("x", task_tests=(VerifyCommand(verify),)),
                artifacts=(ArtifactRef(A.DESIGN, "1"), ArtifactRef(A.TESTS, "a" * 40)),
                branch=BRANCH,
            )
        )
        env.world(
            AcceptFlow(
                command_id=new_id(),
                issuer=Issuer.task_supervisor(task, SESSION),
                task=task,
                steps=steps,
            )
        )
        execution = ex(S.CONFIRM_RED, task=task)
        assert of_type(env.begin(execution), StageStarted)[0].session is None
        events = env.run(execution)
        assert of_type(events, expected), events
        if expected is EscalationRaised:
            assert of_type(events, EscalationRaised)[0].kind is EscalationKind.RED_CHECK_FAILED


TESTGEN_RESULT = {
    "report": None,
    "reportReason": None,
    "unchanged": False,
    "awaitingExpectations": [],
    "notes": "",
}


class QuickCheck:
    """流した回数を数え、worktree に `broken` が在れば落ちる軽い検査。"""

    def __init__(self, env: Env) -> None:
        self.counter = env.repo.parent / "quick-check-runs"
        self.command = f"echo x >> {self.counter}; test ! -e broken"

    @property
    def runs(self) -> int:
        return self.counter.read_text(encoding="utf-8").count("x") if self.counter.exists() else 0


def open_testgen_task(env: Env, quick_check: QuickCheck) -> None:
    """軽い検査を載せた計画の下で、TestGen から始まるフローを受けたタスク。"""
    plan_run(env, quick_checks=(quick_check.command,), regression_tests=())
    env.git.create_branch(BRANCH, "main")
    env.git.add_worktree(env.paths.task_tree(T1), BRANCH)
    env.world(
        OpenTask(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            kind=TaskKind.IMPLEMENTATION,
            spec=TaskSpec("キャッシュ", task_tests=(VerifyCommand("true"),)),
            artifacts=(ArtifactRef(A.DESIGN, "1"),),
            branch=BRANCH,
        )
    )
    env.world(
        AcceptFlow(
            command_id=new_id(),
            issuer=Issuer.task_supervisor(T1, SESSION),
            task=T1,
            steps=(FlowStep(S.TEST_GEN), FlowStep(S.CONFIRM_RED), *IMPL_FLOW),
        )
    )


def writes_tests(*, breaks: bool, result: dict | None = None):
    """テストを commit し、軽い検査を落とす `broken` を置く（または片付ける）。"""

    def behave(call: AgentCall, process: FakeProcess):
        tree = Path(call.cwd)
        commit(tree, "tests/test_cache.py", f"{time.time()}\n")
        (tree / "broken").unlink(missing_ok=True)
        if breaks:
            (tree / "broken").write_text("x", encoding="utf-8")
        return outcome(call, result or TESTGEN_RESULT)

    return behave


def run_reporting(env: Env, execution: ExecutionId):
    """走らせて、実行器が返した証拠と、それを Task に通して出たイベント。"""
    env.executor.run(execution, env.world.inbox.expect(execution))
    env.executor.join()
    (report,) = reports(env)
    return report.evidence, env.world(report)


def facts(results) -> list[tuple[str, int, VerifyKind | None]]:
    return [(str(v.command), v.exit_code, v.kind) for v in results]


def baseline_file(env: Env, execution: ExecutionId) -> Path:
    return env.paths.task_results(T1) / f"{execution}-baseline.json"


def test_TestGenは起こす前に軽い検査を流してbaselineを書き終わった後にも流して証拠に載せる(
    env: Env,
):
    check = QuickCheck(env)
    open_testgen_task(env, check)
    first = ex(S.TEST_GEN)
    env.begin(first)
    before_claude: list[tuple[int, bool]] = []

    def behave(call: AgentCall, process: FakeProcess):
        before_claude.append((check.runs, baseline_file(env, first).is_file()))
        return writes_tests(breaks=True)(call, process)

    env.runtime.behaviors.append(behave)
    evidence, events = run_reporting(env, first)

    assert before_claude == [(1, True)]
    assert check.runs == 2
    assert facts(evidence.baseline or ()) == [(check.command, 0, VerifyKind.QUICK_CHECKS)]
    assert facts(evidence.verify) == [(check.command, 1, VerifyKind.QUICK_CHECKS)]
    # 始める前は通っていたので差し戻す
    assert type(events[0]).__name__ == "StageSentBack"
    assert not of_type(events, EscalationRaised)


def test_差し戻した後の試みは軽い検査を流し直さず最初の試みのbaselineを読む(env: Env):
    check = QuickCheck(env)
    open_testgen_task(env, check)
    first, second = ex(S.TEST_GEN), ex(S.TEST_GEN, attempt=2)
    env.begin(first)
    env.runtime.behaviors.append(writes_tests(breaks=True))
    first_evidence, _ = run_reporting(env, first)

    env.begin(second)
    env.runtime.behaviors.append(writes_tests(breaks=False))
    evidence, events = run_reporting(env, second)

    # 始める前の検査は流さず（1 回目の試みの前に 1 回・各試みの後に 1 回ずつで 3 回）、ファイルも書かない
    assert check.runs == 3
    assert not baseline_file(env, second).exists()
    assert facts(evidence.baseline or ()) == facts(first_evidence.baseline or ())
    assert facts(evidence.verify) == [(check.command, 0, VerifyKind.QUICK_CHECKS)]
    assert of_type(events, StageCompleted)


def test_baselineのファイルが無ければbaselineはNoneで始める前の検査は流し直さない(env: Env):
    check = QuickCheck(env)
    open_testgen_task(env, check)
    first, second = ex(S.TEST_GEN), ex(S.TEST_GEN, attempt=2)
    env.begin(first)
    env.runtime.behaviors.append(writes_tests(breaks=True))
    run_reporting(env, first)
    baseline_file(env, first).unlink()

    env.begin(second)
    env.runtime.behaviors.append(writes_tests(breaks=True))
    evidence, events = run_reporting(env, second)

    assert evidence.baseline is None
    assert check.runs == 3
    # 始める前に通っていたとみなせるので、2 回目も差し戻す
    assert type(events[0]).__name__ == "StageSentBack"


def test_止めたTestGenを続きから起こすときも始める前の検査を流し直さずbaselineを読む(env: Env):
    check = QuickCheck(env)
    open_testgen_task(env, check)
    first = ex(S.TEST_GEN)
    env.begin(first)
    running = threading.Event()

    def blocks(call: AgentCall, process: FakeProcess):
        running.set()
        process.stop.wait(10)
        return outcome(call, None, is_error=True, interrupted=process.interrupted)

    env.runtime.behaviors.append(blocks)
    env.executor.run(first, env.world.inbox.expect(first))
    assert running.wait(10)
    assert check.runs == 1
    env.world(
        InterruptStage(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            execution=first,
            cause=InterruptCause.PANIC,
        )
    )
    env.executor.interrupt(first)
    env.executor.join()
    env.world.submitted()
    env.world(ResumeStage(command_id=new_id(), issuer=Issuer.driver(), task=T1, execution=first))

    env.runtime.behaviors.append(writes_tests(breaks=False))
    evidence, events = run_reporting(env, first)

    assert check.runs == 2
    assert facts(evidence.baseline or ()) == [(check.command, 0, VerifyKind.QUICK_CHECKS)]
    assert of_type(events, StageCompleted)


@pytest.mark.parametrize("shape", ["報告", "形が違う"])
def test_報告を返したときと形が違うときは終わった後の軽い検査を流さない(env: Env, shape: str):
    check = QuickCheck(env)
    open_testgen_task(env, check)
    first = ex(S.TEST_GEN)
    env.begin(first)
    if shape == "報告":
        result = {**TESTGEN_RESULT, "report": "design-gap", "reportReason": "設計に無い形が要る"}
    else:
        result = {k: v for k, v in TESTGEN_RESULT.items() if k != "notes"}
    env.runtime.behaviors.append(writes_tests(breaks=True, result=result))
    if shape == "形が違う":
        for _ in range(SHAPE_CORRECTIONS):
            env.runtime.behaviors.append(lambda call, p: outcome(call, result))
    evidence, _ = run_reporting(env, first)
    assert evidence.verify == ()
    assert check.runs == 1


def test_軽い検査が0件のランではTestGenは検査を流さず今どおり完了する(env: Env):
    plan_run(env, quick_checks=(), regression_tests=())
    env.git.create_branch(BRANCH, "main")
    env.git.add_worktree(env.paths.task_tree(T1), BRANCH)
    env.world(
        OpenTask(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            kind=TaskKind.IMPLEMENTATION,
            spec=TaskSpec("キャッシュ"),
            artifacts=(ArtifactRef(A.DESIGN, "1"),),
            branch=BRANCH,
        )
    )
    env.world(
        AcceptFlow(
            command_id=new_id(),
            issuer=Issuer.task_supervisor(T1, SESSION),
            task=T1,
            steps=(FlowStep(S.TEST_GEN), FlowStep(S.CONFIRM_RED), *IMPL_FLOW),
        )
    )
    env.begin(ex(S.TEST_GEN))
    env.runtime.behaviors.append(writes_tests(breaks=True))
    evidence, events = run_reporting(env, ex(S.TEST_GEN))
    assert evidence.verify == ()
    assert of_type(events, StageCompleted)


def test_差し戻した後のプロンプトには差し戻しの理由が載り初めての試みでは空になる(env: Env):
    check = QuickCheck(env)
    open_testgen_task(env, check)
    prompts = Prompts(env.paths, lambda: [])

    def prompt_of(execution: ExecutionId) -> str:
        context = StageContext(
            execution=execution,
            step=FlowStep(S.TEST_GEN),
            tree=env.paths.task_tree(T1),
        )
        return prompts.prompt(context, env.world.aggregates).text

    first, second = ex(S.TEST_GEN), ex(S.TEST_GEN, attempt=2)
    assert "| `<差し戻しの理由>` | （空）" in prompt_of(first)
    env.begin(first)
    env.runtime.behaviors.append(writes_tests(breaks=True))
    run_reporting(env, first)
    sent_back = env.world.task(StreamId.task(T1)).executions[first].end_reason
    assert sent_back and check.command in sent_back

    text = prompt_of(second)
    assert "## `<差し戻しの理由>`" in text
    assert check.command in text.split("## `<差し戻しの理由>`", 1)[1]


def reach_gate(env: Env) -> ExecutionId:
    """実装済みのタスクを、Review と Judge を指摘なしで通して Gate の手前まで進める。"""
    impl_task(
        env,
        FlowStep(S.REVIEW_LOOP, reviewers=Reviewers((S.REVIEW,))),
        FlowStep(S.GATE),
        FlowStep(S.WRITE_PR_BODY),
        verify=("true",),
        artifacts=(ArtifactRef(A.IMPL, "a" * 40),),
    )
    tree = env.paths.task_tree(T1)
    base = sh(env.repo, "rev-parse", "main").strip()
    commit(tree, "docs/a.md", "x\n")
    env.world(RecordBase(command_id=new_id(), issuer=POLICY, task=T1, base=CommitSha(base)))
    review, judge = ex(S.REVIEW, round=1), ex(S.JUDGE, round=1)
    env.runtime.behaviors.append(lambda call, p: outcome(call, {"findings": []}))
    env.begin(review)
    env.run(review)
    env.world(
        ConfirmHandoff(command_id=new_id(), issuer=POLICY, task=T1, execution=review, refused=None)
    )
    empty_judgement = {"verdicts": [], "comments": [], "stallCause": None, "stallReason": None}
    env.runtime.behaviors.append(lambda call, p: outcome(call, empty_judgement))
    env.begin(judge)
    env.run(judge)
    env.world(
        ConcludeReviewRound(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            judge=judge,
            unresolved=(),
            stalled=(),
            cause=None,
        )
    )
    return ex(S.GATE)


def test_Gateはタスクのテストが通ってもラン共通の軽い検査が落ちればFixに戻る(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env = make_env(tmp_path, monkeypatch, untested_globs=(GlobPattern("docs/**"),))
    plan_run(env, quick_checks=("exit 1",), regression_tests=("true",))
    gate = reach_gate(env)
    env.begin(gate)
    events = env.run(gate)
    # タスクのテスト（true）は通っているので、G-verify を開くのは軽い検査（exit 1）だけ
    (failed,) = of_type(events, GateFailed)
    assert [(r.item, r.reason) for r in failed.failed] == [
        (GateItem.VERIFY, "exit 1 が終了コード 1 で落ちた")
    ]
    assert failed.cursor.inner is S.FIX


def test_Gateはラン共通の回帰テストを流さない(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env = make_env(tmp_path, monkeypatch, untested_globs=(GlobPattern("docs/**"),))
    plan_run(env, quick_checks=("true",), regression_tests=("exit 1",))
    gate = reach_gate(env)
    env.begin(gate)
    events = env.run(gate)
    assert not of_type(events, GateFailed), events
    assert of_type(events, StageCompleted)


def test_IntegrationCheckはラン共通の軽い検査と回帰テストを両方流す(env: Env):
    plan_run(env, quick_checks=("echo quick",), regression_tests=("echo regression",))
    env.git.create_branch(BRANCH, "main")
    env.git.add_worktree(env.paths.task_tree(T1), BRANCH)
    env.git.create_branch(OVERVIEW, "main")
    env.git.add_worktree(env.paths.overview_tree, OVERVIEW)
    commit(env.paths.task_tree(T1), "b.txt", "x\n")
    git = TaskId.git()
    job = GitJob(6, GitJobKind.STACK, task=T1, branch=BRANCH, base=OVERVIEW)
    env.world(
        OpenTask(
            command_id=new_id(),
            issuer=POLICY,
            task=T1,
            kind=TaskKind.IMPLEMENTATION,
            spec=TaskSpec("x", task_tests=(VerifyCommand("exit 1"),)),
            artifacts=(ArtifactRef(A.DESIGN, "1"),),
            branch=BRANCH,
        )
    )
    base = sh(env.repo, "rev-parse", "main").strip()
    env.world(RecordBase(command_id=new_id(), issuer=POLICY, task=T1, base=CommitSha(base)))
    env.world(OpenTask(command_id=new_id(), issuer=POLICY, task=git, kind=TaskKind.GIT))
    env.world(
        AcceptFlow(
            command_id=new_id(),
            issuer=Issuer.task_supervisor(git),
            task=git,
            steps=git_job_flow(job),
            job=job,
        )
    )
    rebase = ex(S.REBASE, task=git)
    env.begin(rebase)
    env.run(rebase)
    check = ex(S.INTEGRATION_CHECK, task=git)
    env.begin(check)
    env.executor.run(check, env.world.inbox.expect(check))
    env.executor.join()
    (report,) = reports(env)
    # 軽い検査 → 回帰テストの順。タスクのテスト（exit 1）は流れない
    assert [(str(v.command), v.exit_code) for v in report.evidence.verify] == [
        ("echo quick", 0),
        ("echo regression", 0),
    ]


def test_joinの待ち時間は全体の上限で走りの数だけ延びない(env: Env):
    release = threading.Event()
    threads = [threading.Thread(target=release.wait, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    env.executor._threads.extend(threads)
    began = time.monotonic()
    env.executor.join(0.5)
    assert time.monotonic() - began < 1.0
    release.set()
