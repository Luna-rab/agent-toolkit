"""Forge: GitHub の PR と stacked PR（`gh` と `gh stack`）。

触るのは git 管理タスクの決定的なステージだけ。`gh stack` のローカルの追跡は
worktree ごとに別なので使わない。`gh stack link` は追跡に依らないので、PR を
`gh pr create` で自分のタイトルと本文で作り、link で連ねる。`gh stack` は概要ブランチの
worktree（`trees/overview`）を cwd にして叩く。

確かめた範囲（gh-stack v0.1.0 の `--help`・GitHub の REST API の文書。2026-10-02）:

- `gh stack link [--base <ブランチ>] <PR>...`: 下から順に渡す。足すだけで、すでにスタックにある PR を
  外さない。`--base` を省くと一番下の base がリポジトリの既定ブランチになる。ブランチ名を
  渡すと push と PR の作成までするので、PR 番号だけを渡す。`--open` は付けない（draft を外してしまう）
- `gh stack unstack <スタックの番号>`: 対話なしで、GitHub の API を通してスタックを解く。手元の追跡は
  要らない
- スタックの番号は REST の `GET /repos/{owner}/{repo}/stacks?pull_request=<PR 番号>` で引く
  （API の版 `2026-03-10`）。返るのはスタックの配列で、各要素の `number` が番号である。実物の
  応答で確かめていない
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ...domain.value_objects.branch_name import BranchName
from ...domain.value_objects.pr_number import PrNumber
from ..process.command import CommandFailed, Completed, run

#: `gh pr create` の出力の最後の行の URL
_PR_URL = re.compile(r"/pull/(\d+)\s*$")
#: スタックの REST API の版（文書の値）
STACKS_API_VERSION = "2026-03-10"
#: 対話で止めない。出力の文言で分岐する所があるので、言語を固定する
_ENV: dict[str, str | None] = {
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "LC_ALL": "C",
}
NETWORK_TIMEOUT = 900


@dataclass(frozen=True)
class PullRequest:
    number: PrNumber
    head: str
    base: str
    #: `OPEN`・`CLOSED`・`MERGED`
    state: str
    draft: bool


class ForgeError(CommandFailed):
    """gh が落ちた。"""


class Forge:
    def __init__(self, gh: str = "gh") -> None:
        self.gh = gh

    def _run(self, cwd: str | os.PathLike[str], *args: str, stdin: str | None = None) -> Completed:
        return run([self.gh, *args], cwd=cwd, env=_ENV, stdin=stdin, timeout=NETWORK_TIMEOUT)

    def _ok(self, cwd: str | os.PathLike[str], *args: str, stdin: str | None = None) -> str:
        got = self._run(cwd, *args, stdin=stdin)
        if not got.ok:
            raise ForgeError(got)
        return got.out

    # --- 前提 ---

    def missing(self, cwd: str | os.PathLike[str]) -> list[str]:
        """足りないもの。空なら使える（走り出す前に確かめる）。"""
        if not self._run(cwd, "auth", "status").ok:
            return ["gh が認証されていない（`gh auth login`）"]
        listed = self._run(cwd, "extension", "list")
        if not listed.ok or "gh-stack" not in listed.out:
            return ["gh stack の拡張が入っていない（`gh extension install github/gh-stack`）"]
        return []

    # --- PR ---

    def create_pr(
        self,
        tree: str | os.PathLike[str],
        *,
        base: BranchName,
        head: BranchName,
        title: str,
        body: str,
        draft: bool = False,
    ) -> PrNumber:
        """本文は標準入力から渡す（`--body-file -`）。一時ファイルを置かずに済む。"""
        args = ["pr", "create", "--base", str(base), "--head", str(head), "--title", title]
        args += ["--body-file", "-"]
        if draft:
            args.append("--draft")
        out = self._ok(tree, *args, stdin=body)
        number = _pr_number(out)
        if number is None:
            raise ForgeError(Completed((self.gh, *args), 0, out, "PR の URL が出力に無い"))
        return number

    def find_pr(
        self, tree: str | os.PathLike[str], head: BranchName, state: str = "open"
    ) -> PullRequest | None:
        """`head` のブランチの PR。CreatePR を呼び直したとき、作り済みの PR を使うために引く。

        `--head` で絞っても、返った PR の head を確かめる。別のブランチの PR を自分のものとして使うと、
        その PR に積み・本文を書いてしまう。
        """
        out = self._ok(
            tree,
            "pr",
            "list",
            "--head",
            str(head),
            "--state",
            state,
            "--json",
            _PR_FIELDS,
        )
        found = [_pull_request(item) for item in _json_list(out)]
        return next((pr for pr in found if pr.head == str(head)), None)

    def view_pr(self, tree: str | os.PathLike[str], pr: PrNumber) -> PullRequest:
        out = self._ok(tree, "pr", "view", str(pr), "--json", _PR_FIELDS)
        loaded = json.loads(out)
        if not isinstance(loaded, dict):
            raise ValueError(f"gh pr view の出力が JSON の object でない: {out[:200]!r}")
        return _pull_request(loaded)

    def edit_pr(
        self,
        tree: str | os.PathLike[str],
        pr: PrNumber,
        *,
        title: str | None = None,
        body: str | None = None,
    ) -> None:
        args = ["pr", "edit", str(pr)]
        if title is not None:
            args += ["--title", title]
        if body is not None:
            args += ["--body-file", "-"]
        if len(args) == 3:
            return
        self._ok(tree, *args, stdin=body)

    def close_pr(self, tree: str | os.PathLike[str], pr: PrNumber) -> None:
        got = self._run(tree, "pr", "close", str(pr))
        # 閉じ済みなら通す（呼び直しで同じ結果にする）
        if not got.ok and self.view_pr(tree, pr).state != "CLOSED":
            raise ForgeError(got)

    def ready_pr(self, tree: str | os.PathLike[str], pr: PrNumber) -> None:
        got = self._run(tree, "pr", "ready", str(pr))
        if not got.ok and self.view_pr(tree, pr).draft:
            raise ForgeError(got)

    # --- スタック ---

    def stack_link(
        self, overview_tree: str | os.PathLike[str], base: BranchName, prs: Sequence[PrNumber]
    ) -> None:
        """`prs` を下から順に渡す。gh stack link は 2 つ以上を要る。"""
        if len(prs) < 2:
            raise ValueError(
                f"gh stack link には PR を 2 つ以上渡す: {[int(p.value) for p in prs]}"
            )
        self._ok(overview_tree, "stack", "link", "--base", str(base), *(str(p) for p in prs))

    def stack_number(self, overview_tree: str | os.PathLike[str], pr: PrNumber) -> int | None:
        """`pr` を含むスタックの番号。スタックに入っていなければ None。"""
        for stack in self.stacks(overview_tree, pr):
            return stack["number"]
        return None

    def stacks(
        self, overview_tree: str | os.PathLike[str], pr: PrNumber | None = None
    ) -> list[dict[str, Any]]:
        """REST API でスタックを一覧する。`pr` を渡すと、それを含むスタックだけに絞る。

        応答が文書の形（object の配列で、各要素が整数の `number` を持つ）でなければ `ValueError`。
        黙って読み飛ばすと、スタックが在るのに「無い」と答えてしまう。
        """
        path = "repos/{owner}/{repo}/stacks"
        if pr is not None:
            path += f"?pull_request={pr}"
        out = self._ok(
            overview_tree,
            "api",
            "--paginate",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {STACKS_API_VERSION}",
            path,
        )
        stacks = _json_list(out)
        for item in stacks:
            number = item.get("number") if isinstance(item, dict) else None
            if not isinstance(number, int) or isinstance(number, bool):
                raise ValueError(f"スタックの一覧の形が文書と違う: {json.dumps(item)[:200]}")
        return stacks

    def unstack(self, overview_tree: str | os.PathLike[str], stack: int) -> None:
        """スタックを GitHub の上で解く。閉じた PR はスタックに残り上の PR をマージできなくするので、
        閉じただけでは足りない。"""
        self._ok(overview_tree, "stack", "unstack", str(stack))


_PR_FIELDS = "number,headRefName,baseRefName,state,isDraft"


def _pr_number(text: str) -> PrNumber | None:
    for line in reversed(text.strip().splitlines()):
        found = _PR_URL.search(line.strip())
        if found:
            return PrNumber(int(found.group(1)))
    return None


def _pull_request(item: Any) -> PullRequest:
    return PullRequest(
        number=PrNumber(int(item["number"])),
        head=str(item["headRefName"]),
        base=str(item["baseRefName"]),
        state=str(item["state"]),
        draft=bool(item["isDraft"]),
    )


def _json_list(text: str) -> list[Any]:
    """`gh api --paginate` はページごとの配列を続けて出すので、つないで 1 つの配列にする。

    配列でないもの（エラーの object など）が混ざったら `ValueError`。
    """
    decoder = json.JSONDecoder()
    items: list[Any] = []
    index = 0
    text = text.strip()
    while index < len(text):
        value, end = decoder.raw_decode(text, index)
        if not isinstance(value, list):
            raise ValueError(f"JSON の配列でない: {text[index:end][:200]}")
        items.extend(value)
        index = end
        while index < len(text) and text[index].isspace():
            index += 1
    return items
