"""Git: ブランチ・worktree・rebase・差分。

worktree はどれも対象リポジトリの外（ランディレクトリの `trees/`）に置く。対象リポジトリの手元の
ブランチと作業中のファイルには触らない。

呼び直しで同じ結果になるように作る。ブランチの
`already exists`・worktree の `already used by worktree` は、作り済みとして通す。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ...domain.value_objects.branch_name import BranchName
from ...domain.value_objects.commit_sha import CommitSha
from ..process.command import CommandFailed, Completed, checked, run, run_raw

#: git が対話で止まらないようにする（資格情報の入力・エディタ）。出力の文言で分岐する所があるので、
#: 言語を固定する
_ENV: dict[str, str | None] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
    "LC_ALL": "C",
}
#: 時間のかかるネットワークの操作の上限（秒）
NETWORK_TIMEOUT = 900


@dataclass(frozen=True)
class RebaseOutcome:
    """rebase の結果。衝突したら止まったまま返す（取りやめるか解くかは呼んだ側が決める）。"""

    completed: bool
    #: 衝突したファイル（リポジトリの根からのパス）
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConflictSides:
    """衝突したファイル 1 つの、共通の祖先・両側の中身。無い側（足した・消した）は None。

    rebase の中では `ours` が積む先（upstream）、`theirs` が積み直しているコミットである。
    """

    path: str
    base: bytes | None
    ours: bytes | None
    theirs: bytes | None


class WorktreeMismatch(RuntimeError):
    """在る worktree が、求めたものと違う（別のブランチ・ランディレクトリの外の古いディレクトリ）。"""


def toplevel(path: str | os.PathLike[str]) -> Path | None:
    """`path` を含む git のリポジトリの根。リポジトリの中でなければ None。"""
    got = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], env=_ENV)
    return Path(got.out.strip()) if got.ok and got.out.strip() else None


def available() -> bool:
    """`git` が PATH にあって起動できるか。"""
    return run(["git", "--version"], env=_ENV).ok


class Git:
    def __init__(self, repo: str | os.PathLike[str]) -> None:
        #: 対象リポジトリ（worktree を足す元）
        self.repo = Path(repo)

    # --- 低い層 ---

    def _git(
        self, cwd: str | os.PathLike[str], *args: str, timeout: float | None = 600
    ) -> Completed:
        return run(["git", "-C", str(cwd), *args], env=_ENV, timeout=timeout)

    def _ok(self, cwd: str | os.PathLike[str], *args: str, timeout: float | None = 600) -> str:
        return checked(self._git(cwd, *args, timeout=timeout)).out

    # --- リポジトリ ---

    def fetch(self) -> None:
        self._ok(self.repo, "fetch", "--prune", "origin", timeout=NETWORK_TIMEOUT)

    def default_branch(self) -> BranchName:
        """origin の既定ブランチ。取れなければ origin の main・master の順に探し、無ければ main。"""
        got = self._git(self.repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if got.ok and got.out.strip():
            return BranchName(got.out.strip().removeprefix("refs/remotes/origin/"))
        for name in ("main", "master"):
            if self._git(self.repo, "show-ref", "--verify", f"refs/remotes/origin/{name}").ok:
                return BranchName(name)
        return BranchName("main")

    def remote_branch_exists(self, branch: BranchName) -> bool:
        got = self._git(
            self.repo,
            "ls-remote",
            "--exit-code",
            "--heads",
            "origin",
            str(branch),
            timeout=NETWORK_TIMEOUT,
        )
        return got.ok

    # --- ブランチ ---

    def branch_exists(self, branch: BranchName) -> bool:
        return self._git(self.repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").ok

    def create_branch(self, branch: BranchName, start: str) -> None:
        """`start`（ブランチ名・`origin/<名前>`・SHA）からブランチを作る。在れば何もしない。"""
        got = self._git(self.repo, "branch", "--no-track", str(branch), start)
        if not got.ok and "already exists" not in got.err:
            raise CommandFailed(got)

    def delete_branch(self, branch: BranchName) -> None:
        got = self._git(self.repo, "branch", "-D", str(branch))
        if not got.ok and "not found" not in got.err:
            raise CommandFailed(got)

    def branches(self, prefix: str) -> list[BranchName]:
        """`refs/heads/<prefix>` に当たる手元のブランチ（例: `stack/<ラン名>--task-*`）。"""
        out = self._ok(
            self.repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}"
        )
        return [BranchName(line.strip()) for line in out.splitlines() if line.strip()]

    # --- worktree ---

    def prune_worktrees(self) -> None:
        """ランディレクトリを手で消しても git 側の登録は残り、ブランチが「別の場所でチェックアウト中」に
        なって作り直せない。worktree を作る前に通す。"""
        self._ok(self.repo, "worktree", "prune")

    def add_worktree(self, path: str | os.PathLike[str], branch: BranchName) -> None:
        """ブランチをチェックアウトした worktree を足す。同じブランチの worktree が在れば何もしない。"""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        got = self._git(self.repo, "worktree", "add", str(path), str(branch))
        if got.ok:
            return
        registered, checked_out = self._worktree(path)
        # 登録だけ残ってディレクトリが無いもの（手で消した）は、prune してから作り直す
        if not registered or not Path(path).is_dir():
            raise CommandFailed(got)
        if checked_out != f"refs/heads/{branch}":
            raise WorktreeMismatch(f"{path} は別のもの（{checked_out}）をチェックアウトしている")

    def switch_worktree(self, path: str | os.PathLike[str], branch: BranchName) -> None:
        """`path` を `branch` をチェックアウトした worktree にする。無ければ足す。

        在る worktree が HEAD を切り離した状態でも `branch` に移す。始め直す・積み直すタスクは新しい
        名前のブランチで切り直すので、在る worktree を別のブランチのまま残さない。rebase の途中なら
        `git checkout` が落ちるので、呼ぶ側が先に取りやめておく。
        """
        registered, _ = self._worktree(path)
        if not registered or not Path(path).is_dir():
            self.add_worktree(path, branch)
            return
        if self.current_branch(path) != branch:
            self.checkout(path, branch)

    def add_detached_worktree(
        self, path: str | os.PathLike[str], commit: str, *, run_dir: str | os.PathLike[str]
    ) -> None:
        """HEAD を `commit` に固定した、切り離した状態の worktree を切り直す（読むためだけ。ブランチは作らない）。

        在れば消してから作る。前の再計画の HEAD のまま残さないため。git に登録されていない古い
        ディレクトリは、ランディレクトリの中にあると確かめてから消す（外のものは消さない）。
        """
        target = Path(path)
        registered, _ = self._worktree(target)
        if registered:
            self.remove_worktree(target)
        elif target.exists():
            if not _inside(target, Path(run_dir)):
                raise WorktreeMismatch(f"ランディレクトリの外のディレクトリは消さない: {target}")
            shutil.rmtree(target)
        self.prune_worktrees()
        target.parent.mkdir(parents=True, exist_ok=True)
        self._ok(self.repo, "worktree", "add", "--detach", str(target), commit)

    def remove_worktree(self, path: str | os.PathLike[str]) -> None:
        """worktree を消すと、無視されたファイルも一緒に消える（実測）。
        push が済んでいるかを確かめるのは呼んだ側である。"""
        got = self._git(self.repo, "worktree", "remove", "--force", str(path))
        if not got.ok and "is not a working tree" not in got.err:
            raise CommandFailed(got)

    def is_worktree(self, path: str | os.PathLike[str]) -> bool:
        """`path` が、このリポジトリに登録した worktree か（中の `.git` が壊れていても、登録で見る）。"""
        return self._worktree(path)[0]

    def _worktree(self, path: str | os.PathLike[str]) -> tuple[bool, str | None]:
        """（git に登録されているか, チェックアウトしているもの）。ブランチなら `refs/heads/…`、
        切り離した状態なら `detached`。"""
        want = os.path.realpath(path)
        # `-z` は git 2.36 から。改行の入ったパスは置かないので、行で読む
        out = self._ok(self.repo, "worktree", "list", "--porcelain")
        for block in out.split("\n\n"):
            fields = [f for f in block.split("\n") if f]
            if not fields or not fields[0].startswith("worktree "):
                continue
            if os.path.realpath(fields[0][len("worktree ") :]) != want:
                continue
            for field in fields[1:]:
                if field.startswith("branch "):
                    return True, field[len("branch ") :]
                if field == "detached":
                    return True, "detached"
            return True, None
        return False, None

    # --- コミット ---

    def head(self, tree: str | os.PathLike[str]) -> CommitSha:
        return CommitSha(self._ok(tree, "rev-parse", "HEAD").strip())

    def rev_parse(self, tree: str | os.PathLike[str], rev: str) -> CommitSha | None:
        got = self._git(tree, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        return CommitSha(got.out.strip()) if got.ok else None

    def commit_empty(self, tree: str | os.PathLike[str], message: str) -> CommitSha:
        """空のコミットを 1 つ載せる。base との差分が 0 だと `gh pr create` が落ちる。"""
        self._ok(tree, "commit", "--allow-empty", "--no-verify", "-m", message)
        return self.head(tree)

    def commit_count(self, tree: str | os.PathLike[str], base: str, head: str = "HEAD") -> int:
        return int(self._ok(tree, "rev-list", "--count", f"{base}..{head}").strip())

    def commits_since(
        self, tree: str | os.PathLike[str], base: str, head: str = "HEAD"
    ) -> list[CommitSha]:
        """`base` から辿れず `head` から辿れるコミット（`base..head`、古い順）。"""
        out = self._ok(tree, "rev-list", "--reverse", f"{base}..{head}")
        return [CommitSha(line) for line in out.split() if line]

    def checkout(self, tree: str | os.PathLike[str], branch: BranchName) -> None:
        """worktree のチェックアウトを `branch` に移す（積み直す・始め直すときの新しい名前のブランチ）。"""
        self._ok(tree, "checkout", "--quiet", str(branch))

    def current_branch(self, tree: str | os.PathLike[str]) -> BranchName | None:
        got = self._git(tree, "symbolic-ref", "--quiet", "--short", "HEAD")
        return BranchName(got.out.strip()) if got.ok and got.out.strip() else None

    def reset_hard(self, tree: str | os.PathLike[str], commit: str) -> None:
        """`commit` に戻し、追跡していない新しいファイルも消す（無視されたファイルは残す）。"""
        self._ok(tree, "reset", "--quiet", "--hard", commit)
        self._ok(tree, "clean", "-fdq")

    def reset_keep(self, tree: str | os.PathLike[str], commit: str) -> None:
        """`commit` に戻す。HEAD と `commit` の間で変わるファイルにコミットしていない変更があれば、
        消さずに落ちる（`reset --keep`）。HEAD がもう `commit` なら何もしない（汚れた worktree も残す）。"""
        self._ok(tree, "reset", "--quiet", "--keep", commit)

    def restore_conflicts(self, tree: str | os.PathLike[str], paths: Sequence[str]) -> None:
        """rebase の途中で、解きかけたファイルを衝突の印のある状態に戻す（index の段から作り直す）。"""
        if paths:
            self._ok(tree, "checkout", "--merge", "--", *paths)

    def merge_base(self, tree: str | os.PathLike[str], a: str, b: str) -> CommitSha | None:
        got = self._git(tree, "merge-base", a, b)
        return CommitSha(got.out.strip()) if got.ok else None

    # --- 差分 ---

    def changed_files(
        self, tree: str | os.PathLike[str], base: str, head: str = "HEAD"
    ) -> list[str]:
        """`base` から `head` までに変わったファイル。`base...head` ではなく 2 点の比較である。"""
        out = self._ok(tree, "diff", "--name-only", "--no-renames", base, head)
        return [line for line in out.splitlines() if line]

    def diff(
        self,
        tree: str | os.PathLike[str],
        base: str,
        head: str = "HEAD",
        paths: Sequence[str] = (),
    ) -> str:
        args = ["diff", "--no-color", base, head]
        if paths:
            args += ["--", *paths]
        return self._ok(tree, *args)

    def dirty_files(self, tree: str | os.PathLike[str]) -> list[str]:
        """コミットしていない変更のあるファイル（追跡していない新しいファイルを含む）。"""
        out = self._ok(tree, "status", "--porcelain", "-z", "--untracked-files=all")
        entries = out.split("\0")
        found: list[str] = []
        skip = False
        for entry in entries:
            if skip:
                # 名前を変えたときは、元の名前が後ろに続く
                skip = False
                continue
            if not entry:
                continue
            found.append(entry[3:])
            skip = entry[0] in "RC"
        return found

    def tracked_files(self, tree: str | os.PathLike[str]) -> list[str]:
        return [p for p in self._ok(tree, "ls-files", "-z").split("\0") if p]

    def show(self, tree: str | os.PathLike[str], rev: str, path: str) -> bytes | None:
        """`rev` の時点の `path` の中身。そのコミットに無ければ None。

        バイト列で返す。utf-8 でないファイルを文字列にすると置き換えた字が混ざり、両側の行を
        残したかの比べ方が狂う。
        """
        got = run_raw(["git", "-C", str(tree), "show", f"{rev}:{path}"], env=_ENV, timeout=600)
        return got.out if got.ok else None

    # --- rebase ---

    def rebase(
        self, tree: str | os.PathLike[str], onto: str, upstream: str | None = None
    ) -> RebaseOutcome:
        """`tree` のブランチを `onto` の上へ rebase する。衝突したら止まったまま返す。

        `upstream` を渡すと、そこから辿れるコミットを除いた分だけを載せ直す（`--onto`）。
        """
        args = ["rebase", "--onto", onto, upstream] if upstream is not None else ["rebase", onto]
        got = self._git(tree, *args)
        if got.ok:
            return RebaseOutcome(completed=True)
        conflicts = self.conflicted_files(tree)
        if not conflicts:
            raise CommandFailed(got)
        return RebaseOutcome(completed=False, conflicts=tuple(conflicts))

    def rebase_continue(self, tree: str | os.PathLike[str]) -> RebaseOutcome:
        """衝突したファイルを足して先へ進める。次のコミットでまた衝突したら止まったまま返す。

        足すのは衝突したファイルだけで、ほかに置かれたファイルはコミットに混ぜない。
        """
        conflicted = self.conflicted_files(tree)
        if conflicted:
            self._ok(tree, "add", "--", *conflicted)
        got = self._git(tree, "rebase", "--continue")
        while not got.ok and self._replayed_commit_is_empty(tree):
            # 解いた結果が積む先と同じになり、積み直しているコミットが空になった。空のコミットは
            # 残さずに飛ばす（git rebase が衝突の無い空のコミットを落とすのと同じ）
            got = self._git(tree, "rebase", "--skip")
        if got.ok:
            return RebaseOutcome(completed=True)
        conflicts = self.conflicted_files(tree)
        if not conflicts:
            raise CommandFailed(got)
        return RebaseOutcome(completed=False, conflicts=tuple(conflicts))

    def _replayed_commit_is_empty(self, tree: str | os.PathLike[str]) -> bool:
        if not self.rebase_in_progress(tree) or self.conflicted_files(tree):
            return False
        staged = self._git(tree, "diff", "--cached", "--quiet")
        unstaged = self._git(tree, "diff", "--quiet")
        return staged.ok and unstaged.ok

    def rebase_abort(self, tree: str | os.PathLike[str]) -> None:
        if self.rebase_in_progress(tree):
            self._ok(tree, "rebase", "--abort")

    def remove_index_lock(self, tree: str | os.PathLike[str]) -> bool:
        """`tree` の worktree に残った `index.lock` を消す。消したら真。

        lock を持つ git が走っていないと確かめてから呼ぶ（走っている git の lock を消すと index が壊れる）。
        """
        path = Path(tree) / self._ok(tree, "rev-parse", "--git-path", "index.lock").strip()
        if not path.exists():
            return False
        path.unlink(missing_ok=True)
        return True

    def rebase_in_progress(self, tree: str | os.PathLike[str]) -> bool:
        for name in ("rebase-merge", "rebase-apply"):
            path = self._ok(tree, "rev-parse", "--git-path", name).strip()
            # 相対のパスは tree から数える（絶対のパスなら Path の / はそれを返す）
            if (Path(tree) / path).exists():
                return True
        return False

    def conflicted_files(self, tree: str | os.PathLike[str]) -> list[str]:
        out = self._ok(tree, "diff", "--name-only", "--diff-filter=U", "-z")
        return [p for p in out.split("\0") if p]

    def conflict_sides(self, tree: str | os.PathLike[str], path: str) -> ConflictSides:
        """衝突している間（解いて `git add` する前）だけ取れる。index の段 1・2・3 から読む。"""
        return ConflictSides(
            path=path,
            base=self.show(tree, ":1", path),
            ours=self.show(tree, ":2", path),
            theirs=self.show(tree, ":3", path),
        )

    def read_file(self, tree: str | os.PathLike[str], path: str) -> bytes | None:
        """worktree の中のファイルの今の中身（解いた結果）。無ければ None。`show` と同じくバイト列。"""
        full = Path(tree) / path
        if not full.is_file():
            return None
        return full.read_bytes()

    # --- push ---

    def push(self, tree: str | os.PathLike[str], branch: BranchName) -> None:
        """force push はしない。積み直すときは新しいブランチ名で切り直す。"""
        self._ok(
            tree,
            "push",
            "--set-upstream",
            "origin",
            f"{branch}:{branch}",
            timeout=NETWORK_TIMEOUT,
        )

    def commits_off_refs(self, tree: str | os.PathLike[str]) -> int:
        """`tree` の HEAD から辿れて、どの手元のブランチにも origin にも無いコミットの数（HEAD を
        切り離した worktree で作ったコミット。worktree を外すと辿れなくなる）。"""
        out = self._ok(
            tree, "rev-list", "--count", "HEAD", "--not", "--branches", "--remotes=origin"
        )
        return int(out.strip())

    def unpushed_count(self, branch: BranchName) -> int:
        """origin のどのブランチにも無いコミットの数。worktree やブランチを消すと失う数である。"""
        out = self._ok(self.repo, "rev-list", "--count", str(branch), "--not", "--remotes=origin")
        return int(out.strip())


def _inside(path: Path, root: Path) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)
