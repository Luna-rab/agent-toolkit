"""Codex のガードのフック（`autodev hook deny-writes|park-on-ask`）と、その記録。

Codex の PreToolUse は `tool_name` が `Bash` か `apply_patch`、`tool_input.command` に
コマンド行かパッチの本文を渡す。判定の規則は Claude と同じ（test_guard_hooks.py）。ここでは、
入口が呼ばれるたびに記録を書き足すこと、ガードが無いときは何もしないこと、記録が書けないときは
止めること、を確かめる。`autodev` は PATH のコマンドなので、サブプロセスの試験は偽の起動スクリプトを使う。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import REPO_ROOT

from autodev.adapters.claude import guard
from autodev.adapters.claude.guard import guard_context
from autodev.adapters.codex.hook import (
    DENY_WRITES,
    HOOK_RECORD_ENV,
    PARK_ON_ASK,
    HookRecord,
    count_records,
    read_records,
    run_codex_hook,
)
from autodev.domain.value_objects.guard import Guard
from autodev.domain.value_objects.write_scope import WriteScope

NONE = WriteScope.NONE
NON_TESTS = WriteScope.NON_TESTS
SYSTEM_PATH = "/usr/bin:/bin"


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
        (self.tree / "tests" / "test_app.py").write_text("def test(): ...\n", encoding="utf-8")
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

    def payload(self, tool: str, command: str, **extra: Any) -> str:
        body = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": {"command": command},
            "cwd": str(self.tree),
            **extra,
        }
        return json.dumps(body)


@pytest.fixture
def places(tmp_path: Path) -> Places:
    return Places(tmp_path)


def outside_write(places: Places) -> str:
    return f"echo x > {places.repo}/a.py"


def protected_write() -> str:
    return "echo x > tests/test_app.py"


# --- ガードが無いとき ---


@pytest.mark.parametrize("handler", [DENY_WRITES, PARK_ON_ASK])
def test_ガードが無ければ何も書かず通す(places: Places, handler: str):
    environ = {HOOK_RECORD_ENV: str(places.record)}
    reply = run_codex_hook(handler, places.payload("Bash", outside_write(places)), environ)
    assert (reply.exit_code, reply.stdout, reply.stderr) == (0, "", "")
    assert not places.record.exists()


def launcher(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "autodev"
    path.parent.mkdir()
    path.write_text(
        f'#!/bin/sh\nexec {sys.executable} -I -m autodev "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def run_cli(tmp_path: Path, args: list[str], stdin: str, env: dict[str, str]):
    bin_dir = launcher(tmp_path).parent
    full_env = {"PATH": f"{bin_dir}:{SYSTEM_PATH}", "HOME": str(tmp_path / "h")}
    full_env.update(env)
    return subprocess.run(
        ["sh", "-c", " ".join(["autodev", *args])],
        input=stdin,
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )


def test_コマンドのautodev_hookはガードが無ければ終了コード0で記録を作らない(
    places: Places, tmp_path: Path
):
    got = run_cli(
        tmp_path,
        ["hook", DENY_WRITES],
        places.payload("Bash", outside_write(places)),
        {HOOK_RECORD_ENV: str(places.record)},
    )
    assert got.returncode == 0, got.stderr
    assert not places.record.exists()


def test_コマンドのautodev_hookは拒んだら終了コード2で標準エラーに理由を書く(
    places: Places, tmp_path: Path
):
    got = run_cli(
        tmp_path, ["hook", DENY_WRITES], places.payload("Bash", protected_write()), places.env()
    )
    assert got.returncode == 2
    assert "テスト" in got.stderr
    assert [r.denied for r in read_records(places.record)] == [True]


def test_コマンドのautodev_hookは通したら終了コード0で記録に足す(places: Places, tmp_path: Path):
    got = run_cli(
        tmp_path, ["hook", DENY_WRITES], places.payload("Bash", "cat src/app.py"), places.env()
    )
    assert got.returncode == 0, got.stderr
    assert [(r.tool, r.denied) for r in read_records(places.record)] == [("Bash", False)]


# --- deny-writes と記録 ---


def test_通した呼び出しも拒んだ呼び出しも1行ずつ記録する(places: Places):
    env = places.env()
    passed = run_codex_hook(DENY_WRITES, places.payload("Bash", "cat src/app.py"), env)
    denied = run_codex_hook(DENY_WRITES, places.payload("Bash", protected_write()), env)
    assert passed.exit_code == 0
    assert denied.exit_code == 2
    assert denied.stderr
    records = read_records(places.record)
    assert [(r.hook, r.tool, r.denied) for r in records] == [
        (DENY_WRITES, "Bash", False),
        (DENY_WRITES, "Bash", True),
    ]
    assert len(places.record.read_text(encoding="utf-8").splitlines()) == 2


def test_apply_patchの宛先を判定して記録する(places: Places):
    outside = f"*** Begin Patch\n*** Add File: {places.repo}/a.py\n+x\n*** End Patch\n"
    inside = "*** Begin Patch\n*** Add File: src/new.py\n+x\n*** End Patch\n"
    tests = "*** Begin Patch\n*** Update File: tests/test_app.py\n@@\n+x\n*** End Patch\n"
    env = places.env()
    assert run_codex_hook(DENY_WRITES, places.payload("apply_patch", inside), env).exit_code == 0
    assert run_codex_hook(DENY_WRITES, places.payload("apply_patch", outside), env).exit_code == 0
    refused = run_codex_hook(DENY_WRITES, places.payload("apply_patch", tests), env)
    assert refused.exit_code == 2
    assert "テスト" in refused.stderr
    assert [(r.tool, r.denied) for r in read_records(places.record)] == [
        ("apply_patch", False),
        ("apply_patch", False),
        ("apply_patch", True),
    ]


def test_書き込みでないツールの呼び出しも記録して通す(places: Places):
    reply = run_codex_hook(DENY_WRITES, places.payload("Read", "x"), places.env())
    assert reply.exit_code == 0
    assert [(r.tool, r.denied) for r in read_records(places.record)] == [("Read", False)]


def test_記録は書き足す(places: Places):
    places.record.write_text(
        json.dumps({"hook": DENY_WRITES, "tool": "Bash", "denied": False}) + "\n",
        encoding="utf-8",
    )
    run_codex_hook(DENY_WRITES, places.payload("Bash", "ls"), places.env())
    assert count_records(places.record) == 2


# --- 記録の読み方 ---


def test_read_recordsはstart行目から後ろだけを返しcount_recordsは行数を返す(places: Places):
    env = places.env()
    for command in ("ls", "cat src/app.py", protected_write()):
        run_codex_hook(DENY_WRITES, places.payload("Bash", command), env)
    assert count_records(places.record) == 3
    assert [r.denied for r in read_records(places.record, start=2)] == [True]
    assert len(read_records(places.record)) == 3
    assert read_records(places.record, start=3) == []


def test_無い記録のファイルは空と0を返す(tmp_path: Path):
    missing = tmp_path / "none"
    assert read_records(missing) == []
    assert count_records(missing) == 0


def test_読めない行は飛ばして行数には数える(tmp_path: Path):
    path = tmp_path / "rec"
    good = {"hook": DENY_WRITES, "tool": "Bash", "denied": True}
    path.write_text("not json\n" + json.dumps(good) + "\n", encoding="utf-8")
    assert count_records(path) == 2
    assert read_records(path) == [HookRecord(DENY_WRITES, "Bash", True)]
    assert read_records(path, start=1) == read_records(path)
    assert read_records(path, start=2) == []


# --- 記録が書けない・入力が読めない ---


def test_記録のファイルの指定が無ければ判定せずに止める(places: Places):
    env = places.env()
    del env[HOOK_RECORD_ENV]
    reply = run_codex_hook(DENY_WRITES, places.payload("Bash", "ls"), env)
    assert reply.exit_code == 2
    assert HOOK_RECORD_ENV in reply.stderr


def test_記録のファイルの指定が空でも止める(places: Places):
    env = {**places.env(), HOOK_RECORD_ENV: ""}
    reply = run_codex_hook(PARK_ON_ASK, places.payload("Bash", "ls"), env)
    assert reply.exit_code == 2
    assert HOOK_RECORD_ENV in reply.stderr


def test_記録のファイルに書けなければ止める(places: Places):
    env = {**places.env(), HOOK_RECORD_ENV: str(places.record.parent)}
    reply = run_codex_hook(DENY_WRITES, places.payload("Bash", "ls"), env)
    assert reply.exit_code == 2
    assert HOOK_RECORD_ENV in reply.stderr


def test_コマンドで記録のファイルの指定が無ければ終了コード2(places: Places, tmp_path: Path):
    env = places.env()
    del env[HOOK_RECORD_ENV]
    got = run_cli(tmp_path, ["hook", DENY_WRITES], places.payload("Bash", "ls"), env)
    assert got.returncode == 2
    assert HOOK_RECORD_ENV in got.stderr


@pytest.mark.parametrize("handler", [DENY_WRITES, PARK_ON_ASK])
def test_標準入力がJSONでなければ止める(places: Places, handler: str):
    reply = run_codex_hook(handler, "not json", places.env())
    assert reply.exit_code == 2
    assert reply.stderr


def test_コマンドで標準入力がJSONでなければ終了コード2(places: Places, tmp_path: Path):
    got = run_cli(tmp_path, ["hook", DENY_WRITES], "not json", places.env())
    assert got.returncode == 2


# --- park-on-ask ---

ASK = "autodev ask --question 'Q1'"


def test_回答が無いaskは記録に控えて終了コード2で止める(places: Places):
    reply = run_codex_hook(
        PARK_ON_ASK,
        places.payload("Bash", ASK, tool_use_id="call_1"),
        places.env(NONE, can_ask=True),
    )
    assert reply.exit_code == 2
    assert reply.stderr
    (record,) = read_records(places.record)
    assert record.hook == PARK_ON_ASK
    assert record.tool == "Bash"
    assert record.question == "Q1"
    assert record.tool_use_id == "call_1"
    assert record.command == ASK


def test_回答のファイルが別の呼び出しのものならaskを止める(places: Places):
    answers = places.run_dir / "answers"
    answers.mkdir(parents=True)
    (answers / "call_9.json").write_text("{}", encoding="utf-8")
    reply = run_codex_hook(
        PARK_ON_ASK,
        places.payload("Bash", ASK, tool_use_id="call_1"),
        places.env(NONE, can_ask=True),
    )
    assert reply.exit_code == 2
    assert [r.question for r in read_records(places.record)] == ["Q1"]


def put_answer(places: Places, tool_use_id: str, answer: str) -> None:
    answers = places.run_dir / "answers"
    answers.mkdir(parents=True, exist_ok=True)
    (answers / f"{tool_use_id}.json").write_text(json.dumps({"answer": answer}), encoding="utf-8")


def ask_payload(places: Places, tool_use_id: str, question: str = "Q1") -> str:
    return places.payload("Bash", f"autodev ask --question '{question}'", tool_use_id=tool_use_id)


def test_回答が届いているaskは通さず回答を標準エラーで返して控えを足さない(places: Places):
    put_answer(places, "call_1", "A1")
    reply = run_codex_hook(
        PARK_ON_ASK, ask_payload(places, "call_1"), places.env(NONE, can_ask=True)
    )
    assert reply.exit_code == 2
    assert "A1" in reply.stderr
    assert "autodev ask を呼び直さない" in reply.stderr
    (record,) = read_records(places.record)
    assert (record.denied, record.question, record.answered_from) == (False, None, "call_1")


def test_tool_use_idが変わっても同じ質問なら前の回答を返す(places: Places):
    env = places.env(NONE, can_ask=True)
    first = run_codex_hook(PARK_ON_ASK, ask_payload(places, "call_1"), env)
    assert first.exit_code == 2
    put_answer(places, "call_1", "A1")
    second = run_codex_hook(PARK_ON_ASK, ask_payload(places, "call_2"), env)
    assert second.exit_code == 2
    assert "A1" in second.stderr
    records = read_records(places.record)
    assert [(r.question, r.answered_from) for r in records] == [("Q1", None), (None, "call_1")]


def test_前の質問に回答が無ければ別のidの同じ質問は控え直す(places: Places):
    env = places.env(NONE, can_ask=True)
    run_codex_hook(PARK_ON_ASK, ask_payload(places, "call_1"), env)
    reply = run_codex_hook(PARK_ON_ASK, ask_payload(places, "call_2"), env)
    assert reply.exit_code == 2
    records = read_records(places.record)
    assert [(r.question, r.tool_use_id, r.denied) for r in records] == [
        ("Q1", "call_1", True),
        ("Q1", "call_2", True),
    ]


def test_質問の本文が違えば前の回答を使わず控える(places: Places):
    env = places.env(NONE, can_ask=True)
    run_codex_hook(PARK_ON_ASK, ask_payload(places, "call_1", "Q1"), env)
    put_answer(places, "call_1", "A1")
    reply = run_codex_hook(PARK_ON_ASK, ask_payload(places, "call_2", "Q2"), env)
    assert reply.exit_code == 2
    assert "A1" not in reply.stderr
    last = read_records(places.record)[-1]
    assert (last.question, last.tool_use_id, last.denied) == ("Q2", "call_2", True)


def test_askできないステージのaskは理由つきで止めて質問を控えない(places: Places):
    reply = run_codex_hook(
        PARK_ON_ASK, places.payload("Bash", ASK, tool_use_id="call_1"), places.env(NONE)
    )
    assert reply.exit_code == 2
    assert "ask で聞けません" in reply.stderr
    (record,) = read_records(places.record)
    assert record.question is None
    assert record.denied is True


def test_askでないBashはpark_on_askが通す(places: Places):
    reply = run_codex_hook(
        PARK_ON_ASK,
        places.payload("Bash", "ls", tool_use_id="call_1"),
        places.env(NONE, can_ask=True),
    )
    assert reply.exit_code == 0
    assert [(r.denied, r.question) for r in read_records(places.record)] == [(False, None)]


def test_askの呼び出しはdeny_writesが書き込みとして止めない(places: Places):
    reply = run_codex_hook(
        DENY_WRITES,
        places.payload("Bash", "autodev ask --question 'a > b か'"),
        places.env(NONE, can_ask=True),
    )
    assert reply.exit_code == 0


def test_コマンドのpark_on_askは回答が無いaskを終了コード2で止める(places: Places, tmp_path: Path):
    got = run_cli(
        tmp_path,
        ["hook", PARK_ON_ASK],
        places.payload("Bash", ASK, tool_use_id="call_1"),
        places.env(NONE, can_ask=True),
    )
    assert got.returncode == 2
    assert [r.question for r in read_records(places.record)] == ["Q1"]


# --- config/codex/hooks.json の登録コマンドを sh -c で流す ---


GUARD_COMMANDS = [
    '[ -z "$AUTODEV_GUARD" ] || autodev hook deny-writes || exit 2',
    '[ -z "$AUTODEV_GUARD" ] || autodev hook park-on-ask || exit 2',
]


def registered_commands() -> list[str]:
    config = json.loads((REPO_ROOT / "config" / "codex" / "hooks.json").read_text(encoding="utf-8"))
    groups = config["hooks"].get("PreToolUse", [])
    return [hook["command"] for group in groups for hook in group["hooks"]]


def run_registered(command: str, stdin: str, env: dict[str, str], path: str):
    full_env = {"PATH": path, "HOME": env.get("HOME", "/nonexistent")}
    full_env.update(env)
    return subprocess.run(
        ["sh", "-c", command],
        input=stdin,
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )


def test_登録するコマンドは2つのフックを呼ぶ():
    commands = registered_commands()
    for command in GUARD_COMMANDS:
        assert commands.count(command) == 1


@pytest.mark.parametrize("command", GUARD_COMMANDS)
def test_登録したコマンドはガードの下でautodevが無ければ終了コード2(places: Places, command: str):
    assert shutil.which("autodev", path=SYSTEM_PATH) is None
    got = run_registered(command, places.payload("Bash", "ls"), places.env(), SYSTEM_PATH)
    assert got.returncode == 2


@pytest.mark.parametrize("command", GUARD_COMMANDS)
def test_登録したコマンドはガードが無ければautodevが無くても終了コード0(
    places: Places, command: str
):
    assert shutil.which("autodev", path=SYSTEM_PATH) is None
    got = run_registered(command, places.payload("Bash", "ls"), {}, SYSTEM_PATH)
    assert got.returncode == 0, got.stderr


@pytest.mark.parametrize("command", GUARD_COMMANDS)
def test_登録したコマンドはガードの下で入力が通れば0_拒まれれば2(
    places: Places, tmp_path: Path, command: str
):
    path = f"{launcher(tmp_path).parent}:{SYSTEM_PATH}"
    passed = run_registered(command, places.payload("Bash", "ls"), places.env(NONE), path)
    refused = run_registered(
        command, places.payload("Bash", protected_write()), places.env(NONE), path
    )
    assert passed.returncode == 0, passed.stderr
    assert refused.returncode == (2 if DENY_WRITES in command else 0)
    assert os.path.exists(places.record)


# --- Bash から呼ぶ apply_patch ---


def add_patch(path: str) -> str:
    return f"*** Begin Patch\n*** Add File: {path}\n+x\n*** End Patch"


def bash_decision(places: Places, command: str) -> int:
    reply = run_codex_hook(DENY_WRITES, places.payload("Bash", command), places.env())
    return reply.exit_code


@pytest.mark.parametrize(
    "template",
    [
        "apply_patch '{inside}'",
        "apply_patch <<'EOF'\n{inside}\nEOF",
        "grep -rn apply_patch src",
        "git commit -m 'apply_patch のヘッダを直す'",
        "git log --grep applypatch",
        "grep -rn -e --codex-run-as-apply-patch src",
        'grep -rn "--codex-run-as-apply-patch" src',
        "codex -c x=1 --codex-run-as-apply-patch '{inside}'",
    ],
)
def test_Bashのapply_patchは宛先がworktreeの中なら通し_語を含むだけのコマンドも通す(
    places: Places, template: str
):
    command = template.format(inside=add_patch("src/new.py"))
    assert bash_decision(places, command) == 0


@pytest.mark.parametrize(
    "template",
    [
        "apply_patch <<'EOF'\n{outside}\nEOF",
        "apply_patch '{outside}'",
        "apply_pat''ch '{outside}'",
        "\"apply_patch\" '{outside}'",
        "sh -c \"apply_patch '{outside}'\"",
        "codex --codex-run-as-apply-patch '{outside}'",
        "find . -exec apply_patch '{outside}' ';'",
    ],
)
def test_Bashのapply_patchは宛先がworktreeの外でも止めない(places: Places, template: str):
    command = template.format(outside=add_patch(f"{places.repo}/a.py"))
    reply = run_codex_hook(DENY_WRITES, places.payload("Bash", command), places.env())
    assert reply.exit_code == 0
    assert [r.denied for r in read_records(places.record)] == [False]


@pytest.mark.parametrize(
    "template",
    [
        "cd {home} && apply_patch <<'EOF'\n{relative}\nEOF",
        "env -C {home} apply_patch '{relative}'",
        "eval 'cd {home}' && apply_patch '{relative}'",
        "(cd {home}; apply_patch '{relative}')",
    ],
)
def test_Bashのapply_patchは作業ディレクトリをworktreeの外へ変えた後の相対パスでも止めない(
    places: Places, template: str
):
    command = template.format(home=places.home, relative=add_patch("a.txt"))
    assert bash_decision(places, command) == 0


def test_Bashのapply_patchは作業ディレクトリを変えた後でもworktreeの中のテストの編集は止める(
    places: Places,
):
    patch = "*** Begin Patch\n*** Update File: test_app.py\n@@\n+x\n*** End Patch"
    command = f"cd {places.tree}/tests && apply_patch '{patch}'"
    assert bash_decision(places, command) == 2


@pytest.mark.parametrize(
    "template",
    [
        "cat <<'EOF' > /dev/null\nx\nEOF\nprintf x | apply_patch",
        "apply_patch '{inside}' && apply_patch \"$(cat /tmp/e)\"",
        'apply_patch "{outside_var}"',
        'T=tests/test_app.py; apply_patch "{outside_var}"',
        "sh -c 'apply_patch \"$0\"' '{inside}'",
        'bash -c "cd {home}; apply_patch" <<EOF\n{inside}\nEOF',
    ],
)
def test_Bashのapply_patchは本文を決められない形を止める(places: Places, template: str):
    command = template.format(
        inside=add_patch("src/new.py"),
        outside_var=add_patch("$HOME/evil.py"),
        home=places.home,
    )
    assert bash_decision(places, command) == 2


def test_Bashのapply_patchでもテストの編集は止める(places: Places):
    patch = "*** Begin Patch\n*** Update File: tests/test_app.py\n@@\n+x\n*** End Patch"
    reply = run_codex_hook(
        DENY_WRITES, places.payload("Bash", f"apply_patch '{patch}'"), places.env()
    )
    assert reply.exit_code == 2
    assert "テスト" in reply.stderr
