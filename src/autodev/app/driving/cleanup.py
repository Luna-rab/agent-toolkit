"""`autodev clean`・`purge` の中身: 外す・消す前の証拠を集め、ランの手元の跡を外す・消す。

**GitHub の PR とリモートのブランチには触らない。** 公開したものは消すと戻せない。消してよいかは
ここで決めず、集めた証拠（`Leftovers`）を `domain/services/housekeeping.py` に渡して聞く。
worktree を外すと、無視されたファイルも一緒に消える。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ...adapters.github.git import Git
from ...adapters.process import children
from ...adapters.process.command import CommandFailed
from ...domain.services.housekeeping import Leftovers, LiveProcess, WorktreeState
from ...domain.value_objects.branch_name import BranchName
from ...domain.value_objects.repository import Repository
from ...infra.paths import RunPaths
from .assembly import running_executions


def trees(paths: RunPaths) -> list[Path]:
    """ランの worktree の置き場（`trees/` の下のディレクトリ）。"""
    if not paths.trees.is_dir():
        return []
    return sorted(path for path in paths.trees.iterdir() if path.is_dir())


def live_children(paths: RunPaths) -> tuple[LiveProcess, ...]:
    """前の driver が起こして、まだ生きている子プロセス。"""
    return tuple(LiveProcess(c.pid, c.command) for c in children.survivors(paths.children))


def _worktree(paths: RunPaths, repo: Git, tree: Path) -> WorktreeState | str | None:
    """worktree の中の証拠。確かめられなければ、その理由の文。

    git に登録していないディレクトリ（作りかけで止まった など）は、git の変更も無いので数えない。
    登録してあるのに中を読めない（壊れた `.git`）なら、失うものがあるか分からないと返す。
    """
    if not repo.is_worktree(tree):
        return None
    where = paths.relative(tree)
    try:
        return WorktreeState(
            where,
            dirty=tuple(repo.dirty_files(tree)),
            detached_commits=repo.commits_off_refs(tree),
        )
    except CommandFailed as error:
        return f"{where} を確かめられなかった（{error}）"


def leftovers(paths: RunPaths, repository: Repository | None, *, count_unpushed: bool) -> Leftovers:
    """外す・消す前の証拠。`count_unpushed` なら、origin を fetch してから push していないコミットを
    数える（fetch しないと、ほかの口から push 済みのコミットも push していないと数える）。fetch できな
    ければ数えず、確かめられなかったとして渡す。"""
    live = live_children(paths)
    running = tuple(running_executions(paths))
    if repository is None:
        return Leftovers(running=running, live=live)
    repo = Git(repository.value)
    unpushed: tuple[tuple[BranchName, int], ...] = ()
    unverified: list[str] = []
    if count_unpushed:
        try:
            repo.fetch()
            unpushed = tuple(
                (branch, repo.unpushed_count(branch)) for branch in run_branches(paths, repository)
            )
        except CommandFailed as error:
            unverified.append(f"origin を確かめられなかった（{error}）")
    states: list[WorktreeState] = []
    for tree in trees(paths):
        found = _worktree(paths, repo, tree)
        if isinstance(found, str):
            unverified.append(found)
        elif found is not None:
            states.append(found)
    return Leftovers(
        running=running,
        unpushed=unpushed,
        worktrees=tuple(states),
        live=live,
        unverified=tuple(unverified),
    )


def remove_worktrees(paths: RunPaths, repository: Repository | None) -> list[Path]:
    """worktree を外し、実際に外したものを返す。ブランチと記録は残る。"""
    if repository is None:
        # 始める前に止まったラン。git に登録した worktree は無い
        return []
    repo = Git(repository.value)
    removed: list[Path] = []
    for tree in trees(paths):
        try:
            repo.remove_worktree(tree)
        except CommandFailed:
            # `.git` が壊れた worktree は git が外せない。ここへ来るのは、確かめられないことを承知で
            # --force を付けたときだけ。ランディレクトリの中なので消し、登録は prune で外す
            if not repo.is_worktree(tree):
                raise
            shutil.rmtree(tree)
        if not tree.exists():
            removed.append(tree)
    repo.prune_worktrees()
    return removed


def run_branches(paths: RunPaths, repository: Repository | None) -> list[BranchName]:
    """そのランが切った、手元のブランチ。"""
    if repository is None:
        return []
    return Git(repository.value).branches(BranchName.run_prefix(paths.name) + "*")


def purge(paths: RunPaths, repository: Repository | None) -> list[BranchName]:
    """worktree・手元のブランチ・ランディレクトリを消し、消したブランチを返す。"""
    remove_worktrees(paths, repository)
    branches = run_branches(paths, repository)
    if repository is not None:
        repo = Git(repository.value)
        for branch in branches:
            repo.delete_branch(branch)
    shutil.rmtree(paths.root)
    return branches
