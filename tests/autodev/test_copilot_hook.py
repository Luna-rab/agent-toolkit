"""Copilot のガードのフック（`python -I -m autodev.adapters.copilot.hook deny-writes|park-on-ask`）。

判定の規則は Claude と同じ（test_guard_hooks.py）。ここでは、Copilot の preToolUse の入力
（`sessionId`・`cwd`・`toolName`・`toolArgs`）を既存の判定へ写せること、ask をツール呼び出し ID
なしで質問の本文から控えて回答と結べること、記録の形が Codex と同じことを確かめる。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from autodev.adapters.claude import guard
from autodev.adapters.claude.guard import guard_context
from autodev.adapters.codex.hook import HOOK_RECORD_ENV, read_records
from autodev.adapters.copilot.hook import (
    DENY_WRITES,
    PARK_ON_ASK,
    ask_id,
    run_copilot_hook,
)
from autodev.domain.value_objects.guard import Guard
from autodev.domain.value_objects.write_scope import WriteScope

NONE = WriteScope.NONE
NON_TESTS = WriteScope.NON_TESTS
SESSION = "s1"


class Places:
    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.repo = self.home / "src" / "project"
        self.run_dir = self.home / ".local" / "state" / "autodev" / "r"
        self.tree = self.run_dir / "trees" / "task1"
        self.tmp = root / "tmpdir"
        self.record = root / "log" / "stage.jsonl.hooks"
        for path in (self.tree / "src", self.tree / "tests", self.tmp, self.repo):
            path.mkdir(parents=True)
        (self.tree / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        self.record.parent.mkdir()

    def env(self, scope: WriteScope = NON_TESTS, *, can_ask: bool = False) -> dict[str, str]:
        context = guard_context(
            tree=self.tree,
            run_dir=self.run_dir,
            target_repo=self.repo,
            home=self.home,
            temp_dirs=[self.tmp, "/tmp"],
            listed=(),
        )
        return {
            **guard.stage_env(Guard(scope, can_ask=can_ask), context),
            HOOK_RECORD_ENV: str(self.record),
        }

    def payload(self, tool: str, args: object, **extra: Any) -> str:
        body = {"sessionId": SESSION, "cwd": str(self.tree), "toolName": tool, "toolArgs": args}
        body.update(extra)
        return json.dumps(body)

    def put_answer(self, ask: str, answer: str) -> None:
        answers = self.run_dir / "answers"
        answers.mkdir(parents=True, exist_ok=True)
        (answers / f"{ask}.json").write_text(json.dumps({"answer": answer}), encoding="utf-8")


@pytest.fixture
def places(tmp_path: Path) -> Places:
    return Places(tmp_path)


RM_ROOT = {"command": "rm -rf /"}


# --- ガードが無い・記録が無い ---


@pytest.mark.parametrize("handler", [DENY_WRITES, PARK_ON_ASK])
def test_ガードが無ければ何も書かず通す(places: Places, handler: str):
    environ = {HOOK_RECORD_ENV: str(places.record)}
    reply = run_copilot_hook(handler, places.payload("bash", RM_ROOT), environ)
    assert (reply.exit_code, reply.stdout, reply.stderr) == (0, "", "")
    assert not places.record.exists()


@pytest.mark.parametrize("handler", [DENY_WRITES, PARK_ON_ASK])
def test_ガードが在り記録の指定が無ければ止める(places: Places, handler: str):
    env = places.env()
    del env[HOOK_RECORD_ENV]
    reply = run_copilot_hook(handler, places.payload("view", {"path": "x"}), env)
    assert reply.exit_code == 2
    assert HOOK_RECORD_ENV in reply.stderr


# --- deny-writes ---


def test_worktreeの外へのcreateは止めず記録する(places: Places):
    args = {"path": "/etc/x", "file_text": "a"}
    reply = run_copilot_hook(DENY_WRITES, places.payload("create", args), places.env())
    assert reply.exit_code == 0
    (record,) = read_records(places.record)
    assert (record.hook, record.tool, record.denied) == (DENY_WRITES, "create", False)


def test_書いてよいパスへのeditは通して記録する(places: Places):
    args = {"path": str(places.tree / "src" / "app.py"), "old_str": "a", "new_str": "b"}
    reply = run_copilot_hook(DENY_WRITES, places.payload("edit", args), places.env())
    assert reply.exit_code == 0
    (record,) = read_records(places.record)
    assert (record.hook, record.tool, record.denied) == (DENY_WRITES, "edit", False)


def test_テストを書けないステージはテストへのcreateを止める(places: Places):
    args = {"path": str(places.tree / "tests" / "test_a.py"), "file_text": "a"}
    reply = run_copilot_hook(DENY_WRITES, places.payload("create", args), places.env())
    assert reply.exit_code == 2


def test_toolArgsがJSONの文字列でもobjectと同じに判定する(places: Places):
    env = places.env()
    write = json.dumps({"command": "echo x > tests/test_a.py"})
    refused = run_copilot_hook(DENY_WRITES, places.payload("bash", write), env)
    assert refused.exit_code == 2
    listed = run_copilot_hook(
        DENY_WRITES, places.payload("bash", json.dumps({"command": "ls"})), env
    )
    assert listed.exit_code == 0
    assert [r.denied for r in read_records(places.record)] == [True, False]


def test_bashのgit_pushは止めない(places: Places):
    args = {"command": "git push origin HEAD"}
    reply = run_copilot_hook(DENY_WRITES, places.payload("bash", args), places.env())
    assert reply.exit_code == 0
    assert [r.denied for r in read_records(places.record)] == [False]


def test_apply_patchはパッチの文字列の宛先を判定する(places: Places):
    env = places.env()
    outside = "*** Begin Patch\n*** Add File: /etc/x\n+a\n*** End Patch"
    inside = "*** Begin Patch\n*** Add File: src/new.py\n+a\n*** End Patch"
    protected = "*** Begin Patch\n*** Update File: tests/test_a.py\n@@\n+a\n*** End Patch"
    assert run_copilot_hook(DENY_WRITES, places.payload("apply_patch", outside), env).exit_code == 0
    assert run_copilot_hook(DENY_WRITES, places.payload("apply_patch", inside), env).exit_code == 0
    assert (
        run_copilot_hook(DENY_WRITES, places.payload("apply_patch", protected), env).exit_code == 2
    )
    assert [(r.tool, r.denied) for r in read_records(places.record)] == [
        ("apply_patch", False),
        ("apply_patch", False),
        ("apply_patch", True),
    ]


@pytest.mark.parametrize("args", [{"input": "rm -rf /"}, {"input": "ls"}, {}])
def test_write_bashは中身を問わず止める(places: Places, args: dict[str, str]):
    reply = run_copilot_hook(DENY_WRITES, places.payload("write_bash", args), places.env())
    assert reply.exit_code == 2
    assert [(r.tool, r.denied) for r in read_records(places.record)] == [("write_bash", True)]


@pytest.mark.parametrize(
    ("tool", "args"),
    [("view", {"path": "/etc/passwd"}), ("grep", {"pattern": "x"}), ("glob", {"pattern": "*"})],
)
def test_写し先の無いツールは通す(places: Places, tool: str, args: dict[str, str]):
    reply = run_copilot_hook(DENY_WRITES, places.payload(tool, args), places.env())
    assert reply.exit_code == 0
    assert [(r.tool, r.denied) for r in read_records(places.record)] == [(tool, False)]


@pytest.mark.parametrize("handler", [DENY_WRITES, PARK_ON_ASK])
def test_標準入力がJSONでなければ止める(places: Places, handler: str):
    reply = run_copilot_hook(handler, "not json", places.env())
    assert reply.exit_code == 2
    assert reply.stderr


# --- ask_id ---


def test_ask_idは同じ引数なら同じ値で答えのファイル名に使える形():
    first = ask_id("s1", "TTL は?")
    assert ask_id("s1", "TTL は?") == first
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", first)
    assert ask_id("s2", "TTL は?") != first
    assert ask_id("s1", "TTL は 60 秒か?") != first


# --- park-on-ask ---

QUESTION = "TTL は 60 秒か 300 秒か"
ASK = {"command": f"autodev ask --question '{QUESTION}'"}


def test_回答が無いaskは質問を控えて止める(places: Places):
    reply = run_copilot_hook(
        PARK_ON_ASK, places.payload("bash", ASK), places.env(NONE, can_ask=True)
    )
    assert reply.exit_code == 2
    assert reply.stderr
    (record,) = read_records(places.record)
    assert record.hook == PARK_ON_ASK
    assert record.tool_use_id == ask_id(SESSION, QUESTION)
    assert record.question == QUESTION
    assert record.command == ASK["command"]


def test_回答が届いているaskは回答を理由に入れて止め控えを足さない(places: Places):
    env = places.env(NONE, can_ask=True)
    identifier = ask_id(SESSION, QUESTION)
    places.put_answer(identifier, "60 秒")
    reply = run_copilot_hook(PARK_ON_ASK, places.payload("bash", ASK), env)
    assert reply.exit_code == 2
    assert "60 秒" in reply.stderr
    (record,) = read_records(places.record)
    assert record.denied is False
    assert record.answered_from == identifier
    assert record.question is None


def test_別の質問の回答は使わず質問を控える(places: Places):
    places.put_answer(ask_id(SESSION, "別の質問"), "A")
    reply = run_copilot_hook(
        PARK_ON_ASK, places.payload("bash", ASK), places.env(NONE, can_ask=True)
    )
    assert reply.exit_code == 2
    (record,) = read_records(places.record)
    assert record.question == QUESTION


def test_askできないステージのaskは止めて質問を控えない(places: Places):
    reply = run_copilot_hook(PARK_ON_ASK, places.payload("bash", ASK), places.env(NONE))
    assert reply.exit_code == 2
    (record,) = read_records(places.record)
    assert record.denied is True
    assert record.question is None


def test_sessionIdの無いaskは止める(places: Places):
    body = {"cwd": str(places.tree), "toolName": "bash", "toolArgs": ASK}
    reply = run_copilot_hook(PARK_ON_ASK, json.dumps(body), places.env(NONE, can_ask=True))
    assert reply.exit_code == 2
    assert all(r.question is None for r in read_records(places.record))


def test_askでないbashはpark_on_askが通す(places: Places):
    reply = run_copilot_hook(
        PARK_ON_ASK, places.payload("bash", {"command": "ls"}), places.env(NONE, can_ask=True)
    )
    assert reply.exit_code == 0
    assert [(r.denied, r.question) for r in read_records(places.record)] == [(False, None)]


def test_askの呼び出しはdeny_writesが書き込みとして止めない(places: Places):
    args = {"command": "autodev ask --question 'a > b か'"}
    reply = run_copilot_hook(
        DENY_WRITES, places.payload("bash", args), places.env(NONE, can_ask=True)
    )
    assert reply.exit_code == 0


# --- コマンドとして起こす ---


def run_module(args: list[str], stdin: str, env: dict[str, str]):
    return subprocess.run(
        [sys.executable, "-I", "-m", "autodev.adapters.copilot.hook", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_ハンドラ名なしで起こすと終了コード2():
    got = run_module([], "{}", {})
    assert got.returncode == 2
    assert got.stderr


def test_コマンドで拒むと終了コード2で理由を標準エラーに書く(places: Places):
    args = {"path": str(places.tree / "tests" / "test_a.py"), "file_text": "a"}
    got = run_module([DENY_WRITES], places.payload("create", args), places.env())
    assert got.returncode == 2
    assert got.stderr
    assert [r.denied for r in read_records(places.record)] == [True]


def test_コマンドはガードが無ければ終了コード0(places: Places):
    got = run_module([DENY_WRITES], places.payload("bash", RM_ROOT), {})
    assert got.returncode == 0, got.stderr
