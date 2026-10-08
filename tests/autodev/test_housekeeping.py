"""ランの跡を外す・消すのを拒む理由（`domain/services/housekeeping.py`）。証拠だけで決まり、I/O は無い。"""

from __future__ import annotations

from autodev.domain.services.housekeeping import (
    Leftovers,
    LiveProcess,
    WorktreeState,
    blocking,
    clean_blockers,
    purge_blockers,
    start_blockers,
)
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.task_id import TaskId

PLAN = ExecutionId(TaskId.planning(), StageKind.PLAN, 0, 1)
BRANCH = BranchName("stack/r--task-0")
CLAUDE = LiveProcess(4242, "claude -p")


def reasons(blockers) -> list[str]:
    return [blocker.reason for blocker in blockers]


def test_何も残っていなければ拒まない():
    assert clean_blockers(Leftovers(), finished=True) == []
    assert purge_blockers(Leftovers()) == []
    assert start_blockers(Leftovers()) == []


def test_cleanはランを終えていなければ拒みforceで越えられる():
    blockers = clean_blockers(Leftovers(), finished=False)
    assert reasons(blockers) == [
        "ランを終えていない。worktree を外すと、呼び直しても続きから進めない"
    ]
    assert blocking(blockers, force=False) == blockers
    assert blocking(blockers, force=True) == []


def test_purgeは走っている実行とpushしていないコミットと未コミットの変更と切り離したHEADのコミットを挙げる():
    leftovers = Leftovers(
        running=(PLAN,),
        unpushed=((BRANCH, 2), (BranchName("stack/r--task-1"), 0)),
        worktrees=(
            WorktreeState("trees/overview", dirty=("wip.txt",)),
            WorktreeState("trees/stack-top", detached_commits=1),
            WorktreeState("trees/task1"),
        ),
    )
    assert reasons(purge_blockers(leftovers)) == [
        f"記録の上で走っている実行がある: {PLAN}",
        f"{BRANCH} に origin に無いコミットがある（2 件。PR を squash か rebase でマージ済み"
        "なら、--force で消してよい）",
        "trees/overview にコミットしていない変更がある: wip.txt",
        "trees/stack-top の切り離した HEAD にしか無いコミットがある（1 件）",
    ]
    assert blocking(purge_blockers(leftovers), force=True) == []


def test_cleanも未コミットの変更と切り離したHEADのコミットを失うので拒む():
    leftovers = Leftovers(
        worktrees=(WorktreeState("trees/overview", dirty=("a", "b"), detached_commits=3),)
    )
    assert reasons(clean_blockers(leftovers, finished=True)) == [
        "trees/overview にコミットしていない変更がある: a, b",
        "trees/overview の切り離した HEAD にしか無いコミットがある（3 件）",
    ]


def test_生きている子プロセスはどれもforceでも越えられない():
    leftovers = Leftovers(live=(CLAUDE,))
    for blockers in (
        clean_blockers(leftovers, finished=True),
        purge_blockers(leftovers),
        start_blockers(leftovers),
    ):
        assert reasons(blockers) == [
            "前の driver が起こしたプロセスがまだ走っている: pid 4242（claude -p）。"
            "終わるのを待つか、kill 4242 で止めてから呼び直す"
        ]
        assert blocking(blockers, force=True) == blockers


def test_確かめられなかったことはforceで越えられる理由にする():
    leftovers = Leftovers(unverified=("origin を確かめられなかった（fetch が落ちた）",))
    for blockers in (clean_blockers(leftovers, finished=True), purge_blockers(leftovers)):
        assert reasons(blockers) == ["origin を確かめられなかった（fetch が落ちた）"]
        assert blocking(blockers, force=True) == []
