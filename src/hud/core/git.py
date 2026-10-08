"""`git status --porcelain=v2 --branch` の出力を読む。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GitStatus:
    branch: str
    staged: int
    modified: int
    untracked: int


def parse(output: str) -> GitStatus:
    branch, staged, modified, untracked = "", 0, 0, 0
    for line in output.splitlines():
        if line.startswith("# branch.head "):
            branch = line.split(" ", 2)[2]
        elif line.startswith(("1 ", "2 ", "u ")):
            xy = line.split(" ", 2)[1]
            staged += xy[0] != "."
            modified += xy[1] != "."
        elif line.startswith("? "):
            untracked += 1
    return GitStatus(branch, staged, modified, untracked)
