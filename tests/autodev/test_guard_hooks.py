"""ガードのフック（deny-writes.py・park-on-ask.py）と、その翻訳（adapters/claude/guard.py・adapters/process/shell.py）。

止めるか通すかはドメインの Guard が決める（test_domain_guard.py）。ここでは、コマンド行とパスが
正しい宛先・場所・操作に翻訳され、その結果として通す場合と止める場合を
確かめる。フックは claude の子プロセスとして別に起動され、落ちてもガードが黙って消えるだけ
なので、入口のスクリプトも実際に起動する。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from autodev.adapters.claude import guard
from autodev.adapters.claude.guard import GuardContext, guard_context
from autodev.app.stages.files import write_answer
from autodev.domain.guard import RefusalReason, WriteZone
from autodev.domain.value_objects.glob_pattern import DEFAULT_TEST_GLOBS
from autodev.domain.value_objects.guard import Guard
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.write_scope import WriteScope
from autodev.infra.paths import RunPaths

HOOK_MODULE = "autodev.adapters.claude.hook"
NONE = WriteScope.NONE
NON_TESTS = WriteScope.NON_TESTS
TESTS_ONLY = WriteScope.TESTS_ONLY
STUBS = WriteScope.TESTS_AND_STUBS


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class Places:
    """検査の場所。tmp_path は /tmp の下にあるが、ホームとランディレクトリは /tmp より深いので、
    一番深い場所を選ぶ翻訳で HOME・RUN_DIR に振り分けられる。"""

    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.repo = self.home / "src" / "project"
        self.run_dir = self.home / ".local" / "state" / "autodev" / "r"
        self.tree = self.run_dir / "trees" / "task1"
        self.tmp = root / "tmpdir"
        for path in (self.tree / "src", self.tree / "tests", self.tmp, self.repo):
            path.mkdir(parents=True)
        (self.tree / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.tree / "tests" / "test_app.py").write_text("def test(): ...\n", encoding="utf-8")
        (self.tree / ".gitignore").write_text("build/\n", encoding="utf-8")
        (self.tree / "build").mkdir()
        (self.tree / "build" / "test_generated.py").write_text("", encoding="utf-8")
        (self.tree / "scratch").mkdir()
        (self.tree / "scratch" / "test_untracked.py").write_text("", encoding="utf-8")
        git(self.tree, "init", "-q")
        git(self.tree, "add", "src", "tests", ".gitignore")


@pytest.fixture
def places(tmp_path: Path) -> Places:
    return Places(tmp_path)


def context(places: Places, **fields: Any) -> GuardContext:
    made = guard_context(
        tree=places.tree,
        run_dir=places.run_dir,
        target_repo=places.repo,
        home=places.home,
        temp_dirs=[places.tmp, "/tmp"],
        listed=fields.pop("listed", ()),
    )
    return replace(made, **fields) if fields else made


def check(places: Places, scope: WriteScope, tool: str, tool_input: dict[str, Any], **fields):
    return guard.check_tool(
        Guard(scope), context(places, **fields), tool, tool_input, str(places.tree)
    )


def bash(places: Places, scope: WriteScope, command: str, **fields: Any):
    return check(places, scope, "Bash", {"command": command}, **fields)


def reason(places: Places, scope: WriteScope, command: str) -> RefusalReason | None:
    refusal = bash(places, scope, command)
    return refusal.reason if refusal else None


# --- 場所への振り分け（一番深い場所を選ぶ） ---


def test_一番深い場所に振り分ける(places: Places):
    ctx = context(places)
    cases = {
        places.tree / "a.py": WriteZone.TREE,
        places.run_dir / "events.db": WriteZone.RUN_DIR,
        places.run_dir / "trees" / "task2" / "a.py": WriteZone.RUN_DIR,
        places.home / ".bashrc": WriteZone.HOME,
        places.repo / "a.py": WriteZone.TARGET_REPO,
        places.tmp / "x": WriteZone.TEMP,
        Path("/opt/elsewhere"): WriteZone.ELSEWHERE,
    }
    for path, zone in cases.items():
        assert guard.zone_of(ctx, os.path.realpath(path))[0] is zone, path


def test_一時ディレクトリがホームの下でも一時ディレクトリに振り分ける(places: Places):
    inner = places.home / "tmp"
    ctx = replace(context(places), temp_dirs=(str(inner),))
    assert guard.zone_of(ctx, str(inner / "x"))[0] is WriteZone.TEMP


# --- ツールのパス（そのまま使う） ---


@pytest.mark.parametrize(
    ("scope", "path", "allowed"),
    [
        (NONE, "src/app.py", False),
        (NON_TESTS, "src/app.py", True),
        (NON_TESTS, "tests/test_app.py", False),
        (STUBS, "tests/test_app.py", True),
        (TESTS_ONLY, "src/app.py", False),
        (TESTS_ONLY, "tests/test_app.py", True),
        # golden と snapshot もテストのパスに入れておけば守られる
        (TESTS_ONLY, "tests/golden/draw.json", True),
        (NON_TESTS, "tests/golden/draw.json", False),
    ],
)
def test_worktreeの中はテストのパスかで翻訳する(places: Places, scope, path, allowed):
    refusal = check(places, scope, "Write", {"file_path": str(places.tree / path)})
    assert (refusal is None) is allowed


def test_ツールのパスはglobや変数とみなさない(places: Places):
    """`[`・`*`・`$` は、ツールのパスではただの文字（そういう名前のファイル）である。"""
    assert check(places, NON_TESTS, "Write", {"file_path": "src/[id].tsx"}) is None
    assert check(places, NON_TESTS, "Write", {"file_path": "src/$x.py"}) is None
    assert check(places, NON_TESTS, "Write", {"file_path": "src/*.py"}) is None


def test_衝突したファイルだけを書ける(places: Places):
    listed = ("src/app.py",)
    assert (
        check(places, WriteScope.LISTED, "Edit", {"file_path": "src/app.py"}, listed=listed) is None
    )
    refusal = check(places, WriteScope.LISTED, "Edit", {"file_path": "src/b.py"}, listed=listed)
    assert refusal is not None
    assert "src/app.py" in guard.describe(refusal, context(places, listed=listed))


def test_MultiEditはeditsの宛先もNotebookEditはnotebook_pathも見る(places: Places):
    edits = {"edits": [{"file_path": str(places.tree / "tests" / "test_app.py")}]}
    assert check(places, NON_TESTS, "MultiEdit", edits) is not None
    notebook = {"notebook_path": str(places.tree / "tests" / "test_n.ipynb")}
    assert check(places, NON_TESTS, "NotebookEdit", notebook) is not None


def test_ほかのツールは見ない(places: Places):
    assert check(places, NONE, "Read", {"file_path": str(places.tree / "src" / "app.py")}) is None


# --- worktree の外 ---


def test_ランディレクトリとホームと対象リポジトリにも書ける(places, monkeypatch):
    monkeypatch.setenv("HOME", str(places.home))
    targets = (
        places.run_dir / "events.db",
        places.run_dir / "answers" / "x.json",
        places.repo / "a.py",
        places.home / ".bashrc",
    )
    for path in targets:
        assert check(places, STUBS, "Write", {"file_path": str(path)}) is None
    assert bash(places, STUBS, "echo x > ~/.config/foo") is None
    assert bash(places, STUBS, "echo x >> ~/.bashrc") is None
    assert bash(places, STUBS, f"cp src/app.py {places.repo}/x.py") is None
    assert bash(places, STUBS, f"cd {places.repo} && touch a.py") is None


def test_一時ディレクトリには書ける(places: Places, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TMPDIR", str(places.tmp))
    assert check(places, NONE, "Write", {"file_path": str(places.tmp / "x.py")}) is None
    assert bash(places, NONE, "echo x > $TMPDIR/a.txt") is None
    assert bash(places, NONE, "cp src/app.py /tmp/app.py") is None


# --- 場所が決まらないもの（UNKNOWN） ---


@pytest.mark.parametrize("scope", [NONE, NON_TESTS, STUBS])
@pytest.mark.parametrize(
    "command",
    [
        "echo x > $UNSET_VAR/a.py",
        "D=/tmp; rm -rf $D/x",
        "echo x > $(mktemp -d)/a",
        "echo x > `pwd`/a",
        "cd $UNSET_VAR && echo x > a.py",
        "cd - && rm a.py",
    ],
)
def test_先頭が展開できないパスは範囲にかかわらず止める(places, monkeypatch, scope, command):
    monkeypatch.delenv("UNSET_VAR", raising=False)
    monkeypatch.delenv("D", raising=False)
    assert reason(places, scope, command) is RefusalReason.UNKNOWN_PLACE


def test_固定の前置きで場所が決まるglobは許す(places: Places):
    assert bash(places, NON_TESTS, "rm src/*.pyc") is None
    assert reason(places, NON_TESTS, "rm tests/*.py") is RefusalReason.TESTS_PROTECTED
    assert reason(places, NON_TESTS, "rm src/../tests/*") is RefusalReason.TESTS_PROTECTED
    assert bash(places, TESTS_ONLY, "rm tests/*.json") is None
    assert reason(places, WriteScope.LISTED, "rm src/*.py") is RefusalReason.NOT_LISTED


# --- cd・pushd・git -C を追う ---


def test_cdの後の相対パスはcdした先からも数える(places: Places):
    assert (
        reason(places, NON_TESTS, "cd tests && echo x > test_app.py")
        is RefusalReason.TESTS_PROTECTED
    )
    assert (
        reason(places, NONE, "pushd /tmp && rm a; popd; rm src/app.py")
        is RefusalReason.READ_ONLY_TREE
    )


def test_サブシェルのcdでも元の場所を候補に残す(places: Places):
    assert reason(places, NONE, "(cd /tmp); rm src/app.py") is RefusalReason.READ_ONLY_TREE


def test_git_Cの先で数える(places: Places):
    assert (
        reason(places, NON_TESTS, "git -C tests checkout test_app.py")
        is RefusalReason.TESTS_PROTECTED
    )
    assert bash(places, NON_TESTS, "git -C src checkout app.py") is None


# --- Bash のコマンド行 ---


@pytest.mark.parametrize(
    "command",
    [
        "cat src/app.py 2>&1 | head",
        'grep -rn "x" src README.md tests 2>/dev/null',
        "git log --oneline > /dev/null",
        "uv run pytest -q 2>&1 | tail -20",
        "PYTHONPATH=src python3 -c \"print('inf ->', 1)\"",
        "python3 -c \"print('rm -rf src/app.py')\"",
        'echo "a > b"',
        "git status && git diff",
        "echo rm src/app.py",
        "grep -l x src | head",
        "cat <<'EOF' > /tmp/t.py\nif a > b:\n    rm = 1\nEOF\npython3 /tmp/t.py",
        "cat <<EOF > /tmp/t.txt\nx > src/app.py\nEOF",
        "cat <<< 'a > src/app.py'",
        "diff <(cat src/app.py) src/app.py",
    ],
)
@pytest.mark.parametrize("scope", [NONE, NON_TESTS])
def test_読むだけのコマンドは書き込みと数えない(places: Places, scope, command):
    assert bash(places, scope, command) is None


@pytest.mark.parametrize(
    "command",
    [
        "cat x > src/app.py",
        "cat x >src/app.py",
        "cat x >> src/app.py",
        "cat x 1> src/app.py",
        "echo x &> src/app.py",
        "rm src/app.py",
        "echo x | tee src/app.py",
        "mv src/app.py src/b.py",
        "cp /tmp/x src/app.py",
        "cp -t src /tmp/x",
        "ln -s /tmp/x src/link",
        "install /tmp/x src/app.py",
        "rsync -a /tmp/x/ src/",
        "touch src/new.py",
        "truncate -s 0 src/app.py",
        "dd if=/dev/zero of=src/app.py",
        "sed -i s/a/b/ src/app.py",
        "sed --in-place=.bak -e s/a/b/ src/app.py",
        "perl -pi -e 's/a/b/' src/app.py",
        "sqlite3 src/db.sqlite 'create table t(x)'",
        "find src -name '*.pyc' -delete",
        "find src -name '*.py' -exec rm {} \\;",
        "git checkout src/app.py",
        "git restore src/app.py",
        "git rm src/app.py",
        "git clean -fd",
        "git reset --hard",
        "find . -name '*.pyc' | xargs rm",
        "true; rm src/app.py",
        "true\nrm src/app.py",
        "bash -c 'echo x > src/app.py'",
        'echo "$(rm src/app.py)"',
        "cat <<'EOF' > src/app.py\nx\nEOF",
    ],
)
def test_読むだけのステージは書き込むコマンドを止める(places: Places, command: str):
    assert reason(places, NONE, command) is RefusalReason.READ_ONLY_TREE


@pytest.mark.parametrize(
    "command",
    [
        "echo x > tests/test_app.py",
        "sed -i s/a/b/ tests/test_app.py",
        "git checkout -- tests/test_app.py",
        "rm -rf tests",
        "rm -rf .",
        "find tests -exec rm {} +",
        "find . -delete",
    ],
)
def test_実装のステージはテストを書き換えるコマンドを止める(places: Places, command: str):
    assert reason(places, NON_TESTS, command) is RefusalReason.TESTS_PROTECTED


def test_ディレクトリの中は追跡しているファイルだけをたどる(places: Places):
    """git が無視している build/ と、追跡していない scratch/ の中のテストの名前では止めない。"""
    assert bash(places, NON_TESTS, "rm -rf build") is None
    assert bash(places, NON_TESTS, "rm -rf scratch") is None
    assert bash(places, NON_TESTS, "rm -rf src") is None


@pytest.mark.parametrize(
    "command",
    [
        "echo x > src/app.py",
        "rm src/app.py",
        "sed -i 's/x/y/' src/app.py",
        "sed -i -e 's/x/y/' src/app.py",
        "cp src/app.py src/b.py",
        "cp tests/test_app.py src/copy.py",
        "truncate -s 0 src/app.py",
    ],
)
def test_実装のステージはテスト以外を書き換えられる(places: Places, command: str):
    assert bash(places, NON_TESTS, command) is None


def test_期待値を決めるステージはテストだけを書ける(places: Places):
    assert bash(places, TESTS_ONLY, "sed -i -e 's/1/2/' tests/golden.json") is None
    assert bash(places, TESTS_ONLY, "truncate -s 0 tests/golden.json") is None
    assert (
        reason(places, TESTS_ONLY, "sed -i 's/1/2/' src/app.py")
        is RefusalReason.NON_TESTS_PROTECTED
    )


def test_引用が閉じていなければ止める側に倒す(places: Places):
    assert bash(places, NONE, "echo 'unterminated > src/app.py") is not None


# --- gh と git push ---


@pytest.mark.parametrize(
    "command",
    [
        "gh pr list",
        "gh pr create --fill",
        "/usr/bin/gh api repos/o/r",
        "GH_TOKEN=x gh pr list",
        "env gh pr list",
        "echo $(gh pr list)",
        "bash -lc 'gh pr merge 1'",
        "find . -exec gh pr list \\;",
        "git push origin x",
        "git -C /w push origin HEAD",
        "git -c alias.p=push p origin",
        "true && git push origin main",
        "GH=gh; $GH pr create",
        "$(echo gh) pr list",
        "${CMD} x",
    ],
)
@pytest.mark.parametrize("scope", [NONE, NON_TESTS, STUBS])
def test_ghとgit_pushと展開できないコマンド名は止めない(
    places: Places, monkeypatch: pytest.MonkeyPatch, scope, command: str
):
    monkeypatch.delenv("GH", raising=False)
    monkeypatch.delenv("CMD", raising=False)
    assert bash(places, scope, command) is None


def test_git_の別名の中の書き込みは今までどおり数える(places: Places):
    assert reason(places, NONE, "git -c 'alias.w=!rm src/app.py' w") is RefusalReason.READ_ONLY_TREE


def test_ghと同じ行の書き込みは止める(places: Places):
    assert reason(places, NONE, "gh pr view 1 && rm src/app.py") is RefusalReason.READ_ONLY_TREE


def test_入れ子が深すぎて読めないコマンド行は止める(places: Places):
    command = "echo x"
    for _ in range(8):
        command = f"bash -c {shlex.quote(command)}"
    assert reason(places, STUBS, command) is RefusalReason.UNKNOWN_PLACE


@pytest.mark.parametrize("command", ['apply_patch "$P"', "apply_patch $(cat /tmp/p)"])
def test_宛先の読めないapply_patchは止める(places: Places, command: str):
    assert reason(places, STUBS, command) is RefusalReason.UNKNOWN_PLACE


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --grep push",
        "echo gh",
        "grep -rn 'gh pr' src",
        "git commit -m 'push it'",
        "echo '$(gh pr list)'",
        "cat <<'EOF'\n$(gh pr list)\nEOF",
    ],
)
def test_ghとgit_pushの語を含むだけのコマンドも止めない(places: Places, command: str):
    assert bash(places, STUBS, command) is None


# --- 受け渡し ---


def test_Guardと場所は環境変数で往復する(places: Places):
    rules = Guard(WriteScope.LISTED, judge=True, reads_design=False, can_ask=True)
    made = context(places, listed=("./a/b.py",))
    back_guard, back = guard.load(guard.stage_env(rules, made))
    assert back_guard == rules
    assert back == made
    assert back.listed == ("a/b.py",)
    assert back.answers_dir == str(places.run_dir / "answers")


def test_テストのパスが渡らなかったら既定に戻す(places: Places):
    made = replace(context(places), test_globs=())
    _, back = guard.load(guard.stage_env(Guard(NON_TESTS), made))
    assert back.test_globs == DEFAULT_TEST_GLOBS


def test_設定が渡っていなければ使えない():
    with pytest.raises(guard.GuardUnavailable):
        guard.load({})
    with pytest.raises(guard.GuardUnavailable):
        guard.load({guard.GUARD_ENV: '{"guard": {"writes": "bogus"}}'})


def test_フックの設定は2つのフックをモジュールの実行で呼ぶ(tmp_path: Path):
    """worktree に置かず、--settings で渡す。"""
    path = guard.write_hook_settings(tmp_path / "run" / "guard.json")
    entries = json.loads(path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert [e["matcher"] for e in entries] == ["Write|Edit|MultiEdit|NotebookEdit|Bash", "Bash"]
    for entry, name in zip(entries, ["deny-writes.py", "park-on-ask.py"], strict=True):
        command = entry["hooks"][0]["command"]
        assert command == shlex.join([sys.executable, "-I", "-m", HOOK_MODULE, name]) + " || exit 2"
    assert guard.hook_settings() == json.loads(path.read_text(encoding="utf-8"))
    assert not (tmp_path / "run" / ".guard.json.tmp").exists()


# --- 入口のスクリプトを起動する ---


def env_for(places: Places, scope: WriteScope, *, can_ask: bool = False) -> dict[str, str]:
    return guard.stage_env(Guard(scope, can_ask=can_ask), context(places))


def hook_command(name: str, module: str = HOOK_MODULE) -> str:
    """hook_settings() の deny-writes のコマンドから、名前とモジュールを差し替えたコマンドを作る。"""
    entry = guard.hook_settings()["hooks"]["PreToolUse"][0]
    command = entry["hooks"][0]["command"]
    return command.replace(HOOK_MODULE, module).replace("deny-writes.py", name)


def run_hook(name: str, payload: Any, env: dict[str, str]):
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    full_env = {k: v for k, v in os.environ.items() if k != guard.GUARD_ENV}
    full_env.update(env)
    return subprocess.run(
        ["sh", "-c", hook_command(name)],
        input=stdin,
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )


def payload(places: Places, tool: str, tool_input: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"tool_name": tool, "tool_input": tool_input, "cwd": str(places.tree), **extra}


def test_deny_writesは終了コード2と標準エラーの理由で止める(places: Places):
    got = run_hook(
        "deny-writes.py",
        payload(places, "Edit", {"file_path": "tests/test_app.py"}),
        env_for(places, NON_TESTS),
    )
    assert got.returncode == 2
    assert "テスト" in got.stderr


def test_autodevをimportできないときもフックは終了コード2で止める(places: Places):
    full_env = {k: v for k, v in os.environ.items() if k != guard.GUARD_ENV}
    full_env.update(env_for(places, NON_TESTS))
    got = subprocess.run(
        ["sh", "-c", hook_command("deny-writes.py", "autodev_absent.hook")],
        input=json.dumps(payload(places, "Edit", {"file_path": "src/app.py"})),
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )
    assert got.returncode == 2


def test_deny_writesは書いてよい呼び出しを通す(places: Places):
    got = run_hook(
        "deny-writes.py",
        payload(places, "Edit", {"file_path": "src/app.py"}),
        env_for(places, NON_TESTS),
    )
    assert got.returncode == 0
    assert got.stderr == ""


@pytest.mark.parametrize("name", ["deny-writes.py", "park-on-ask.py"])
def test_入力が読めなければ止める(places: Places, name: str):
    got = run_hook(name, "not json", env_for(places, NON_TESTS))
    assert got.returncode == 2
    assert "読めなかった" in got.stderr


@pytest.mark.parametrize("name", ["deny-writes.py", "park-on-ask.py"])
def test_設定が渡っていなければ止める(places: Places, name: str):
    got = run_hook(name, payload(places, "Bash", {"command": "ls"}), {})
    assert got.returncode == 2
    assert "設定が渡っていない" in got.stderr


def test_知らないフックの名前は止める(places: Places):
    got = run_hook("no-such-hook.py", payload(places, "Bash", {"command": "ls"}), {})
    assert got.returncode == 2


# --- ask ---

LAUNCHER = "/home/u/.local/bin/autodev"


@pytest.mark.parametrize(
    ("command", "question"),
    [
        (f'{LAUNCHER} ask --question "空は None か"', "空は None か"),
        (f"python3 {LAUNCHER} ask --question=どちら", "どちら"),
        (f"python3 {LAUNCHER} ask", ""),
    ],
)
def test_入口のパスで呼ばれたaskを見分ける(command: str, question: str):
    found = guard.parse_ask(command)
    assert found is not None
    assert found.question == question


@pytest.mark.parametrize(
    "command", [f"{LAUNCHER} status --json", "echo ask --question x", "ask --question x"]
)
def test_ask以外は見分けない(command: str):
    assert guard.parse_ask(command) is None


def test_PATHのコマンド名のautodevのaskを見分ける():
    found = guard.parse_ask("/home/u/.local/bin/autodev ask --question 'A か B か'")
    assert found is not None
    assert found.question == "A か B か"
    assert guard.parse_ask("autodev ask --question 'A か B か'") is not None


def test_askでないautodevの呼び出しと旧い入口は見分けない():
    assert guard.parse_ask("autodev status --json") is None
    assert guard.parse_ask("python3 /s/scripts/autodev.py ask --question x") is None


def test_止まった呼び出しから質問を読む():
    assert guard.ask_question({"command": f"{LAUNCHER} ask --question 'x か y か'"}) == "x か y か"
    assert guard.ask_question({"command": "ls"}) is None


def ask_payload(places: Places, command: str | None = None, tool_use_id: str = "toolu_01"):
    command = command or f"python3 {LAUNCHER} ask --question 'どちら?'"
    return payload(places, "Bash", {"command": command}, tool_use_id=tool_use_id)


def decision(stdout: str) -> dict[str, Any]:
    return json.loads(stdout)["hookSpecificOutput"]


def answered(places: Places, tool_use_id: str = "toolu_01") -> Path:
    answers = places.run_dir / "answers"
    answers.mkdir(parents=True, exist_ok=True)
    path = answers / f"{tool_use_id}.json"
    path.write_text("{}", encoding="utf-8")
    return path


def test_回答が無ければdeferで止め何度呼んでも同じ判断になる(places: Places):
    env = env_for(places, NONE, can_ask=True)
    for _ in range(2):
        got = run_hook("park-on-ask.py", ask_payload(places), env)
        assert got.returncode == 0
        assert decision(got.stdout) == {
            "hookEventName": "PreToolUse",
            "permissionDecision": "defer",
        }


def test_回答のファイルがあれば通しaskの語だけで組み直す(places: Places):
    """回答のファイルの実在だけを見て、中身は解釈しない。後ろのパイプや `;` は落とす。"""
    path = answered(places)
    command = f"python3 {LAUNCHER} ask --question 'どちら?' 2>&1 | tail -5; rm -rf src"
    got = run_hook(
        "park-on-ask.py", ask_payload(places, command), env_for(places, NONE, can_ask=True)
    )
    assert got.returncode == 0
    reply = decision(got.stdout)
    assert reply["permissionDecision"] == "allow"
    rebuilt = reply["updatedInput"]["command"]
    assert shlex.split(rebuilt) == [
        "python3",
        LAUNCHER,
        "ask",
        "--question",
        "どちら?",
        "--answer-file",
        str(path),
    ]


def test_フックが組み直したaskのコマンドを流すと反応が書いた回答が出る(
    tmp_path: Path, places: Places
):
    """フック（park-on-ask）→ 組み直したコマンド → `autodev ask` を、つないで流す。"""
    paths = RunPaths(RunName("r"), places.run_dir)
    write_answer(paths, "toolu_01", "300 秒にする")
    launcher = tmp_path / "bin" / "autodev"
    launcher.parent.mkdir()
    launcher.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -m autodev "$@"\n', encoding="utf-8"
    )
    launcher.chmod(0o755)
    command = f"{launcher} ask --question 'どちら?'"
    got = run_hook(
        "park-on-ask.py", ask_payload(places, command), env_for(places, NONE, can_ask=True)
    )
    rebuilt = decision(got.stdout)["updatedInput"]["command"]
    ran = subprocess.run(
        ["bash", "-c", rebuilt], capture_output=True, text=True, check=False, cwd=places.tree
    )
    assert (ran.returncode, ran.stdout) == (0, "300 秒にする\n"), ran.stderr


def test_別の呼び出しの回答では通さない(places: Places):
    answered(places, "toolu_99")
    got = run_hook("park-on-ask.py", ask_payload(places), env_for(places, NONE, can_ask=True))
    assert decision(got.stdout)["permissionDecision"] == "defer"


def test_askで聞けないステージのaskは止める(places: Places):
    got = run_hook("park-on-ask.py", ask_payload(places), env_for(places, NONE))
    assert got.returncode == 2
    assert "ask で聞けません" in got.stderr


def test_ファイル名にならないtool_use_idは止める(places: Places):
    got = run_hook(
        "park-on-ask.py",
        ask_payload(places, tool_use_id="../x"),
        env_for(places, NONE, can_ask=True),
    )
    assert got.returncode == 2


def test_ask以外のBashはpark_on_askが通す(places: Places):
    env = env_for(places, NONE, can_ask=True)
    got = run_hook("park-on-ask.py", payload(places, "Bash", {"command": "ls"}), env)
    assert got.returncode == 0
    assert got.stdout == ""


def test_askの呼び出しは書き込みとして止めない(places: Places):
    """deny > defer なので、deny-writes が ask を止めると defer に届かない。"""
    assert bash(places, NONE, f"python3 {LAUNCHER} ask --question 'a > b か'") is None


# --- Codex の apply_patch ---


def patch_of(*lines: str) -> str:
    return "\n".join(["*** Begin Patch", *lines, "*** End Patch"]) + "\n"


def apply_patch(places: Places, scope: WriteScope, patch: str, **fields: Any):
    return check(places, scope, "apply_patch", {"command": patch}, **fields)


def test_patch_pathsはパッチの宛先を行の順に返す():
    patch = (
        "*** Begin Patch\n*** Add File: a.py\n+x\n*** Update File: b.py\n"
        "*** Move to: c.py\n*** Delete File: d.py\n*** End Patch"
    )
    assert guard.patch_paths(patch) == ["a.py", "b.py", "c.py", "d.py"]


def test_patch_pathsは本文の行と宛先の無いパッチを宛先に数えない():
    body = patch_of("*** Update File: a.py", "@@", "-*** Add File: not_a_target.py", "+x")
    assert guard.patch_paths(body) == ["a.py"]
    assert guard.patch_paths(patch_of()) == []
    assert guard.patch_paths("") == []


def test_apply_patchでworktreeの外をAddしてもMove_toしても止めない(places: Places):
    outside = str(places.repo / "a.py")
    assert check(places, STUBS, "Write", {"file_path": outside}) is None
    assert apply_patch(places, STUBS, patch_of(f"*** Add File: {outside}", "+x")) is None
    moved = patch_of("*** Update File: src/app.py", f"*** Move to: {places.repo}/moved.py", "@@")
    assert apply_patch(places, STUBS, moved) is None


def test_apply_patchでテストをUpdateすると実装のステージは止める(places: Places):
    patch = patch_of(
        "*** Update File: tests/test_app.py", "@@", "-def test(): ...", "+def test(): 1"
    )
    refusal = apply_patch(places, NON_TESTS, patch)
    assert refusal is not None
    assert refusal.reason is RefusalReason.TESTS_PROTECTED
    assert apply_patch(places, TESTS_ONLY, patch) is None


def test_apply_patchはDeleteとMove_toのテストのパスも止める(places: Places):
    delete = patch_of("*** Delete File: tests/test_app.py")
    moved = patch_of("*** Update File: src/app.py", "*** Move to: tests/test_new.py", "@@")
    for patch in (delete, moved):
        refusal = apply_patch(places, NON_TESTS, patch)
        assert refusal is not None
        assert refusal.reason is RefusalReason.TESTS_PROTECTED


def test_apply_patchは複数の宛先の1つでも止める対象なら止める(places: Places):
    patch = patch_of(
        "*** Add File: src/ok.py", "+x", "*** Update File: tests/test_app.py", "@@", "+y"
    )
    refusal = apply_patch(places, NON_TESTS, patch)
    assert refusal is not None
    assert refusal.reason is RefusalReason.TESTS_PROTECTED


def test_apply_patchは書いてよい宛先を通す(places: Places):
    patch = patch_of("*** Add File: src/new.py", "+x", "*** Update File: src/app.py", "@@")
    assert apply_patch(places, NON_TESTS, patch) is None
    assert apply_patch(places, STUBS, patch_of("*** Add File: tests/test_new.py", "+x")) is None


def test_apply_patchは読むだけのステージで止める(places: Places):
    refusal = apply_patch(places, NONE, patch_of("*** Add File: src/new.py", "+x"))
    assert refusal is not None
    assert refusal.reason is RefusalReason.READ_ONLY_TREE


def test_apply_patchは衝突したファイルだけを書ける(places: Places):
    listed = ("src/app.py",)
    ok = patch_of("*** Update File: src/app.py", "@@")
    other = patch_of("*** Update File: src/b.py", "@@")
    assert apply_patch(places, WriteScope.LISTED, ok, listed=listed) is None
    refusal = apply_patch(places, WriteScope.LISTED, other, listed=listed)
    assert refusal is not None
    assert refusal.reason is RefusalReason.NOT_LISTED
