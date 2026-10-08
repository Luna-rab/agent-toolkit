"""決定的なステージの中身（`app/stages/programs/`）。本物の git（bare の origin を含む）と偽の gh で確かめる。

中身は、ドメインが決めたこと（仕事の相手）を実行して、起きた事実を返すだけである。合否は返さない
（Gate の項目ごとの合否は GateEvaluator が、外れたときの扱いは Task が決める）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from autodev_harness import POLICY, new_id, of_type
from executor_fakes import Env, commit, make_env, sh

from autodev.adapters.process.command import CommandFailed
from autodev.app.stages.programs.common import ProgramOutcome, Tools, own_commits
from autodev.app.stages.programs.discard import close_prs, relink, unstack
from autodev.app.stages.programs.implementation import confirm_red, gate
from autodev.app.stages.programs.overview import (
    create_overview_pr,
    ready_overview,
    refresh_overview,
)
from autodev.app.stages.programs.planning import prepare
from autodev.app.stages.programs.registry import run_program
from autodev.app.stages.programs.stacking import (
    check_union,
    create_pr,
    integration_check,
    push,
    stack_link,
)
from autodev.app.stages.stage_context import (
    GateFacts,
    OverviewFacts,
    StackFacts,
    StageContext,
    TargetFacts,
    TaskRow,
    WaitingRow,
)
from autodev.domain.commands.task import AcceptFlow, BeginStage, OpenTask
from autodev.domain.events.task import StageCompleted, StageStarted, WorktreeReady
from autodev.domain.flow.flow import FlowStep
from autodev.domain.flow.standard import git_job_flow
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.artifact_ref import ArtifactRef
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.commit_sha import CommitSha
from autodev.domain.value_objects.decision import Decision
from autodev.domain.value_objects.decision_origin import DecisionOrigin
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.gate_item import GateItem
from autodev.domain.value_objects.git_job import GitJob
from autodev.domain.value_objects.git_job_kind import GitJobKind
from autodev.domain.value_objects.glob_pattern import GlobPattern
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.pr_number import PrNumber
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.stack_entry import StackEntry
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_kind import TaskKind
from autodev.domain.value_objects.task_spec import TaskSpec
from autodev.domain.value_objects.task_status import TaskStatus
from autodev.domain.value_objects.verify_command import VerifyCommand
from autodev.domain.value_objects.verify_kind import VerifyKind

S = StageKind
J = GitJobKind
A = ArtifactKind
T1 = TaskId("task1")
T2 = TaskId("task2")
GIT = TaskId.git()
PLANNING = TaskId.planning()
OVERVIEW = BranchName("stack/r--task-0")
B1 = BranchName("stack/r--task-1")
B2 = BranchName("stack/r--task-2")
OVERVIEW_ENTRY = StackEntry(GIT, OVERVIEW, PrNumber(5), BranchName("main"))
ENTRY1 = StackEntry(T1, B1, PrNumber(11), OVERVIEW)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return make_env(
        tmp_path,
        monkeypatch,
        quick_checks=(VerifyCommand("ruff check ."),),
        regression_tests=(VerifyCommand("pytest -q"),),
        untested_globs=(GlobPattern("docs/**"),),
    )


def tools(env: Env) -> Tools:
    return env.executor._tools


#: worktree ごとの、タスクのブランチの根元（Task.base_commit）。ドメインと同じく、CutBranch が切った元
#: のうち根元になるもの（CutPoint.roots_branch）だけを覚える。検査ごとに tmp_path が違うので混ざらない
ROOTS: dict[Path, CommitSha] = {}


def context(
    env: Env, stage: StageKind, *, job: GitJob | None = None, task: TaskId = GIT, **fields
) -> StageContext:
    where = job.task if job is not None and job.task is not None else task
    fields.setdefault("tree", env.paths.tree_of(where))
    if fields["tree"] in ROOTS:
        fields.setdefault("base_commit", ROOTS[fields["tree"]])
    return StageContext(
        execution=ExecutionId(task, stage, 0, 1),
        step_index=0,
        step=FlowStep(stage),
        job=job,
        **fields,
    )


def cut(env: Env, job: GitJob) -> dict:
    ctx = context(env, S.CUT_BRANCH, job=job)
    outcome = run_program(ctx, tools(env))
    assert outcome.result is not None
    point = job.cut_point
    if point is not None and point.roots_branch and not point.detached:
        ROOTS[ctx.tree] = CommitSha(outcome.result["base"])
    return outcome.result


def rebase(env: Env, job: GitJob, **fields) -> ProgramOutcome:
    """Rebase を実行器と同じ入口（途中の rebase を取りやめてから）で流す。衝突せずに終えたら、ドメインと
    同じく載せ直した先を根元にする（BranchRebased）。"""
    ctx = context(env, S.REBASE, job=job, **fields)
    outcome = run_program(ctx, tools(env))
    if not outcome.conflicts and outcome.result is not None:
        ROOTS[ctx.tree] = CommitSha(outcome.result["onto"])
    return outcome


def overview(env: Env) -> Path:
    cut(env, GitJob(1, J.CUT_OVERVIEW, task=PLANNING, branch=OVERVIEW, base=BranchName("main")))
    return env.paths.overview_tree


def task_tree(env: Env, task: TaskId = T1, branch: BranchName = B1) -> Path:
    cut(env, GitJob(2, J.CUT_TASK, task=task, branch=branch, base=OVERVIEW))
    return env.paths.task_tree(task)


def pr_list(env: Env, head: BranchName, out: str) -> None:
    """`gh pr list` の答え。`--head`・`--state` が違う呼び出しには答えない（空の出力で落ちる）。"""
    env.gh.reply(["pr", "list", "--head", str(head), "--state", "open"], out=out)


def pr_json(number: int, head: str, base: str = "main", draft: bool = False) -> str:
    return json.dumps(
        [
            {
                "number": number,
                "headRefName": head,
                "baseRefName": base,
                "state": "OPEN",
                "isDraft": draft,
            }
        ]
    )


# --- CutBranch ---


def test_概要ブランチとtrees_overviewを切り呼び直しても同じ結果になる(env: Env):
    job = GitJob(1, J.CUT_OVERVIEW, task=PLANNING, branch=OVERVIEW, base=BranchName("main"))
    first = cut(env, job)
    main = sh(env.repo, "rev-parse", "main").strip()
    assert first == {
        "task": "planning",
        "tree": "trees/overview",
        "branch": str(OVERVIEW),
        "base": main,
    }
    assert cut(env, job) == first
    tree = env.paths.overview_tree
    assert sh(tree, "symbolic-ref", "--short", "HEAD").strip() == str(OVERVIEW)


def test_手で消したworktreeもpruneしてから切り直す(env: Env):
    tree = overview(env)
    shutil.rmtree(tree)
    overview(env)
    assert (tree / "a.txt").is_file()


def test_タスクのブランチをスタックの一番上から切る(env: Env):
    top = overview(env)
    head = commit(top, "top.txt", "top\n")
    result = cut(env, GitJob(2, J.CUT_TASK, task=T1, branch=B1, base=OVERVIEW))
    # 切った元（スタックの一番上の HEAD）が、タスクの差分の起点になる
    assert result == {"task": "task1", "tree": "trees/task1", "branch": str(B1), "base": head}
    assert (env.paths.task_tree(T1) / "top.txt").is_file()


def test_stack_topは一番上のHEADで切り離した状態に切り直す(env: Env):
    top = overview(env)
    first = cut(env, GitJob(3, J.CUT_STACK_TOP, task=PLANNING, base=OVERVIEW))
    before = sh(top, "rev-parse", "HEAD").strip()
    assert first == {"task": "planning", "tree": "trees/stack-top", "branch": None, "base": before}
    head = commit(top, "later.txt", "x\n")
    assert cut(env, GitJob(4, J.CUT_STACK_TOP, task=PLANNING, base=OVERVIEW))["base"] == head
    stack_top = env.paths.stack_top_tree
    assert sh(stack_top, "rev-parse", "HEAD").strip() == head
    assert sh(stack_top, "status", "--porcelain", "--branch").startswith("## HEAD (no branch)")


def test_積み直すタスクは前に積んだブランチから新しい名前で切り直す(env: Env):
    overview(env)
    tree = task_tree(env)
    old = commit(tree, "feature.py", "v1\n")
    renamed = BranchName("stack/r--task-1-r1")
    job = GitJob(5, J.STACK, task=T1, branch=renamed, base=OVERVIEW, previous=B1)
    assert cut(env, job)["base"] == old
    assert sh(tree, "symbolic-ref", "--short", "HEAD").strip() == str(renamed)
    assert sh(tree, "rev-parse", "HEAD").strip() == old


def test_積み直した新しい名前のブランチでも根元から上のコミットを数える(env: Env):
    overview(env)
    tree = task_tree(env)
    commit(tree, "feature.py", "v1\n")
    renamed = BranchName("stack/r--task-1-r1")
    cut(env, GitJob(5, J.STACK, task=T1, branch=renamed, base=OVERVIEW, previous=B1))
    ctx = context(env, S.GATE, task=T1)
    assert len(own_commits(ctx, tools(env), tree)) == 1
    # 上のタスクを、このブランチから切っても数は変わらない（ほかのブランチから辿れるかでは数えない）
    sh(env.repo, "branch", "stack/r--task-2", str(renamed))
    assert len(own_commits(ctx, tools(env), tree)) == 1


def test_根元が無ければコミットを数えずに落ちる(env: Env):
    overview(env)
    tree = task_tree(env)
    ctx = context(env, S.GATE, task=T1, base_commit=None)
    with pytest.raises(RuntimeError, match="根元"):
        own_commits(ctx, tools(env), tree)


def test_git管理タスクのCutBranchは切った後にWorktreeReadyまで進む(env: Env):
    overview(env)
    job = GitJob(2, J.CUT_TASK, task=T1, branch=B1, base=OVERVIEW)
    env.world(OpenTask(command_id=new_id(), issuer=POLICY, task=GIT, kind=TaskKind.GIT))
    env.world(
        AcceptFlow(
            command_id=new_id(),
            issuer=Issuer.task_supervisor(GIT),
            task=GIT,
            steps=git_job_flow(job),
            job=job,
        )
    )
    execution = ExecutionId(GIT, S.CUT_BRANCH, 0, 1)
    (started,) = of_type(env.begin(execution), StageStarted)
    # まだ無い worktree の HEAD は、切る元（スタックの一番上）のコミット
    assert str(started.start_commit) == sh(env.paths.overview_tree, "rev-parse", "HEAD").strip()
    events = env.run(execution)
    assert of_type(events, StageCompleted)
    (ready,) = of_type(events, WorktreeReady)
    assert (ready.task, ready.tree, ready.branch) == (T1, "trees/task1", B1)
    assert ready.base == started.start_commit


# --- 積む ---


def conflicting(env: Env) -> tuple[Path, StageContext]:
    """一番上とタスクのブランチが同じファイルの同じ所を変えた。"""
    top = overview(env)
    tree = task_tree(env)
    commit(tree, "a.txt", "1\n2\ntask\n3\n")
    commit(top, "a.txt", "1\n2\ntop\n3\n")
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    outcome = rebase(env, job)
    assert outcome.conflicts == ("a.txt",)
    # 流し直しても同じ衝突を返す
    assert rebase(env, job).conflicts == ("a.txt",)
    return tree, context(env, S.CHECK_UNION, job=job, conflicts=("a.txt",))


def test_両側を残して解いたらCheckUnionが通りrebaseを続ける(env: Env):
    tree, ctx = conflicting(env)
    (tree / "a.txt").write_text("1\n2\ntop\ntask\n3\n", encoding="utf-8")
    outcome = check_union(ctx, tools(env))
    assert outcome.union is not None and outcome.union.passed
    assert not env.git.rebase_in_progress(tree)
    assert (tree / "a.txt").read_text(encoding="utf-8") == "1\n2\ntop\ntask\n3\n"


def test_片方を落とした解き方はCheckUnionが通さずrebaseを止めたまま残す(env: Env):
    tree, ctx = conflicting(env)
    (tree / "a.txt").write_text("1\n2\ntask\n3\n", encoding="utf-8")
    outcome = check_union(ctx, tools(env))
    assert outcome.union is not None and not outcome.union.passed
    assert outcome.union.files[0].missing == ("top",)
    assert env.git.rebase_in_progress(tree)


def test_git_add済みでCheckUnionが落ちた後のやり直しでrebaseし直す(env: Env):
    tree, ctx = conflicting(env)
    (tree / "a.txt").write_text("1\n2\ntask\n3\n", encoding="utf-8")
    sh(tree, "add", "a.txt")
    outcome = check_union(ctx, tools(env))
    assert outcome.union is not None and outcome.union.files[0].unreadable
    # IntegrationFailed を受けた反応が、途中の rebase を取りやめる
    env.executor.abort_rebase(T1)
    env.executor.join()
    assert not env.git.rebase_in_progress(tree)
    # 回答の後に同じ仕事で統合をやり直すと、衝突の段がある状態から始まる
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    assert rebase(env, job).conflicts == ("a.txt",)
    sides = env.git.conflict_sides(tree, "a.txt")
    assert sides.ours is not None and sides.theirs is not None


def test_途中のrebaseが残っていてもRebaseは取りやめてから流し直す(env: Env):
    tree, _ = conflicting(env)
    (tree / "a.txt").write_text("解きかけ\n", encoding="utf-8")
    sh(tree, "add", "a.txt")
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    assert rebase(env, job).conflicts == ("a.txt",)
    assert env.git.conflict_sides(tree, "a.txt").base is not None


def test_rebaseを終えた後に流し直しても始めた時点へ戻してから載せ直す(env: Env):
    """driver が、rebase を終えてから結果（BranchRebased）が載る前に落ち、呼び直した driver が同じ実行を
    根元が古いまま流し直す。戻さずに流すと、一番上のコミットまで載せ直して、ありもしない衝突を作る。"""
    top = overview(env)
    tree = task_tree(env)
    commit(tree, "mine.py", "task\n")
    commit(top, "a.txt", "1\n")
    head = commit(top, "a.txt", "2\n")
    start = CommitSha(sh(tree, "rev-parse", "HEAD").strip())
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    ctx = context(env, S.REBASE, job=job, start_commit=start)
    assert run_program(ctx, tools(env)).conflicts == ()
    outcome = run_program(ctx, tools(env))
    assert outcome.conflicts == () and outcome.result == {"onto": head}
    assert sh(tree, "rev-parse", "HEAD^").strip() == head
    assert (tree / "mine.py").is_file()


def test_rebaseが衝突で止まったままのworktreeで始めたRebaseはタスクのコミットを消さない(env: Env):
    """前の Rebase の衝突を取りやめないまま次の試みが始まった（abort_rebase が待ち切れなかった・
    IntegrationFailed の後に driver が落ちた）。rebase 途中の HEAD を始めた時点にすると、流す前に
    そこへ戻して、タスクのコミットをブランチから落とす。"""
    tree, _ = conflicting(env)
    tip = sh(tree, "rev-parse", str(B1)).strip()
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    ctx = context(env, S.REBASE, job=job)
    env.executor._begin(ctx, env.world.inbox.expect(ctx.execution))
    (begun,) = env.world.submitted()
    assert isinstance(begun, BeginStage) and str(begun.head) == tip
    assert not env.git.rebase_in_progress(tree)
    ctx = context(env, S.REBASE, job=job, start_commit=begun.head)
    assert run_program(ctx, tools(env)).conflicts == ("a.txt",)
    assert sh(tree, "rev-parse", str(B1)).strip() == tip


def test_初めて流すRebaseは汚れたworktreeを消さない(env: Env):
    """戻すのは流し直しの分だけ。汚れた worktree は、git rebase が断って落ちる（黙って消さない）。"""
    overview(env)
    tree = task_tree(env)
    commit(tree, "mine.py", "task\n")
    (tree / "mine.py").write_text("書きかけ\n", encoding="utf-8")
    (tree / "scratch.txt").write_text("追跡していない\n", encoding="utf-8")
    start = CommitSha(sh(tree, "rev-parse", "HEAD").strip())
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    ctx = context(env, S.REBASE, job=job, start_commit=start)
    with pytest.raises(CommandFailed):
        run_program(ctx, tools(env))
    assert (tree / "mine.py").read_text(encoding="utf-8") == "書きかけ\n"
    assert (tree / "scratch.txt").is_file()


def test_rebase途中のworktreeにCutBranchを当てても落ちない(env: Env):
    tree, _ = conflicting(env)
    renamed = BranchName("stack/r--task-1-r1")
    cut(env, GitJob(7, J.STACK, task=T1, branch=renamed, base=OVERVIEW, previous=B1))
    assert not env.git.rebase_in_progress(tree)
    assert sh(tree, "symbolic-ref", "--short", "HEAD").strip() == str(renamed)
    # HEAD を切り離した worktree でも、ブランチに移す
    sh(tree, "checkout", "-q", "--detach")
    cut(env, GitJob(7, J.STACK, task=T1, branch=renamed, base=OVERVIEW, previous=B1))
    assert sh(tree, "symbolic-ref", "--short", "HEAD").strip() == str(renamed)


def test_破棄した下のタスクのコミットは積み直したブランチに載らない(env: Env):
    top = overview(env)
    below = task_tree(env)
    commit(below, "below.py", "破棄するタスク\n")
    cut(env, GitJob(3, J.CUT_TASK, task=T2, branch=B2, base=B1))
    tree = env.paths.task_tree(T2)
    mine = commit(tree, "mine.py", "積み直すタスク\n")
    # task1 を破棄し、task2 を残した一番上（概要ブランチ）へ新しい名前で積み直す
    renamed = BranchName("stack/r--task-2-r1")
    job = GitJob(9, J.STACK, task=T2, branch=renamed, base=OVERVIEW, previous=B2)
    cut(env, job)
    assert rebase(env, job).conflicts == ()
    assert (tree / "mine.py").is_file() and not (tree / "below.py").exists()
    assert sh(tree, "rev-parse", "HEAD^").strip() == sh(top, "rev-parse", "HEAD").strip()
    assert (
        sh(tree, "show", "-s", "--format=%s", "HEAD").strip()
        == sh(tree, "show", "-s", "--format=%s", mine).strip()
    )


def test_上のタスクを切ったブランチを積み直しても仕事が消えない(env: Env):
    """task3 を task2 から切った後で task1 を破棄し、task2 を積み直す。task2 のコミットは task3 の
    ブランチからも辿れるが、根元から上を載せ直すので消えない。"""
    top = overview(env)
    below = task_tree(env)
    commit(below, "below.py", "破棄するタスク\n")
    cut(env, GitJob(3, J.CUT_TASK, task=T2, branch=B2, base=B1))
    tree = env.paths.task_tree(T2)
    commit(tree, "mine.py", "積み直すタスク\n")
    t3 = TaskId("task3")
    cut(env, GitJob(4, J.CUT_TASK, task=t3, branch=BranchName("stack/r--task-3"), base=B2))
    commit(env.paths.task_tree(t3), "upper.py", "上のタスク\n")
    renamed = BranchName("stack/r--task-2-r1")
    job = GitJob(9, J.STACK, task=T2, branch=renamed, base=OVERVIEW, previous=B2)
    cut(env, job)
    assert len(own_commits(context(env, S.REBASE, job=job), tools(env), tree)) == 1
    assert rebase(env, job).conflicts == ()
    assert (tree / "mine.py").is_file() and not (tree / "below.py").exists()
    assert sh(tree, "rev-parse", "HEAD^").strip() == sh(top, "rev-parse", "HEAD").strip()


def test_載せ直すコミットが無ければRebaseはブランチを動かさずに0件を返す(env: Env):
    """落とすかは Task が決める（EvidenceCheck.OWN_COMMITS）。中身は流さずに数を渡すだけ。"""
    top = overview(env)
    tree = task_tree(env)
    before = sh(tree, "rev-parse", "HEAD").strip()
    commit(top, "c.txt", "top\n")
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    outcome = rebase(env, job)
    assert (outcome.commits, outcome.result) == (0, None)
    assert sh(tree, "rev-parse", "HEAD").strip() == before


def test_Rebaseは載せ直す前に数えたコミットの数を返す(env: Env):
    """載せ直した後の HEAD は古い根元から辿れないので、実行器に後から数えさせない。"""
    top = overview(env)
    tree = task_tree(env)
    commit(tree, "b.txt", "task\n")
    commit(tree, "d.txt", "task\n")
    commit(top, "c.txt", "top\n")
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    assert rebase(env, job).commits == 2


def test_切り直したブランチは切り直した所から上だけを数えて載せ直す(env: Env):
    """積む列から外して始め直したタスクは、新しい名前（-r<n>）で一番上から切り直す。前のブランチの
    コミットは数えず、載せ直さない。"""
    top = overview(env)
    tree = task_tree(env)
    commit(tree, "old.py", "前の試み\n")
    commit(top, "top.txt", "top\n")
    again = BranchName("stack/r--task-1-r1")
    cut(env, GitJob(7, J.CUT_TASK, task=T1, branch=again, base=OVERVIEW))
    assert sh(tree, "symbolic-ref", "--short", "HEAD").strip() == str(again)
    commit(tree, "new.py", "始め直した試み\n")
    assert len(own_commits(context(env, S.GATE, task=T1), tools(env), tree)) == 1
    head = commit(top, "later.txt", "later\n")
    job = GitJob(8, J.STACK, task=T1, branch=again, base=OVERVIEW)
    assert rebase(env, job).result == {"onto": head}
    assert (tree / "new.py").is_file() and not (tree / "old.py").exists()
    assert sh(tree, "rev-parse", "HEAD^").strip() == head


def test_引き継いだタスクは引き継ぎ元のコミットを載せ直さない(env: Env):
    """統合に失敗した task1 を、差し込んだ task2 が引き継ぐ。task2 は一番上から切るので、task1 の
    ブランチのコミットは task2 の根元より上に無い。"""
    top = overview(env)
    failed = task_tree(env)
    commit(failed, "a.txt", "1\n2\ntask1\n3\n")
    commit(top, "a.txt", "1\n2\ntop\n3\n")
    cut(env, GitJob(9, J.CUT_TASK, task=T2, branch=B2, base=OVERVIEW))
    tree = env.paths.task_tree(T2)
    commit(tree, "a.txt", "1\n2\ntop\ntask1\n3\n")
    head = commit(top, "b.txt", "later\n")
    job = GitJob(10, J.STACK, task=T2, branch=B2, base=OVERVIEW)
    assert len(own_commits(context(env, S.REBASE, job=job), tools(env), tree)) == 1
    assert rebase(env, job).conflicts == ()
    assert sh(tree, "rev-parse", "HEAD^").strip() == head


def test_衝突しないrebaseは衝突したファイルを返さない(env: Env):
    top = overview(env)
    tree = task_tree(env)
    commit(tree, "b.txt", "task\n")
    head = commit(top, "c.txt", "top\n")
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    outcome = rebase(env, job)
    assert outcome.conflicts == ()
    # 載せ直した先（一番上の HEAD）が、タスクのブランチの新しい根元になる
    assert outcome.result == {"onto": head}
    assert sh(tree, "merge-base", "HEAD", head).strip() == head
    assert (tree / "c.txt").is_file()


def test_IntegrationCheckはラン共通の軽い検査と回帰テストを両方流す(env: Env):
    overview(env)
    task_tree(env)
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    ctx = context(
        env,
        S.INTEGRATION_CHECK,
        job=job,
        run_quick_checks=(VerifyCommand("test -f a.txt"),),
        run_regression_tests=(VerifyCommand("test -d ."),),
    )
    outcome = integration_check(ctx, tools(env))
    assert [(str(v.command), v.passed) for v in outcome.verify] == [
        ("test -f a.txt", True),
        ("test -d .", True),
    ]


def test_Pushはタスクのブランチをoriginへ送る(env: Env, tmp_path: Path):
    overview(env)
    tree = task_tree(env)
    head = commit(tree, "b.txt", "x\n")
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    push(context(env, S.PUSH, job=job), tools(env))
    remote = sh(tmp_path / "origin.git", "rev-parse", str(B1)).strip()
    assert remote == head


def body_ref(env: Env, text: str) -> ArtifactRef:
    path = env.paths.pr_body(T1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return ArtifactRef(A.PR_BODY, env.paths.relative(path))


def test_CreatePRは実装タスクが書いた本文と概要PRへの案内で作り題に概要PRの番号を付ける(env: Env):
    overview(env)
    task_tree(env)
    pr_list(env, B1, "[]")
    env.gh.reply(["pr", "create"], out="https://github.com/o/r/pull/12\n")
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    ctx = context(
        env,
        S.CREATE_PR,
        job=job,
        stack=StackFacts(OVERVIEW_ENTRY),
        target=TargetFacts("キャッシュを足す", body_ref(env, "本文 ${x} $$ \\1")),
    )
    assert create_pr(ctx, tools(env)).result == {"pr": 12}
    created = next(c for c in env.gh.calls() if c["args"][:2] == ["pr", "create"])
    assert created["args"][2:8] == [
        "--base",
        str(OVERVIEW),
        "--head",
        str(B1),
        "--title",
        "[autodev #5] キャッシュを足す",
    ]
    assert "概要 PR: #5" in created["stdin"]
    assert "本文 ${x} $$ \\1" in created["stdin"]
    assert "<!-- autodev:" not in created["stdin"]


def test_同じブランチのPRがあればCreatePRはそれを使う(env: Env):
    overview(env)
    task_tree(env)
    # 別のブランチの PR が混ざって返っても、自分のブランチの PR を使う
    others = json.loads(pr_json(9, str(B2), str(OVERVIEW)))
    pr_list(env, B1, json.dumps(others + json.loads(pr_json(12, str(B1), str(OVERVIEW)))))
    job = GitJob(6, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    ctx = context(env, S.CREATE_PR, job=job, stack=StackFacts(OVERVIEW_ENTRY))
    assert create_pr(ctx, tools(env)).result == {"pr": 12}
    assert not [c for c in env.gh.calls() if c["args"][:2] == ["pr", "create"]]


def test_StackLinkは概要PRから一番上まで全部を下から順にbase付きで渡す(env: Env):
    overview(env)
    task_tree(env, T2, B2)
    pr_list(env, B2, pr_json(13, str(B2), str(B1)))
    env.gh.reply(["pr", "view"], out=pr_json(5, str(OVERVIEW))[1:-1])
    job = GitJob(7, J.STACK, task=T2, branch=B2, base=B1)
    ctx = context(env, S.STACK_LINK, job=job, stack=StackFacts(OVERVIEW_ENTRY, (ENTRY1,)))
    assert stack_link(ctx, tools(env)).result == {"pr": 13}
    link = next(c for c in env.gh.calls() if c["args"][:2] == ["stack", "link"])
    assert link["args"] == ["stack", "link", "--base", "main", "5", "11", "13"]
    assert link["cwd"] == str(env.paths.overview_tree)


def test_つないだ後に概要PRのbaseが変わっていたら落とす(env: Env):
    overview(env)
    task_tree(env)
    pr_list(env, B1, pr_json(11, str(B1), str(OVERVIEW)))
    env.gh.reply(["pr", "view"], out=pr_json(5, str(OVERVIEW), base="develop")[1:-1])
    job = GitJob(7, J.STACK, task=T1, branch=B1, base=OVERVIEW)
    ctx = context(env, S.STACK_LINK, job=job, stack=StackFacts(OVERVIEW_ENTRY))
    with pytest.raises(RuntimeError, match="base が develop"):
        stack_link(ctx, tools(env))


# --- 概要 PR ---

OVERVIEW_FACTS = OverviewFacts(
    tasks=(
        TaskRow(T1, "キャッシュ", TaskStatus.STACKED, PrNumber(11)),
        TaskRow(T2, "掃除", TaskStatus.RUNNING),
    ),
    waiting=(WaitingRow(T2, "stall", "指摘 f1 が直らない\nA | B のどちらにするか"),),
    decisions=("TTL は 60 秒",),
    notes=(Decision("A にする", DecisionOrigin.USER, QuestionId("q1")),),
    deferrals=("多段のキャッシュ",),
)


def overview_job(kind: GitJobKind = J.OPEN_OVERVIEW) -> GitJob:
    return GitJob(8, kind, branch=OVERVIEW, base=BranchName("main"))


def test_CreateOverviewPRは空のコミットを載せてpushしdraftで作る(env: Env, tmp_path: Path):
    tree = overview(env)
    env.paths.overview_body.write_text("まとめ\n<!-- autodev:tasks -->\n", encoding="utf-8")
    env.paths.overview_title.write_text("キャッシュを足す\n", encoding="utf-8")
    pr_list(env, OVERVIEW, "[]")
    env.gh.reply(["pr", "create"], out="https://github.com/o/r/pull/5\n")
    ctx = context(env, S.CREATE_OVERVIEW_PR, job=overview_job(), overview=OVERVIEW_FACTS)
    assert create_overview_pr(ctx, tools(env)).result == {"pr": 5}
    assert sh(tree, "rev-list", "--count", "origin/main..HEAD").strip() == "1"
    assert (
        sh(tmp_path / "origin.git", "rev-parse", str(OVERVIEW)).strip()
        == sh(tree, "rev-parse", "HEAD").strip()
    )
    created = next(c for c in env.gh.calls() if c["args"][:2] == ["pr", "create"])
    assert "--draft" in created["args"]
    assert created["args"][created["args"].index("--title") + 1] == "[autodev] キャッシュを足す"
    assert "| task1 | キャッシュ | ✅ 積んだ | #11 |" in created["stdin"]
    # 呼び直しても空のコミットを重ねない
    env.gh.replies.clear()
    pr_list(env, OVERVIEW, pr_json(5, str(OVERVIEW), draft=True))
    create_overview_pr(ctx, tools(env))
    assert sh(tree, "rev-list", "--count", "origin/main..HEAD").strip() == "1"


def test_RefreshOverviewは保存した本文のマーカーを状態から埋め直す(env: Env):
    overview(env)
    saved = (
        "上の区画 ${tasks} $$ \\1\n"
        "<!-- autodev:tasks -->\n"
        "文の中の `<!-- autodev:tasks -->` は埋めない\n"
        "  <!-- autodev:waiting -->  \n"
        "<!-- autodev:decisions -->\n"
        "<!-- autodev:deferrals -->\n"
        "<!-- autodev:instruction -->\n"
        "<!-- autodev:signature -->\n"
        "<!-- autodev:unknown -->\n"
    )
    env.paths.overview_body.parent.mkdir(parents=True, exist_ok=True)
    env.paths.overview_body.write_text(saved, encoding="utf-8")
    env.paths.overview_title.write_text("キャッシュを足して掃除する", encoding="utf-8")
    ctx = context(
        env,
        S.REFRESH_OVERVIEW,
        job=overview_job(J.REWRITE_OVERVIEW),
        stack=StackFacts(OVERVIEW_ENTRY),
        overview=OVERVIEW_FACTS,
    )
    refresh_overview(ctx, tools(env))
    (edit,) = [c for c in env.gh.calls() if c["args"][:2] == ["pr", "edit"]]
    assert edit["args"][:5] == [
        "pr",
        "edit",
        "5",
        "--title",
        "[autodev] キャッシュを足して掃除する",
    ]
    body = edit["stdin"]
    assert body.startswith(
        "上の区画 ${tasks} $$ \\1\n"
        "| タスク | 件名 | 状態 | PR |\n"
        "| --- | --- | --- | --- |\n"
        "| task1 | キャッシュ | ✅ 積んだ | #11 |\n"
        "| task2 | 掃除 | 🔄 作業中 | — |\n"
    )
    assert "文の中の `<!-- autodev:tasks -->` は埋めない" in body
    # 理由の改行と `|` で表が崩れない
    assert (
        "## 回答を待っていること\n\n"
        "| 対象 | 種類 | 理由 |\n"
        "| --- | --- | --- |\n"
        "| task2 | `stall` | 指摘 f1 が直らない <br> A \\| B のどちらにするか |\n"
    ) in body
    assert (
        "| 決めたこと | 出どころ |\n"
        "| --- | --- |\n"
        "| TTL は 60 秒 | 計画 |\n"
        "| A にする | ユーザーの回答 |\n"
    ) in body
    assert "- 多段のキャッシュ" in body
    assert "> キャッシュを足す" in body
    assert "<sub>autodev のラン `r` が 2026-10-02 00:00 UTC に更新した</sub>" in body
    assert "<!-- autodev:unknown -->" in body
    # 保存した本文はマーカー入りのまま残す
    assert env.paths.overview_body.read_text(encoding="utf-8") == saved


def test_回答待ちが無ければRefreshOverviewは回答待ちの節ごと出さない(env: Env):
    overview(env)
    env.paths.overview_body.parent.mkdir(parents=True, exist_ok=True)
    env.paths.overview_body.write_text(
        "上の区画\n\n<!-- autodev:waiting -->\n\n---\n", encoding="utf-8"
    )
    env.paths.overview_title.write_text("キャッシュを足す", encoding="utf-8")
    ctx = context(
        env,
        S.REFRESH_OVERVIEW,
        job=overview_job(J.REWRITE_OVERVIEW),
        stack=StackFacts(OVERVIEW_ENTRY),
        overview=OverviewFacts(tasks=OVERVIEW_FACTS.tasks),
    )
    refresh_overview(ctx, tools(env))
    (edit,) = [c for c in env.gh.calls() if c["args"][:2] == ["pr", "edit"]]
    assert edit["stdin"] == "上の区画\n\n\n\n---\n"


def test_ReadyOverviewは概要PRをdraftから外す(env: Env):
    overview(env)
    ctx = context(
        env, S.READY_OVERVIEW, job=overview_job(J.FINISH), stack=StackFacts(OVERVIEW_ENTRY)
    )
    ready_overview(ctx, tools(env))
    assert [c["args"] for c in env.gh.calls()] == [["pr", "ready", "5"]]


# --- 破棄する ---

ENTRY2 = StackEntry(T2, B2, PrNumber(13), B1)
ENTRY3 = StackEntry(TaskId("task3"), BranchName("stack/r--task-3"), PrNumber(15), B2)


def discard_ctx(env: Env, stage: StageKind, cut_from: StackEntry | None) -> StageContext:
    job = GitJob(9, J.DISCARD, discarded=frozenset({T2}), cut_from=cut_from)
    return context(env, stage, job=job, stack=StackFacts(OVERVIEW_ENTRY, (ENTRY1, ENTRY2, ENTRY3)))


def test_破棄はClosePRsで閉じた所から上を閉じUnstackで解きRelinkで残りをつなぐ(env: Env):
    overview(env)
    env.gh.reply(["api"], out=json.dumps([{"number": 77}]))
    env.gh.reply(["pr", "view"], out=pr_json(5, str(OVERVIEW))[1:-1])
    for stage, program in (
        (S.CLOSE_PRS, close_prs),
        (S.UNSTACK, unstack),
        (S.RELINK, relink),
    ):
        program(discard_ctx(env, stage, ENTRY2), tools(env))
    args = [c["args"] for c in env.gh.calls()]
    assert args[:2] == [["pr", "close", "15"], ["pr", "close", "13"]]
    assert ["stack", "unstack", "77"] in args
    assert ["stack", "link", "--base", "main", "5", "11"] in args


def test_残したのが概要PRだけならRelinkはつながない(env: Env):
    overview(env)
    relink(discard_ctx(env, S.RELINK, ENTRY1), tools(env))
    assert env.gh.calls() == []


# --- Gate・Prepare ---


def test_Gateは証拠を集めて項目ごとの合否をGateEvaluatorに任せる(env: Env):
    overview(env)
    tree = task_tree(env)
    tests_at = commit(tree, "tests/test_cache.py", "def test(): pass\n")
    commit(tree, "src/cache.py", "x\n")
    commit(tree, "tests/test_cache.py", "def test(): assert True\n")
    facts = GateFacts((), (S.REVIEW,), (S.REVIEW,), has_test_gen=True)
    ctx = context(
        env,
        S.GATE,
        task=T1,
        gate=facts,
        artifacts={A.TESTS: ArtifactRef(A.TESTS, tests_at)},
        task_spec=TaskSpec("x", task_tests=(VerifyCommand("exit 3"),)),
    )
    outcome = gate(ctx, tools(env))
    assert outcome.commits == 3
    assert outcome.gate is not None
    failed = {r.item: r.reason for r in outcome.gate.failed}
    assert set(failed) == {GateItem.TESTS_UNCHANGED, GateItem.VERIFY}
    assert "tests/test_cache.py" in failed[GateItem.TESTS_UNCHANGED]
    assert [v.exit_code for v in outcome.verify] == [3]


def test_Gateはタスクのテストが全部通っても軽い検査が落ちればVERIFYの項目が落ちる(env: Env):
    overview(env)
    tree = task_tree(env)
    commit(tree, "src/cache.py", "x\n")
    ctx = context(
        env,
        S.GATE,
        task=T1,
        gate=GateFacts((), (S.REVIEW,), (S.REVIEW,), has_test_gen=False),
        task_spec=TaskSpec("x", task_tests=(VerifyCommand("true"),)),
        run_quick_checks=(VerifyCommand("exit 1"),),
        run_regression_tests=(VerifyCommand("exit 2"),),
    )
    outcome = gate(ctx, tools(env))
    assert outcome.gate is not None
    assert GateItem.VERIFY in {r.item for r in outcome.gate.failed}
    # 回帰テストは Gate では流れない
    assert [(str(v.command), v.exit_code) for v in outcome.verify] == [("true", 0), ("exit 1", 1)]


def test_ConfirmRedはタスクのテストと軽い検査を種類を付けて流し回帰テストは流さない(env: Env):
    overview(env)
    task_tree(env)
    ctx = context(
        env,
        S.CONFIRM_RED,
        task=T1,
        task_spec=TaskSpec("x", task_tests=(VerifyCommand("exit 4"),)),
        run_quick_checks=(VerifyCommand("true"),),
        run_regression_tests=(VerifyCommand("exit 9"),),
    )
    outcome = confirm_red(ctx, tools(env))
    assert [(str(v.command), v.exit_code, v.kind) for v in outcome.verify] == [
        ("exit 4", 4, VerifyKind.TASK_TESTS),
        ("true", 0, VerifyKind.QUICK_CHECKS),
    ]


def test_Gateが流した検証コマンドにも種類が付く(env: Env):
    overview(env)
    tree = task_tree(env)
    commit(tree, "src/cache.py", "x\n")
    ctx = context(
        env,
        S.GATE,
        task=T1,
        gate=GateFacts((), (S.REVIEW,), (S.REVIEW,), has_test_gen=False),
        task_spec=TaskSpec("x", task_tests=(VerifyCommand("true"),)),
        run_quick_checks=(VerifyCommand("exit 1"),),
    )
    outcome = gate(ctx, tools(env))
    assert [(str(v.command), v.kind) for v in outcome.verify] == [
        ("true", VerifyKind.TASK_TESTS),
        ("exit 1", VerifyKind.QUICK_CHECKS),
    ]


def test_TestGenが無ければ変わったファイルがテストの要らないパスに収まるかを見る(env: Env):
    overview(env)
    tree = task_tree(env)
    commit(tree, "docs/a.md", "x\n")
    facts = GateFacts((), (S.REVIEW,), (S.REVIEW,), has_test_gen=False)
    ctx = context(env, S.GATE, task=T1, gate=facts)
    passed = gate(ctx, tools(env)).gate
    assert passed is not None and passed.passed
    commit(tree, "src/b.py", "x\n")
    report = gate(ctx, tools(env)).gate
    assert report is not None
    assert [r.item for r in report.failed] == [GateItem.UNTESTED_PATHS]
    assert "src/b.py" in report.failed[0].reason


def test_Prepareは指示とリポジトリごとの設定からブリーフを書く(env: Env):
    overview(env)
    outcome = prepare(context(env, S.PREPARE, task=PLANNING), tools(env))
    assert outcome.products == (ArtifactRef(A.BRIEF, "brief.md"),)
    brief = env.paths.brief.read_text(encoding="utf-8")
    assert "キャッシュを足す" in brief and "- `docs/**`" in brief
    assert "<!-- autodev:" not in brief
    quick = brief.split("### 軽い検査")[1].split("###")[0]
    regression = brief.split("### 回帰テスト")[1].split("###")[0]
    assert "- `ruff check .`" in quick and "pytest" not in quick
    assert "- `pytest -q`" in regression and "ruff" not in regression


def test_Prepareは軽い検査も回帰テストも無ければ両方の節をなしにする(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bare = make_env(tmp_path, monkeypatch)
    overview(bare)
    prepare(context(bare, S.PREPARE, task=PLANNING), tools(bare))
    brief = bare.paths.brief.read_text(encoding="utf-8")
    assert brief.split("### 軽い検査")[1].split("###")[0].strip() == "（なし）"
    assert brief.split("### 回帰テスト")[1].split("###")[0].strip() == "（なし）"
