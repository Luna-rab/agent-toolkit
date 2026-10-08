"""Forge: 本物の gh は叩かない。呼ばれた引数と標準入力を記録する偽の gh を置く。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from autodev.adapters.github.forge import STACKS_API_VERSION, Forge, ForgeError
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.pr_number import PrNumber

FAKE_GH = """\
import json, os, sys
args = sys.argv[1:]
stdin = sys.stdin.read()
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"args": args, "stdin": stdin, "cwd": os.getcwd()}) + "\\n")
with open(os.environ["FAKE_GH_REPLIES"], encoding="utf-8") as fh:
    replies = json.load(fh)
for reply in replies:
    if args[: len(reply["prefix"])] == reply["prefix"]:
        sys.stdout.write(reply.get("out", ""))
        sys.stderr.write(reply.get("err", ""))
        sys.exit(reply.get("code", 0))
sys.exit(0)
"""


class FakeGh:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        script = tmp_path / "fake_gh.py"
        script.write_text(FAKE_GH, encoding="utf-8")
        self.path = tmp_path / "gh"
        self.path.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        self.path.chmod(0o755)
        self.log = tmp_path / "gh.log"
        self.replies_file = tmp_path / "replies.json"
        self.replies: list[dict[str, Any]] = []
        self._save()
        monkeypatch.setenv("FAKE_GH_LOG", str(self.log))
        monkeypatch.setenv("FAKE_GH_REPLIES", str(self.replies_file))

    def reply(self, prefix: list[str], out: str = "", err: str = "", code: int = 0) -> None:
        self.replies.append({"prefix": prefix, "out": out, "err": err, "code": code})
        self._save()

    def _save(self) -> None:
        self.replies_file.write_text(json.dumps(self.replies), encoding="utf-8")

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    return FakeGh(tmp_path, monkeypatch)


def forge(gh: FakeGh) -> Forge:
    return Forge(gh=str(gh.path))


def test_PRを作ると出力の最後の行のURLから番号を取り本文は標準入力で渡す(
    gh: FakeGh, tmp_path: Path
):
    gh.reply(["pr", "create"], out="Creating pull request\nhttps://github.com/o/r/pull/42\n")
    number = forge(gh).create_pr(
        tmp_path,
        base=BranchName("stack/r--task-0"),
        head=BranchName("stack/r--task-1"),
        title="[autodev #41 task1] 件名",
        body="本文の $tasks と ${x} はそのまま",
        draft=True,
    )
    assert number == PrNumber(42)
    call = gh.calls()[0]
    assert call["args"] == [
        "pr",
        "create",
        "--base",
        "stack/r--task-0",
        "--head",
        "stack/r--task-1",
        "--title",
        "[autodev #41 task1] 件名",
        "--body-file",
        "-",
        "--draft",
    ]
    assert call["stdin"] == "本文の $tasks と ${x} はそのまま"


def test_PRを作れなければForgeErrorにする(gh: FakeGh, tmp_path: Path):
    gh.reply(["pr", "create"], err="No commits between main and x\n", code=1)
    with pytest.raises(ForgeError, match="No commits between"):
        forge(gh).create_pr(
            tmp_path, base=BranchName("main"), head=BranchName("x"), title="t", body="b"
        )


def test_同じブランチの開いたPRを引く(gh: FakeGh, tmp_path: Path):
    item = {
        "number": 7,
        "headRefName": "stack/r--task-1",
        "baseRefName": "stack/r--task-0",
        "state": "OPEN",
        "isDraft": False,
    }
    gh.reply(["pr", "list"], out=json.dumps([item]))
    found = forge(gh).find_pr(tmp_path, BranchName("stack/r--task-1"))
    assert found is not None
    assert found.number == PrNumber(7)
    assert found.base == "stack/r--task-0"
    assert gh.calls()[0]["args"][:6] == [
        "pr",
        "list",
        "--head",
        "stack/r--task-1",
        "--state",
        "open",
    ]


def test_headの違うPRしか返らなければNoneを返す(gh: FakeGh, tmp_path: Path):
    item = {
        "number": 8,
        "headRefName": "stack/r--task-10",
        "baseRefName": "stack/r--task-0",
        "state": "OPEN",
        "isDraft": False,
    }
    gh.reply(["pr", "list"], out=json.dumps([item]))
    assert forge(gh).find_pr(tmp_path, BranchName("stack/r--task-1")) is None


def test_PRが無ければNoneを返す(gh: FakeGh, tmp_path: Path):
    gh.reply(["pr", "list"], out="[]")
    assert forge(gh).find_pr(tmp_path, BranchName("x")) is None


def test_本文とタイトルを書き換える(gh: FakeGh, tmp_path: Path):
    forge(gh).edit_pr(tmp_path, PrNumber(3), title="新しい題", body="新しい本文")
    forge(gh).edit_pr(tmp_path, PrNumber(3))
    calls = gh.calls()
    assert len(calls) == 1
    assert calls[0]["args"] == ["pr", "edit", "3", "--title", "新しい題", "--body-file", "-"]
    assert calls[0]["stdin"] == "新しい本文"


def test_閉じ済みのPRを閉じ直しても通す(gh: FakeGh, tmp_path: Path):
    gh.reply(["pr", "close"], err="already closed", code=1)
    view = {
        "number": 3,
        "headRefName": "h",
        "baseRefName": "b",
        "state": "CLOSED",
        "isDraft": False,
    }
    gh.reply(["pr", "view"], out=json.dumps(view))
    forge(gh).close_pr(tmp_path, PrNumber(3))


def test_draftから外せなければForgeErrorにする(gh: FakeGh, tmp_path: Path):
    gh.reply(["pr", "ready"], err="boom", code=1)
    view = {"number": 3, "headRefName": "h", "baseRefName": "b", "state": "OPEN", "isDraft": True}
    gh.reply(["pr", "view"], out=json.dumps(view))
    with pytest.raises(ForgeError):
        forge(gh).ready_pr(tmp_path, PrNumber(3))


def test_stack_linkはbaseを必ず渡しPR番号を下から順に渡す(gh: FakeGh, tmp_path: Path):
    """--base を省くと一番下の base が既定ブランチに書き換わる。"""
    forge(gh).stack_link(
        tmp_path, BranchName("develop"), [PrNumber(10), PrNumber(11), PrNumber(12)]
    )
    call = gh.calls()[0]
    assert call["args"] == ["stack", "link", "--base", "develop", "10", "11", "12"]
    assert call["cwd"] == str(tmp_path)
    assert "--open" not in call["args"]


def test_stack_linkにPRを1つだけ渡すと拒む(gh: FakeGh, tmp_path: Path):
    """gh stack link は 2 つ以上を要る。"""
    with pytest.raises(ValueError, match="2 つ以上"):
        forge(gh).stack_link(tmp_path, BranchName("main"), [PrNumber(10)])
    assert gh.calls() == []


def test_スタックの番号をREST_APIで引く(gh: FakeGh, tmp_path: Path):
    stacks = [{"id": 1, "number": 5, "pull_requests": [{"number": 10}]}]
    gh.reply(["api"], out=json.dumps(stacks))
    assert forge(gh).stack_number(tmp_path, PrNumber(10)) == 5
    args = gh.calls()[0]["args"]
    assert args[-1] == "repos/{owner}/{repo}/stacks?pull_request=10"
    assert f"X-GitHub-Api-Version: {STACKS_API_VERSION}" in args


def test_ページに分かれた一覧をつなぐ(gh: FakeGh, tmp_path: Path):
    gh.reply(["api"], out='[{"number": 1}]\n[{"number": 2}]\n')
    assert [s["number"] for s in forge(gh).stacks(tmp_path)] == [1, 2]


@pytest.mark.parametrize(
    "out", ['{"message": "Not Found"}', '[{"id": 1}]', '[{"number": "5"}]', "[1]"]
)
def test_スタックの一覧の形が文書と違えば例外にする(gh: FakeGh, tmp_path: Path, out: str):
    """黙って読み飛ばすと、スタックが在るのに「無い」と答えてしまう。"""
    gh.reply(["api"], out=out)
    with pytest.raises(ValueError):
        forge(gh).stack_number(tmp_path, PrNumber(10))


def test_ghは言語を固定して呼ぶ(gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    record = tmp_path / "lc.txt"
    script = gh.path.with_name("fake_gh.py")
    script.write_text(
        f'import os\nopen({str(record)!r}, "w").write(os.environ.get("LC_ALL", ""))\n',
        encoding="utf-8",
    )
    forge(gh).unstack(tmp_path, 5)
    assert record.read_text(encoding="utf-8") == "C"


def test_スタックに入っていなければNoneを返す(gh: FakeGh, tmp_path: Path):
    gh.reply(["api"], out="[]")
    assert forge(gh).stack_number(tmp_path, PrNumber(10)) is None


def test_スタックを番号で解く(gh: FakeGh, tmp_path: Path):
    forge(gh).unstack(tmp_path, 5)
    assert gh.calls()[0]["args"] == ["stack", "unstack", "5"]


def test_認証とgh_stackの拡張を確かめる(gh: FakeGh, tmp_path: Path):
    gh.reply(["extension", "list"], out="gh stack\tgithub/gh-stack\tv0.1.0\n")
    assert forge(gh).missing(tmp_path) == []


def test_gh_stackの拡張が無ければ足りないものに挙げる(gh: FakeGh, tmp_path: Path):
    gh.reply(["extension", "list"], out="")
    assert len(forge(gh).missing(tmp_path)) == 1


def test_認証されていなければ足りないものに挙げる(gh: FakeGh, tmp_path: Path):
    gh.reply(["auth", "status"], code=1)
    assert "認証" in forge(gh).missing(tmp_path)[0]
