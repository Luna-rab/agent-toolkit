"""Git: tmp_path に作った本物のリポジトリで確かめる。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autodev.adapters.github.git import Git, WorktreeMismatch
from autodev.adapters.process.command import CommandFailed
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.commit_sha import CommitSha


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def commit(cwd: Path, path: str, text: str, message: str) -> None:
    (cwd / path).parent.mkdir(parents=True, exist_ok=True)
    (cwd / path).write_text(text, encoding="utf-8")
    sh(cwd, "add", path)
    sh(cwd, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """origin（bare）と、それを clone した対象リポジトリ。"""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    commit(work, "a.txt", "1\n2\n3\n", "first")
    sh(work, "push", "-q", "origin", "main")
    sh(work, "remote", "set-head", "origin", "--auto")
    return work


def test_既定のブランチをoriginから取る(repo: Path):
    assert Git(repo).default_branch() == BranchName("main")


def test_ブランチとworktreeは呼び直しても同じ結果になる(repo: Path, tmp_path: Path):
    """`already exists`・`already used by worktree` は作り済みとして通す。"""
    git = Git(repo)
    branch = BranchName("stack/r--task-0")
    tree = tmp_path / "run" / "trees" / "overview"
    git.prune_worktrees()
    git.create_branch(branch, "origin/main")
    git.create_branch(branch, "origin/main")
    git.add_worktree(tree, branch)
    git.add_worktree(tree, branch)
    assert git.branch_exists(branch)
    assert git.branches("stack/r--task-*") == [branch]
    assert (tree / "a.txt").is_file()


def test_手で消したworktreeはpruneすると作り直せる(repo: Path, tmp_path: Path):
    """ランディレクトリを手で消しても git 側の登録は残る。"""
    git = Git(repo)
    branch = BranchName("stack/r--task-1")
    tree = tmp_path / "run" / "trees" / "task1"
    git.create_branch(branch, "main")
    git.add_worktree(tree, branch)
    subprocess.run(["rm", "-rf", str(tree)], check=True)
    with pytest.raises(CommandFailed):
        git.add_worktree(tree, branch)
    git.prune_worktrees()
    git.add_worktree(tree, branch)
    assert (tree / "a.txt").is_file()


def test_空のコミットで親との差分を作る(repo: Path, tmp_path: Path):
    """base との差分が 0 だと gh pr create が落ちる。"""
    git = Git(repo)
    branch = BranchName("stack/r--task-0")
    tree = tmp_path / "trees" / "overview"
    git.create_branch(branch, "origin/main")
    git.add_worktree(tree, branch)
    assert git.commit_count(tree, "origin/main") == 0
    sha = git.commit_empty(tree, "chore: 概要ブランチ")
    assert git.commit_count(tree, "origin/main") == 1
    assert git.head(tree) == sha
    assert git.rev_parse(tree, "HEAD") == sha
    assert git.rev_parse(tree, "no-such-ref") is None


def test_HEADを固定したworktreeを切り直す(repo: Path, tmp_path: Path):
    git = Git(repo)
    first = git.head(repo)
    commit(repo, "b.txt", "b\n", "second")
    second = git.head(repo)
    top = tmp_path / "trees" / "stack-top"
    git.add_detached_worktree(top, str(first), run_dir=tmp_path)
    assert git.head(top) == first
    git.add_detached_worktree(top, str(second), run_dir=tmp_path)
    assert git.head(top) == second
    assert sh(top, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"


def test_登録されていない古いディレクトリはランディレクトリの中だけ消す(repo: Path, tmp_path: Path):
    git = Git(repo)
    run_dir = tmp_path / "run"
    stale = run_dir / "trees" / "stack-top"
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("old\n", encoding="utf-8")
    git.add_detached_worktree(stale, "HEAD", run_dir=run_dir)
    assert not (stale / "old.txt").exists()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
    with pytest.raises(WorktreeMismatch):
        git.add_detached_worktree(outside, "HEAD", run_dir=run_dir)
    assert (outside / "keep.txt").is_file()


def test_在るworktreeが別のブランチなら作り済みとして通さない(repo: Path, tmp_path: Path):
    git = Git(repo)
    tree = tmp_path / "trees" / "task1"
    git.create_branch(BranchName("a"), "main")
    git.create_branch(BranchName("b"), "main")
    git.add_worktree(tree, BranchName("a"))
    with pytest.raises(WorktreeMismatch):
        git.add_worktree(tree, BranchName("b"))


def test_差分と変わったファイルと中身を取り出す(repo: Path):
    git = Git(repo)
    base = str(git.head(repo))
    commit(repo, "src/x.py", "x = 1\n", "add x")
    commit(repo, "a.txt", "1\n2\n3\n4\n", "grow a")
    assert sorted(git.changed_files(repo, base)) == ["a.txt", "src/x.py"]
    assert "+4" in git.diff(repo, base, paths=["a.txt"])
    assert git.show(repo, base, "a.txt") == b"1\n2\n3\n"
    assert git.show(repo, base, "src/x.py") is None
    assert git.commit_count(repo, base) == 2
    assert git.merge_base(repo, base, "HEAD") == CommitSha(base)


def test_コミットしていない変更を一覧する(repo: Path):
    git = Git(repo)
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new dir").mkdir()
    (repo / "new dir" / "n.txt").write_text("n\n", encoding="utf-8")
    assert sorted(git.dirty_files(repo)) == ["a.txt", "new dir/n.txt"]
    assert git.tracked_files(repo) == ["a.txt"]


def test_rebaseが衝突したら止まったまま衝突したファイルと両側を返す(repo: Path, tmp_path: Path):
    git = Git(repo)
    branch = BranchName("stack/r--task-1")
    tree = tmp_path / "trees" / "task1"
    git.create_branch(branch, "main")
    git.add_worktree(tree, branch)
    commit(tree, "a.txt", "1\n2\nfrom-task\n", "task")
    commit(repo, "a.txt", "1\n2\nfrom-main\n", "main moves")

    got = git.rebase(tree, "main")
    assert not got.completed
    assert got.conflicts == ("a.txt",)
    assert git.rebase_in_progress(tree)
    sides = git.conflict_sides(tree, "a.txt")
    assert sides.base == b"1\n2\n3\n"
    assert sides.ours == b"1\n2\nfrom-main\n"
    assert sides.theirs == b"1\n2\nfrom-task\n"

    (tree / "a.txt").write_text("1\n2\nfrom-main\nfrom-task\n", encoding="utf-8")
    (tree / "stray.txt").write_text("混ぜない\n", encoding="utf-8")
    assert git.read_file(tree, "a.txt") == b"1\n2\nfrom-main\nfrom-task\n"
    done = git.rebase_continue(tree)
    assert done.completed
    assert not git.rebase_in_progress(tree)
    assert git.show(tree, "HEAD", "stray.txt") is None
    assert git.commit_count(tree, "main") == 1


def test_gitは言語を固定して呼ぶ(repo: Path):
    """`already exists` などの文言で分岐するので、翻訳された文言で外さない。"""
    assert Git(repo)._ok(repo, "-c", "alias.lc=!printenv LC_ALL", "lc").strip() == "C"


def test_utf8でない中身もバイト列のまま取り出す(repo: Path):
    git = Git(repo)
    (repo / "latin1.txt").write_bytes(b"caf\xe9\n")
    sh(repo, "add", "latin1.txt")
    sh(repo, "commit", "-q", "-m", "latin1")
    assert git.show(repo, "HEAD", "latin1.txt") == b"caf\xe9\n"


def test_解いた結果が空のコミットになったら飛ばして進める(repo: Path, tmp_path: Path):
    git = Git(repo)
    branch = BranchName("stack/r--task-1")
    tree = tmp_path / "trees" / "task1"
    git.create_branch(branch, "main")
    git.add_worktree(tree, branch)
    commit(tree, "a.txt", "task\n", "task")
    commit(tree, "t.txt", "t\n", "second task commit")
    commit(repo, "a.txt", "main\n", "main moves")
    assert not git.rebase(tree, "main").completed
    # 積む先の側をそのまま採ると、積み直しているコミットは空になる
    (tree / "a.txt").write_text("main\n", encoding="utf-8")
    assert git.rebase_continue(tree).completed
    assert git.commit_count(tree, "main") == 1
    assert git.show(tree, "HEAD", "t.txt") == b"t\n"


def test_rebaseを取りやめると元に戻る(repo: Path, tmp_path: Path):
    git = Git(repo)
    branch = BranchName("stack/r--task-1")
    tree = tmp_path / "trees" / "task1"
    git.create_branch(branch, "main")
    git.add_worktree(tree, branch)
    commit(tree, "a.txt", "task\n", "task")
    before = git.head(tree)
    commit(repo, "a.txt", "main\n", "main moves")
    assert not git.rebase(tree, "main").completed
    git.rebase_abort(tree)
    git.rebase_abort(tree)
    assert git.head(tree) == before


def test_衝突しないrebaseは最後まで進む(repo: Path, tmp_path: Path):
    git = Git(repo)
    branch = BranchName("stack/r--task-1")
    tree = tmp_path / "trees" / "task1"
    git.create_branch(branch, "main")
    git.add_worktree(tree, branch)
    commit(tree, "t.txt", "t\n", "task")
    commit(repo, "m.txt", "m\n", "main moves")
    assert git.rebase(tree, "main").completed
    assert git.commit_count(tree, "main") == 1


def test_pushするとoriginにブランチができる(repo: Path, tmp_path: Path):
    git = Git(repo)
    branch = BranchName("stack/r--task-1")
    tree = tmp_path / "trees" / "task1"
    git.create_branch(branch, "main")
    git.add_worktree(tree, branch)
    commit(tree, "t.txt", "t\n", "task")
    assert not git.remote_branch_exists(branch)
    assert git.unpushed_count(branch) == 1
    git.push(tree, branch)
    assert git.remote_branch_exists(branch)
    git.fetch()
    assert git.unpushed_count(branch) == 0


def test_worktreeとブランチを消す(repo: Path, tmp_path: Path):
    git = Git(repo)
    branch = BranchName("stack/r--task-1")
    tree = tmp_path / "trees" / "task1"
    git.create_branch(branch, "main")
    git.add_worktree(tree, branch)
    git.remove_worktree(tree)
    git.remove_worktree(tree)
    git.delete_branch(branch)
    git.delete_branch(branch)
    assert not tree.exists()
    assert not git.branch_exists(branch)
