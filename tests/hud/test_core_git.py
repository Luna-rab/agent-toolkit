"""`git status --porcelain=v2 --branch` の出力の読み方（`hud/core/git.py`）。"""

from __future__ import annotations

from hud.core import git

OUTPUT = """\
# branch.oid 0123abcd
# branch.head main
1 M. N... 100644 100644 100644 aaa bbb c
1 .M N... 100644 100644 100644 aaa bbb a
2 RM N... 100644 100644 100644 aaa bbb R100 new\told
u UU N... 100644 100644 100644 100644 aaa bbb ccc conflict
? b
? d
"""


def test_ブランチと件数を数える():
    assert git.parse(OUTPUT) == git.GitStatus(branch="main", staged=3, modified=3, untracked=2)


def test_空の出力は何も数えない():
    assert git.parse("") == git.GitStatus(branch="", staged=0, modified=0, untracked=0)
