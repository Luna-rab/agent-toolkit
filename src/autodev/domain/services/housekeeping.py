"""ランの跡を外す（clean）・消す（purge）・呼び直す（run）のを拒む理由。

アプリケーション層が集めた証拠（`Leftovers`）だけで決める。実行器が証拠を集めて `Task.handle` が
決めるのと同じ分け方で、どの証拠でなぜ拒むかはここにしか書かない。

- `forcible` の理由は、失うものを承知で `--force` を付ければ越えられる
- 前の driver が起こしたプロセスがまだ走っているときは、`--force` でも越えさせない。走っている
  claude の下から worktree を消す・同じ実行を 2 本走らせることになり、失うものの話ではないため
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..value_objects.branch_name import BranchName
from ..value_objects.execution_id import ExecutionId


@dataclass(frozen=True)
class LiveProcess:
    """前の driver が起こして、まだ生きている子プロセス。"""

    pid: int
    command: str


@dataclass(frozen=True)
class WorktreeState:
    #: ランディレクトリからのパス（`trees/overview` など）
    path: str
    #: コミットしていない変更のあるファイル（無視されたファイルは含めない）
    dirty: tuple[str, ...] = ()
    #: HEAD から辿れて、どのブランチにも origin にも無いコミットの数（HEAD を切り離した worktree で
    #: 作ったコミット。ブランチの上の worktree では 0）
    detached_commits: int = 0


@dataclass(frozen=True)
class Leftovers:
    """ランの手元の跡について、外す・消す前に集めた証拠。"""

    #: 記録の上で running のまま残っている実行（driver が落ちた後は、走っていなくても残る）
    running: tuple[ExecutionId, ...] = ()
    #: ランが切った手元のブランチごとの、origin のどこにも無いコミットの数（fetch した後で数える）
    unpushed: tuple[tuple[BranchName, int], ...] = ()
    worktrees: tuple[WorktreeState, ...] = ()
    live: tuple[LiveProcess, ...] = ()
    #: 確かめられなかったこと（origin を fetch できない・壊れた worktree の中を読めない）。失うものが
    #: あるかは分からないが、壊れた所を片付けるのも clean・purge の役目なので、--force で越えられる
    unverified: tuple[str, ...] = ()


@dataclass(frozen=True)
class Blocker:
    reason: str
    forcible: bool = True


def _live(leftovers: Leftovers) -> list[Blocker]:
    return [
        Blocker(
            f"前の driver が起こしたプロセスがまだ走っている: pid {p.pid}（{p.command}）。"
            f"終わるのを待つか、kill {p.pid} で止めてから呼び直す",
            forcible=False,
        )
        for p in leftovers.live
    ]


def _worktrees(leftovers: Leftovers) -> list[Blocker]:
    found = [Blocker(reason) for reason in leftovers.unverified]
    for tree in leftovers.worktrees:
        if tree.dirty:
            found.append(
                Blocker(f"{tree.path} にコミットしていない変更がある: {', '.join(tree.dirty)}")
            )
        if tree.detached_commits:
            found.append(
                Blocker(
                    f"{tree.path} の切り離した HEAD にしか無いコミットがある"
                    f"（{tree.detached_commits} 件）"
                )
            )
    return found


def start_blockers(leftovers: Leftovers) -> list[Blocker]:
    """呼び直す前。前の driver の子が生きていると、同じ実行が 2 本走る。"""
    return _live(leftovers)


def clean_blockers(leftovers: Leftovers, *, finished: bool) -> list[Blocker]:
    """worktree を外す前。`finished` は `Run.complete`（ランを終え、仕上げの並びも終えた）。

    終えていないランの worktree を外すと、呼び直したステージの cwd が無く、続きから進めない。ブランチは
    残るので push していないコミットは失わないが、worktree の中の変更は失う。
    """
    blockers = _live(leftovers)
    if not finished:
        blockers.append(
            Blocker("ランを終えていない。worktree を外すと、呼び直しても続きから進めない")
        )
    return blockers + _worktrees(leftovers)


def purge_blockers(leftovers: Leftovers) -> list[Blocker]:
    """worktree・手元のブランチ・ランディレクトリを消す前。終えたかは見ない（やめたランを片付けるのも
    purge の役目で、失うものは下の証拠に出る）。"""
    blockers = _live(leftovers)
    blockers += [
        Blocker(f"記録の上で走っている実行がある: {execution}") for execution in leftovers.running
    ]
    # squash・rebase でマージした PR のコミットは origin のどこにも無いままなので、マージ済みのランでも
    # ここに当たる。数え方は変えず（マージ済みかは GitHub を見ないと分からない）、越え方を添える
    blockers += [
        Blocker(
            f"{branch} に origin に無いコミットがある（{count} 件。PR を squash か rebase で"
            "マージ済みなら、--force で消してよい）"
        )
        for branch, count in leftovers.unpushed
        if count
    ]
    return blockers + _worktrees(leftovers)


def blocking(blockers: Sequence[Blocker], *, force: bool) -> list[Blocker]:
    """`--force` を付けても残る理由。空なら進めてよい。"""
    return [b for b in blockers if not (force and b.forcible)]
