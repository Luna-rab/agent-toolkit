"""CLI（`autodev/cli.py`）: 本物の組み立て・実行器・git で、claude と gh だけを偽物にして通す。

偽の `claude`（`fake_claude_run.py`）と偽の `gh`（`test_adapter_forge.FakeGh`）を PATH の先頭に置く。
対象リポジトリは一時ディレクトリの git リポジトリ（bare の origin を clone したもの）。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from executor_fakes import commit, make_env, make_repo, sh
from fake_claude_run import ASK_ID, QUESTION
from test_adapter_forge import FakeGh
from test_model_classes import flags, stage_argv

from autodev import cli
from autodev.adapters.codex.hook import ASK_ANSWERED_RULE
from autodev.app.driving import assembly
from autodev.app.driving.driver import Driver
from autodev.domain.commands.questions import AnswerQuestion
from autodev.domain.events.questions import QuestionAnswered, QuestionPosted, QuestionWithdrawn
from autodev.domain.events.run import RunStarted
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.instruction import Instruction
from autodev.domain.value_objects.model_class import (
    AgentKind,
    Effort,
    ModelChoice,
    ModelClass,
    ModelClasses,
    ModelName,
)
from autodev.domain.value_objects.parallel_limit import ParallelLimit
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.repository import Repository
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.infra.lock import DriverLock
from autodev.infra.paths import RunPaths
from autodev.infra.repo_config import config_path
from autodev.infra.status import status as status_module
from autodev.infra.store.eventstore import EventStore
from autodev.infra.store.requests import RequestBox
from conftest import AUTODEV_PACKAGE

FAKE_CLAUDE = Path(__file__).with_name("fake_claude_run.py")
FAKE_CODEX = Path(__file__).with_name("fake_codex_run.py")
FAKE_COPILOT = Path(__file__).with_name("fake_copilot_run.py")
NAME = "add-ttl"


@dataclass
class World:
    tmp: Path
    repo: Path
    gh: FakeGh
    capsys: pytest.CaptureFixture[str]

    @property
    def paths(self) -> RunPaths:
        return RunPaths.of(RunName(NAME))

    def cli(self, *args: str) -> tuple[int, str, str]:
        self.capsys.readouterr()
        code = cli.main(list(args))
        out, err = self.capsys.readouterr()
        return code, out, err

    def claude_calls(self) -> list[dict[str, Any]]:
        log = self.tmp / "claude.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def codex_invocations(self) -> list[dict[str, Any]]:
        """偽の codex が受けた起動の全部（`--version`・`login status` も）。"""
        log = self.tmp / "codex.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def codex_calls(self) -> list[dict[str, Any]]:
        """偽の codex が `exec` で起こされた呼び出し。"""
        return [call for call in self.codex_invocations() if "role" in call]

    def copilot_invocations(self) -> list[dict[str, Any]]:
        """偽の copilot が受けた起動の全部（`--version` も）。"""
        log = self.tmp / "copilot.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def copilot_calls(self) -> list[dict[str, Any]]:
        """偽の copilot が `-p` で起こされた呼び出し。"""
        return [call for call in self.copilot_invocations() if "role" in call]

    @property
    def copilot_source(self) -> Path:
        """元の COPILOT_HOME（`config.json` が認証の項目を持つか）。"""
        return self.tmp / "copilot-source"


@pytest.fixture
def world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> World:
    repo = make_repo(tmp_path, monkeypatch)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = FakeGh(bin_dir, monkeypatch)
    gh.reply(["auth", "status"])
    gh.reply(["extension", "list"], out="gh stack\tgithub/gh-stack\tv0.1\n")
    gh.reply(["pr", "list"], out="[]")
    gh.reply(["pr", "create"], out="https://github.com/o/r/pull/7\n")
    gh.reply(
        ["pr", "view"],
        out=json.dumps(
            {
                "number": 7,
                "headRefName": f"stack/{NAME}--task-0",
                "baseRefName": "main",
                "state": "OPEN",
                "isDraft": True,
            }
        ),
    )
    claude = bin_dir / "claude"
    claude.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLAUDE}" "$@"\n', "utf-8")
    claude.chmod(0o755)
    codex = bin_dir / "codex"
    codex.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CODEX}" "$@"\n', "utf-8")
    codex.chmod(0o755)
    copilot = bin_dir / "copilot"
    copilot.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_COPILOT}" "$@"\n', "utf-8")
    copilot.chmod(0o755)
    source = tmp_path / "copilot-source"
    source.mkdir()
    (source / "config.json").write_text(
        json.dumps({"authTokens": {"github.com:u": "t"}, "loggedInUsers": [{"login": "u"}]}),
        "utf-8",
    )
    monkeypatch.setenv("COPILOT_HOME", str(source))
    monkeypatch.setenv("FAKE_COPILOT_SCHEMAS", str(AUTODEV_PACKAGE / "schemas"))
    monkeypatch.setenv("FAKE_COPILOT_LOG", str(tmp_path / "copilot.log"))
    monkeypatch.setenv("FAKE_CODEX_SCHEMAS", str(AUTODEV_PACKAGE / "schemas"))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.log"))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AUTODEV_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("FAKE_CLAUDE_SCHEMAS", str(AUTODEV_PACKAGE / "schemas"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    return World(tmp_path, repo, gh, capsys)


def start(world: World, *extra: str) -> tuple[int, str, str]:
    return world.cli(
        "run", "--name", NAME, "--instruction", "TTL を足す", "--repo", str(world.repo), *extra
    )


# --- run ---


def test_runは計画のaskで回答待ちの4で止まり回答を置いて呼び直すと仕上げて0で終える(
    world: World,
):
    code, _, err = start(world)
    assert code == 4, err
    # ラン統括がユーザーに聞いた質問が questions/ にある
    question = json.loads(world.paths.question(QuestionId(QUESTION)).read_text("utf-8"))
    assert question["status"] == "open"

    code, out, err = world.cli(
        "answer", "--name", NAME, "--question", QUESTION, "--answer", "300 秒"
    )
    assert code == 0, err
    assert json.loads(out)["question"] == QUESTION

    code, _, err = world.cli("run", "--name", NAME)
    assert code == 0, err
    # 回答は ask の回答のファイルを通って、defer で止まった Plan に届いた
    answer = json.loads(world.paths.answer(ASK_ID).read_text("utf-8"))
    assert answer == {"answer": "300 秒"}
    plans = [call for call in world.claude_calls() if call["role"] == "plan"]
    assert [call["resumed"] for call in plans] == [False, True]
    assert all(Path(call["cwd"]) == world.paths.overview_tree for call in plans)
    # 概要 PR を draft で作り、PR は残したまま（gh pr ready は呼ばない）
    calls = [call["args"][:2] for call in world.gh.calls()]
    assert ["pr", "create"] in calls
    assert ["pr", "ready"] not in calls
    (created,) = [call["args"] for call in world.gh.calls() if call["args"][:2] == ["pr", "create"]]
    # タイトルは偽の claude の WriteOverview が返したもの（fake_claude_run.py の OUTPUTS）
    assert created[created.index("--title") + 1] == "[autodev] 何も変えない"
    assert sh(world.repo, "ls-remote", "origin", f"stack/{NAME}--task-0").strip()

    # 終えたランの events を JSON で書き出せる
    code, out, _ = world.cli("events", "--name", NAME)
    assert code == 0
    types = [row["type"] for row in json.loads(out)]
    assert types[0] == "RunStarted"
    assert "RunFinished" in types

    # 終えたランは、--force 無しで worktree を外せる
    code, out, err = world.cli("clean", "--name", NAME)
    assert code == 0, err
    assert json.loads(out)["removed"] == [str(world.paths.overview_tree)]
    assert not world.paths.overview_tree.exists()
    assert world.paths.events_db.is_file()
    assert str(world.paths.overview_tree) not in sh(world.repo, "worktree", "list")


def test_gitがPATHに無ければリポジトリを探す前に道具が足りないと言って1で止める(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    # 偽の claude と gh だけを置き、git を見つけられなくする
    monkeypatch.setenv("PATH", str(world.tmp / "bin"))
    code, _, err = start(world)
    assert code == 1
    assert "git が PATH に無い" in err
    assert "リポジトリが見つからない" not in err


def test_既にあるランに指示を付けたら1で止める(world: World):
    assert start(world)[0] == 4
    code, _, err = start(world)
    assert code == 1
    assert "--instruction を付けない" in err


def test_既にあるランにrepoとbaseを違う値で渡したら1で止める(world: World, tmp_path: Path):
    assert start(world)[0] == 4
    other = tmp_path / "other"
    sh(tmp_path, "init", "-q", str(other))
    code, _, err = world.cli("run", "--name", NAME, "--repo", str(other))
    assert (code, "--repo と違う" in err) == (1, True)
    code, _, err = world.cli("run", "--name", NAME, "--base", "develop")
    assert (code, "--base と違う" in err) == (1, True)


def test_新しいランに指示かrepoが無ければ1で止める(world: World):
    code, _, err = world.cli("run", "--name", NAME, "--repo", str(world.repo))
    assert (code, "--instruction" in err) == (1, True)
    code, _, err = world.cli("run", "--name", NAME, "--instruction", "x")
    assert (code, "--repo が要る" in err) == (1, True)
    code, _, err = world.cli("run", "--name", "Bad_Name", "--instruction", "x")
    assert (code, "ラン名が使えない" in err) == (1, True)
    assert not world.paths.root.exists()


def test_originに概要ブランチがあるラン名は使われているとして1で止める(world: World):
    sh(world.repo, "push", "-q", "origin", f"main:stack/{NAME}--task-0")
    code, _, err = start(world)
    assert code == 1
    assert "使われている" in err
    assert not world.paths.events_db.exists()


def test_道具が足りなければ走らずに1で止める(world: World):
    # 偽の gh は先に足した返事から当てるので、認証の返事を差し替える
    world.gh.replies[0] = {"prefix": ["auth", "status"], "out": "", "err": "", "code": 1}
    world.gh._save()
    code, _, err = start(world)
    assert code == 1
    assert "gh が認証されていない" in err
    assert world.claude_calls() == []


def test_崩れたリポジトリの設定では走らず直し方を出して1で止める(world: World):
    path = config_path(Repository(str(world.repo)))
    path.parent.mkdir(parents=True)
    path.write_text('{"quickChecks": ["uv run ruff check ."], "testGlob": []}', "utf-8")
    code, _, err = start(world)
    assert code == 1
    assert str(path) in err
    assert "知らない欄がある: testGlob" in err
    assert world.claude_calls() == []


def test_リポジトリの設定は実行器に渡りブリーフに載る(world: World):
    path = config_path(Repository(str(world.repo)))
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "quickChecks": ["uv run ruff check ."],
                "regressionTests": ["uv run pytest -q"],
                "testGlobs": ["spec/**"],
                "protected": ["uv.lock"],
                "untested": ["docs/**"],
            }
        ),
        "utf-8",
    )
    code, _, err = start(world)
    assert code == 4
    assert "リポジトリの設定が無い" not in err
    brief = world.paths.brief.read_text("utf-8")
    for written in (
        "`uv run ruff check .`",
        "`uv run pytest -q`",
        "`spec/**`",
        "`uv.lock`",
        "`docs/**`",
    ):
        assert written in brief
    # testGlobs は既定に足さず置き換える
    assert "`**/test_*.py`" not in brief


def test_同じランのdriverが走っている間はrunを1で止める(world: World):
    assert start(world)[0] == 4
    with DriverLock(world.paths.driver_lock):
        code, _, err = world.cli("run", "--name", NAME)
    assert code == 1
    assert "ほかに走っている" in err


def test_引数の誤りは2ではなく1で返す(world: World):
    with pytest.raises(SystemExit) as exited:
        world.cli("run")
    assert exited.value.code == 1


# --- answer・ask ---


def seed_questions(paths: RunPaths, *, withdrawn: bool) -> None:
    """driver を通さずに、質問を 1 つ出した（取り下げた）記録を作る。"""
    escalation = EventId("run#2")
    store = EventStore.open(paths.events_db)
    try:
        store.append(
            CommandId("start"),
            StreamId.run(),
            0,
            [
                RunStarted(
                    RunName(NAME),
                    Instruction("x"),
                    Repository("/r"),
                    BranchName("main"),
                    ParallelLimit(3),
                )
            ],
        )
        events: list[Any] = [QuestionPosted(QuestionId(QUESTION), "どちら?", escalation)]
        if withdrawn:
            events.append(QuestionWithdrawn(QuestionId(QUESTION), escalation, "タスクを止めた"))
        store.append(CommandId("q"), StreamId.questions(), 0, events)
    finally:
        store.close()


def test_answerは取り下げた質問なら理由を添えて1で落ち要求を置かない(world: World):
    seed_questions(world.paths, withdrawn=True)
    code, _, err = world.cli("answer", "--name", NAME, "--question", QUESTION, "--answer", "A")
    assert code == 1
    assert "取り下げた理由: タスクを止めた" in err
    with RequestBox.open(world.paths.events_db) as box:
        assert box.pending() == []


def test_answerは回答を待つ質問への回答をrequestsに置くだけでdriverを起こさない(
    world: World, tmp_path: Path
):
    seed_questions(world.paths, withdrawn=False)
    # 無い質問・空の回答は、Questions が拒む理由で落ちる
    code, _, err = world.cli("answer", "--name", NAME, "--question", "q-none", "--answer", "A")
    assert (code, "q-none という質問は無い" in err) == (1, True)
    code, _, err = world.cli("answer", "--name", NAME, "--question", QUESTION, "--answer", " ")
    assert (code, "回答が空" in err) == (1, True)
    body = tmp_path / "answer.md"
    body.write_text("A にする\n", "utf-8")
    code, out, _ = world.cli(
        "answer", "--name", NAME, "--question", QUESTION, "--answer-file", str(body)
    )
    assert code == 0
    with RequestBox.open(world.paths.events_db) as box:
        (request,) = box.pending()
    assert request.id == json.loads(out)["request"]
    command = request.to_command()
    assert isinstance(command, AnswerQuestion)
    assert (command.question, command.answer) == (QuestionId(QUESTION), "A にする\n")
    assert world.claude_calls() == []


def test_answerはまだ拾われていない回答を当てた後で確かめ同じ質問への2つ目の回答を置かない(
    world: World,
):
    seed_questions(world.paths, withdrawn=False)
    assert world.cli("answer", "--name", NAME, "--question", QUESTION, "--answer", "60")[0] == 0
    code, _, err = world.cli("answer", "--name", NAME, "--question", QUESTION, "--answer", "300")
    assert code == 1
    assert "回答できるのは open の質問だけ" in err
    with RequestBox.open(world.paths.events_db) as box:
        assert len(box.pending()) == 1


def test_answerは確かめる間にdriverが回答を拾っても2つ目の回答を置かない(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    """driver は、requests の行を拾ってイベントにしてから行を消す。確かめる 2 回の読み取りの間に
    それが起きても、どちらの読み取りからも回答が消えない。"""
    seed_questions(world.paths, withdrawn=False)
    assert world.cli("answer", "--name", NAME, "--question", QUESTION, "--answer", "60")[0] == 0
    from autodev.app.driving import assembly  # noqa: PLC0415

    def pick_up() -> None:
        """driver が回答を拾った: QuestionAnswered を足し、requests の行を消す。"""
        with RequestBox.open(world.paths.events_db) as box:
            (request,) = box.pending()
            store = EventStore.open(world.paths.events_db)
            try:
                store.append(
                    CommandId(request.id),
                    StreamId.questions(),
                    1,
                    [QuestionAnswered(QuestionId(QUESTION), "60", EventId("run#2"))],
                )
            finally:
                store.close()
            box.delete(request.id)

    reads: list[str] = []

    def after_first_read(name: str, result: Any) -> Any:
        reads.append(name)
        if len(reads) == 1:
            pick_up()
        return result

    pending = assembly.RequestBox.pending
    read_stream = assembly.EventReader.read_stream
    monkeypatch.setattr(
        assembly.RequestBox,
        "pending",
        lambda self: after_first_read("pending", pending(self)),
    )
    monkeypatch.setattr(
        assembly.EventReader,
        "read_stream",
        lambda self, stream: after_first_read("events", read_stream(self, stream)),
    )
    code, _, err = world.cli("answer", "--name", NAME, "--question", QUESTION, "--answer", "300")
    assert code == 1, err
    assert "回答できるのは open の質問だけ" in err


def test_answerは無いランに要求を置かない(world: World):
    code, _, err = world.cli("answer", "--name", NAME, "--question", QUESTION, "--answer", "A")
    assert (code, "そのランが無い" in err) == (1, True)
    assert not world.paths.root.exists()


def test_askはフックが足した回答のファイルの中身を出す(world: World, tmp_path: Path):
    answer = tmp_path / "toolu_1.json"
    answer.write_text(json.dumps({"answer": "300 秒"}), "utf-8")
    code, out, _ = world.cli("ask", "--question", "どちら?", "--answer-file", str(answer))
    assert (code, out) == (0, "300 秒\n")


def test_askは回答のファイルが無ければ単独で呼び直せと言って1で落ちる(world: World):
    code, _, err = world.cli("ask", "--question", "どちら?")
    assert code == 1
    assert "単独に呼び直す" in err


# --- status ---


def test_statusはnameがあればrun_statusを無ければall_statusesを呼んでJSONを出す(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    seen: list[Any] = []
    monkeypatch.setattr(
        status_module,
        "run_status",
        lambda paths: seen.append(paths) or {"name": paths.name.value},
    )
    monkeypatch.setattr(status_module, "all_statuses", lambda: [{"name": "x"}])
    code, out, _ = world.cli("status", "--json", "--name", NAME)
    assert (code, json.loads(out)) == (0, {"name": NAME})
    assert seen == [world.paths]
    code, out, _ = world.cli("status", "--json")
    assert (code, json.loads(out)) == (0, [{"name": "x"}])


def test_statusは置き場にディレクトリもevents_dbも無いランを5で拒み標準出力に何も出さない(
    world: World,
):
    assert not world.paths.root.exists()
    code, out, err = world.cli("status", "--json", "--name", NAME)
    assert (code, out) == (5, "")
    assert "そのランが無い" in err


def test_statusはランのディレクトリがあってもevents_dbが無ければ5で拒む(world: World):
    world.paths.root.mkdir(parents=True)
    code, out, err = world.cli("status", "--json", "--name", NAME)
    assert (code, out) == (5, "")
    assert "そのランが無い" in err
    assert not world.paths.events_db.exists()


def test_statusはevents_dbのあるランを0でJSONにして返す(world: World):
    seed_questions(world.paths, withdrawn=False)
    code, out, _ = world.cli("status", "--json", "--name", NAME)
    assert code == 0
    assert json.loads(out)["name"] == NAME


def test_設定を何も渡さずに始めたランのstatusはrunのmodelsに既定のモデルとeffortを出す(
    world: World,
):
    assert start(world)[0] == 4
    code, out, err = world.cli("status", "--json", "--name", NAME)
    assert code == 0, err
    assert json.loads(out)["run"]["models"] == {
        "lead": {"model": "opus", "effort": "high", "agent": "claude"},
        "review": {"model": "opus", "effort": "medium", "agent": "claude"},
        "implement": {"model": "sonnet", "effort": "medium", "agent": "claude"},
        "write": {"model": "sonnet", "effort": "medium", "agent": "claude"},
    }


def write_models(world: World, body: object) -> Path:
    path = world.tmp / "config" / "autodev" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return path


def recorded_models(world: World) -> ModelClasses:
    started = assembly.run_started(world.paths)
    assert started is not None
    assert started.models is not None
    return started.models


def impl_flags(
    world: World, monkeypatch: pytest.MonkeyPatch, models: ModelClasses
) -> tuple[str | None, str | None]:
    """記録した値で本物の実行器を組み、Impl の claude の argv に付く `--model` と `--effort`。"""
    exec_dir = world.tmp / "exec"
    exec_dir.mkdir(exist_ok=True)
    env = make_env(exec_dir, monkeypatch, models=models)
    return flags(stage_argv(env, StageKind.IMPL))


def test_models_jsonでsuperを替えて始めたランはRunStartedに記録しImplのargvに付ける(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    write_models(world, {"implement": {"model": "claude-opus-5-5", "effort": "xhigh"}})
    code, _, err = start(world)
    assert code == 4, err
    models = recorded_models(world)
    assert models.of(ModelClass.IMPLEMENT) == ModelChoice(
        ModelName("claude-opus-5-5"), Effort.XHIGH
    )
    assert models.of(ModelClass.LEAD) == ModelClasses.default().of(ModelClass.LEAD)
    code, out, _ = world.cli("status", "--json", "--name", NAME)
    assert json.loads(out)["run"]["models"]["implement"] == {
        "model": "claude-opus-5-5",
        "effort": "xhigh",
        "agent": "claude",
    }
    assert impl_flags(world, monkeypatch, models) == ("claude-opus-5-5", "xhigh")


def test_走り出したランはmodels_jsonを書き換えて呼び直しても記録した値を使う(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    write_models(world, {"implement": {"model": "claude-opus-5-5", "effort": "xhigh"}})
    assert start(world)[0] == 4
    write_models(world, {"implement": {"model": "haiku"}})
    code, _, err = world.cli("run", "--name", NAME)
    assert code == 4, err
    models = recorded_models(world)
    assert models.of(ModelClass.IMPLEMENT) == ModelChoice(
        ModelName("claude-opus-5-5"), Effort.XHIGH
    )
    assert impl_flags(world, monkeypatch, models) == ("claude-opus-5-5", "xhigh")


@pytest.mark.parametrize(
    "body",
    ["{", {"ultra": {}}, {"implement": {"effort": "huge"}}],
    ids=["壊れたJSON", "知らないクラス", "不正なeffort"],
)
def test_models_jsonが崩れていれば新しいランは始めずにパスを出して1で止まる(
    world: World, body: object
):
    path = write_models(world, body)
    code, _, err = start(world)
    assert code == 1
    assert str(path) in err
    assert not world.paths.events_db.exists()
    assert world.claude_calls() == []


def test_既にあるランの呼び直しはmodels_jsonを読まないので崩れていても進む(world: World):
    assert start(world)[0] == 4
    write_models(world, "{")
    code, _, err = world.cli("run", "--name", NAME)
    assert code == 4, err
    assert recorded_models(world) == ModelClasses.default()


def test_statusはラン名の規則に合わない名前を5ではなく1で拒む(world: World):
    code, out, _ = world.cli("status", "--json", "--name", "A B")
    assert (code, out) == (1, "")


def test_statusはnameが無ければランが1つも無くても0で空の配列を返す(world: World):
    code, out, _ = world.cli("status", "--json")
    assert (code, json.loads(out)) == (0, [])


# --- clean・purge ---


def test_cleanは終えていないランのworktreeを外さずforceなら外す(world: World):
    # 回答待ちのランの worktree を外すと、呼び直しても続きから進めない
    assert start(world)[0] == 4
    code, _, err = world.cli("clean", "--name", NAME)
    assert code == 1
    assert "ランを終えていない" in err
    assert world.paths.overview_tree.is_dir()

    code, out, _ = world.cli("clean", "--name", NAME, "--force")
    assert code == 0
    assert json.loads(out)["removed"] == [str(world.paths.overview_tree)]
    assert not world.paths.overview_tree.exists()
    assert world.paths.events_db.is_file()


def test_cleanは始める前に止まったランでは外したものを報告しない(world: World):
    (world.paths.trees / "overview").mkdir(parents=True)
    code, out, _ = world.cli("clean", "--name", NAME, "--force")
    assert code == 0
    assert json.loads(out)["removed"] == []


def test_purgeとcleanはworktreeの未コミットの変更を失うなら消さない(world: World):
    assert start(world)[0] == 4
    branch = f"stack/{NAME}--task-0"
    sh(world.paths.overview_tree, "push", "-q", "origin", f"{branch}:{branch}")
    (world.paths.overview_tree / "wip.txt").write_text("書きかけ\n", "utf-8")
    # 無視されたファイルは変更に数えない
    exclude = world.repo / ".git" / "info" / "exclude"
    exclude.write_text("*.log\n", "utf-8")
    (world.paths.overview_tree / "build.log").write_text("x\n", "utf-8")
    for command in ("purge", "clean"):
        code, _, err = world.cli(command, "--name", NAME)
        assert code == 1
        assert "trees/overview にコミットしていない変更がある: wip.txt" in err
        assert "build.log" not in err
    assert (world.paths.overview_tree / "wip.txt").is_file()


def test_purgeは切り離したHEADのworktreeにしか無いコミットがあれば消さない(world: World):
    assert start(world)[0] == 4
    top = world.paths.stack_top_tree
    sh(world.repo, "worktree", "add", "-q", "--detach", str(top), "HEAD")
    commit(top, "c.txt", "x\n", "切り離した HEAD の上")
    code, _, err = world.cli("purge", "--name", NAME)
    assert code == 1
    assert "trees/stack-top の切り離した HEAD にしか無いコミットがある（1 件）" in err


def test_purgeはfetchしてからpushしていないコミットを数える(world: World):
    assert start(world)[0] == 4
    branch = f"stack/{NAME}--task-0"
    commit(world.paths.overview_tree, "b.txt", "x\n", "別の口から push した")
    # origin の名前でなく URL へ push すると、手元の origin/<ブランチ> は古いまま残る
    url = sh(world.repo, "remote", "get-url", "origin").strip()
    sh(world.paths.overview_tree, "push", "-q", url, f"{branch}:{branch}")
    code, _, err = world.cli("purge", "--name", NAME)
    assert code == 0, err


def test_purgeはoriginに無いコミットがあれば何も消さずforceなら消す(world: World):
    assert start(world)[0] == 4
    branch = f"stack/{NAME}--task-0"
    # 概要ブランチは、計画の後に概要 PR を作るときに push する。ここでは先に push しておく
    sh(world.paths.overview_tree, "push", "-q", "origin", f"{branch}:{branch}")
    commit(world.paths.overview_tree, "b.txt", "x\n", "push していない")
    code, _, err = world.cli("purge", "--name", NAME)
    assert code == 1
    assert f"{branch} に origin に無いコミットがある（1 件。" in err
    assert world.paths.overview_tree.is_dir()

    code, out, err = world.cli("purge", "--name", NAME, "--force")
    assert code == 0, err
    assert json.loads(out)["branches"] == [branch]
    assert not world.paths.root.exists()
    assert sh(world.repo, "branch", "--list", branch).strip() == ""
    # リモートのブランチと PR には触らない
    assert sh(world.repo, "ls-remote", "origin", branch).strip()
    assert not any(call["args"][:2] == ["pr", "close"] for call in world.gh.calls())


def test_purgeはoriginを確かめられなければ止めforceなら消す(world: World):
    assert start(world)[0] == 4
    sh(world.repo, "remote", "set-url", "origin", str(world.tmp / "no-such-origin.git"))
    code, _, err = world.cli("purge", "--name", NAME)
    assert code == 1
    assert "origin を確かめられなかった" in err
    code, _, err = world.cli("purge", "--name", NAME, "--force")
    assert code == 0, err


def test_壊れたworktreeは確かめられなかった理由にしてforceなら消す(world: World):
    assert start(world)[0] == 4
    # worktree の .git（gitdir を指すファイル）を壊す
    (world.paths.overview_tree / ".git").write_text("gitdir: /nowhere\n", "utf-8")
    code, _, err = world.cli("purge", "--name", NAME)
    assert code == 1
    assert "trees/overview を確かめられなかった" in err
    code, _, err = world.cli("purge", "--name", NAME, "--force")
    assert code == 0, err


def test_purgeの理由はpushしていないコミットがマージ済みならforceでよいと添える(world: World):
    assert start(world)[0] == 4
    commit(world.paths.overview_tree, "b.txt", "x\n", "squash でマージした")
    code, _, err = world.cli("purge", "--name", NAME)
    assert code == 1
    assert "PR を squash か rebase でマージ済みなら、--force で消してよい" in err


def spawn_until_plan(world: World, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, int]:
    return spawn_until(world, monkeypatch, "plan")


def spawn_until(world: World, monkeypatch: pytest.MonkeyPatch, role: str) -> tuple[Any, int]:
    """入口（`python -m autodev`）を別のプロセスで起こし、`role` の claude が走り出して止まるまで待つ。"""
    monkeypatch.setenv("FAKE_CLAUDE_HANG", role)
    driver = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-m",
            "autodev",
            *("run", "--name", NAME, "--instruction", "TTL を足す", "--repo", str(world.repo)),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 30

    def found() -> list[dict[str, Any]]:
        return [call for call in world.claude_calls() if call["role"] == role]

    while not found() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not found():
        os.killpg(driver.pid, signal.SIGKILL)
        pytest.fail(f"{role} の claude が走り出さなかった")
    return driver, found()[0]["pid"]


def records(world: World) -> list[Path]:
    """子プロセスの控え（children/）に残っているもの。"""
    folder = world.paths.root / "children"
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def test_interruptに応じないステージは2回目のSIGINTで子を止めて3で終える(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FAKE_CLAUDE_STUBBORN", "1")
    driver, plan = spawn_until_plan(world, monkeypatch)
    try:
        driver.send_signal(signal.SIGINT)
        # 1 回目では止まらない claude を、driver は待っている（interrupt から kill まで 60 秒）
        time.sleep(1)
        assert driver.poll() is None
        assert alive(plan)
        driver.send_signal(signal.SIGINT)
        assert driver.wait(timeout=30) == 3
    finally:
        if driver.poll() is None:
            os.killpg(driver.pid, signal.SIGKILL)
        if alive(plan):
            os.kill(plan, signal.SIGKILL)
    assert not alive(plan)
    assert records(world) == []


def test_統括がターンの途中でもシグナルで止めると統括の子を待ってから終える(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FAKE_CLAUDE_STUBBORN", "1")
    driver, supervisor = spawn_until(world, monkeypatch, "supervisor-run")
    try:
        driver.send_signal(signal.SIGTERM)
        time.sleep(1)
        # 統括の claude が終わるまで、driver は終わらない
        assert driver.poll() is None
        driver.send_signal(signal.SIGTERM)
        assert driver.wait(timeout=30) == 3
    finally:
        if driver.poll() is None:
            os.killpg(driver.pid, signal.SIGKILL)
        if alive(supervisor):
            os.kill(supervisor, signal.SIGKILL)
    assert not alive(supervisor)
    # 控えが残らないので、次の purge は前の driver の子で止まらない
    assert records(world) == []
    code, _, err = world.cli("purge", "--name", NAME)
    assert code == 0, err


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # 終わったが親（落とした driver）に待たれていない子は、ゾンビとして残る
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def test_driverがSIGTERMを受けたらステージを止めてパニックと同じく3で終える(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    driver, plan = spawn_until_plan(world, monkeypatch)
    driver.send_signal(signal.SIGTERM)
    assert driver.wait(timeout=60) == 3
    assert not alive(plan)
    # 止めた実行は interrupted にしたので、記録の上で走っているものは残らない
    code, _, err = world.cli("purge", "--name", NAME)
    assert code == 0, err


def test_driverがSIGKILLで落ちて子のclaudeが生きている間はrunもcleanもpurgeも止める(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    driver, plan = spawn_until_plan(world, monkeypatch)
    os.killpg(driver.pid, signal.SIGKILL)
    driver.wait()
    try:
        # claude は別のセッションで起こすので、driver を落としても残る
        assert alive(plan)
        code, _, err = world.cli("run", "--name", NAME)
        assert code == 1
        assert f"前の driver が起こしたプロセスがまだ走っている: pid {plan}" in err
        # run に --force は無いので、進め方を書く
        assert f"kill {plan}" in err
        assert "--force" not in err
        for command in ("clean", "purge"):
            code, _, err = world.cli(command, "--name", NAME, "--force")
            assert (command, code) == (command, 1)
            assert f"pid {plan}" in err
        assert world.paths.overview_tree.is_dir()
    finally:
        os.kill(plan, signal.SIGKILL)

    # 子が終われば、残るのは記録の上で走っている実行だけ（--force で越えられる）
    deadline = time.monotonic() + 10
    while alive(plan) and time.monotonic() < deadline:
        time.sleep(0.05)
    code, _, err = world.cli("purge", "--name", NAME)
    assert code == 1
    assert "記録の上で走っている実行がある: planning-Plan" in err
    assert "pid" not in err
    assert world.cli("purge", "--name", NAME, "--force")[0] == 0


def test_purgeとcleanはdriverが走っている間はforceでも消さない(world: World):
    assert start(world)[0] == 4
    with DriverLock(world.paths.driver_lock):
        assert world.cli("purge", "--name", NAME, "--force")[0] == 1
        assert world.cli("clean", "--name", NAME)[0] == 1
    assert world.paths.overview_tree.is_dir()


def test_無いランのcleanとpurgeとeventsは1で止める(world: World):
    for command in ("clean", "purge", "events"):
        code, _, err = world.cli(command, "--name", NAME)
        assert (command, code, "そのランが無い" in err) == (command, 1, True)


def test_models_jsonでreviewをcodexにして始めたランはRunStartedに記録しstatusにcodexを出す(
    world: World,
):
    write_models(world, {"review": {"agent": "codex", "model": "gpt-5.5", "effort": "high"}})
    code, _, err = start(world)
    assert code == 4, err
    models = recorded_models(world)
    assert models.of(ModelClass.REVIEW) == ModelChoice(
        ModelName("gpt-5.5"), Effort.HIGH, AgentKind.CODEX
    )
    assert models.of(ModelClass.LEAD).agent is AgentKind.CLAUDE
    code, out, _ = world.cli("status", "--json", "--name", NAME)
    shown = json.loads(out)["run"]["models"]
    assert shown["review"] == {"model": "gpt-5.5", "effort": "high", "agent": "codex"}
    assert shown["lead"]["agent"] == "claude"


def test_agentの合わないmodels_jsonなら新しいランは始めずに1で止まる(world: World):
    path = write_models(world, {"review": {"agent": "codex", "model": "gpt-5.5", "effort": "max"}})
    code, _, err = start(world)
    assert code == 1
    assert str(path) in err
    assert not world.paths.events_db.exists()


# --- codex を選んだラン ---

CODEX_CLASS = {"agent": "codex", "model": "gpt-5.5", "effort": "high"}


def answer_and_resume(world: World) -> int:
    code, _, err = world.cli("answer", "--name", NAME, "--question", QUESTION, "--answer", "300 秒")
    assert code == 0, err
    code, _, err = world.cli("run", "--name", NAME)
    assert code == 0, err
    return code


def test_reviewをcodexにしたランはDesignReviewだけを偽のcodexで起こし残りは偽のclaudeで起こして0で終える(
    world: World,
):
    write_models(world, {"review": CODEX_CLASS})
    code, _, err = start(world)
    assert code == 4, err
    answer_and_resume(world)

    codex_roles = {call["role"] for call in world.codex_calls()}
    claude_roles = {call["role"] for call in world.claude_calls()}
    assert "design-review" in codex_roles
    assert codex_roles <= {"design-review", "design-judge", "review"}
    assert {"plan", "supervisor-run", "write-overview"} <= claude_roles
    assert not claude_roles & codex_roles
    # codex は始めるときの argv（resume ではない）で起こされ、決めたモデルが渡る
    review = next(call for call in world.codex_calls() if call["role"] == "design-review")
    assert review["argv"][:2] == ["exec", "--json"]
    assert review["argv"][review["argv"].index("-m") + 1] == "gpt-5.5"


def test_leadをcodexにしたランは計画のaskで4で止まり回答の後にresumeで続けて0で終える(
    world: World,
):
    write_models(world, {"lead": CODEX_CLASS})
    code, _, err = start(world)
    assert code == 4, err
    # 質問は 1 件だけ（ask の質問と、統括が聞き直した質問が 2 件に増えない）
    questions = sorted(world.paths.questions.glob("*.json"))
    assert [path.stem for path in questions] == [QUESTION]
    assert json.loads(questions[0].read_text("utf-8"))["status"] == "open"

    answer_and_resume(world)

    plans = [call for call in world.codex_calls() if call["role"] == "plan"]
    assert [call["resumed"] for call in plans] == [False, True]
    assert plans[1]["argv"][:3] == ["exec", "resume", "--json"]
    assert plans[1]["argv"][-2:] == [plans[0]["thread"], "-"]
    # 再開のプロンプトに回答と、ask を呼び直さない一言が入る
    assert "300 秒" in plans[1]["stdin"]
    assert ASK_ANSWERED_RULE in plans[1]["stdin"]
    assert json.loads(world.paths.answer(ASK_ID).read_text("utf-8")) == {"answer": "300 秒"}
    # lead は全部 codex で起こされ、claude は計画にも統括にも呼ばれない
    assert {"plan", "supervisor-run"} <= {call["role"] for call in world.codex_calls()}
    assert not {"plan", "supervisor-run"} & {call["role"] for call in world.claude_calls()}
    code, out, _ = world.cli("events", "--name", NAME)
    assert "RunFinished" in [row["type"] for row in json.loads(out)]


def test_leadをcodexにして統括の初回をtimeoutで止めると次の起動は控えたthread_idでresumeする(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    write_models(world, {"lead": CODEX_CLASS})
    monkeypatch.setenv("FAKE_CODEX_DROP_LEAD", "1")
    # 統括の 1 ターンの上限は CLI から変えられないので、組み立てる Driver に短い上限を渡す
    monkeypatch.setattr(
        assembly,
        "Driver",
        lambda *args, **kwargs: Driver(
            *args,
            **kwargs,
            supervisor_setting=lambda setting: replace(setting, timeout=3.0),
        ),
    )
    code, _, err = start(world)
    assert code == 4, err

    leads = [call for call in world.codex_calls() if call["role"] == "supervisor-run"]
    assert len(leads) >= 2
    first, second = leads[0], leads[1]
    assert first["resumed"] is False
    assert first["argv"][:2] == ["exec", "--json"]
    assert second["resumed"] is True
    assert second["argv"][:3] == ["exec", "resume", "--json"]
    assert second["argv"][-2:] == [first["thread"], "-"]


def test_codexのクラスがありloginが通らなければ走らずに1でログインしていない旨を出す(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    write_models(world, {"review": CODEX_CLASS})
    monkeypatch.setenv("FAKE_CODEX_LOGIN", "1")
    code, _, err = start(world)
    assert code == 1
    assert "codex" in err
    assert "ログイン" in err
    assert world.claude_calls() == []
    assert world.codex_calls() == []
    assert not world.paths.events_db.exists()


def test_codexのクラスが無ければcodexを呼ばない(world: World, monkeypatch: pytest.MonkeyPatch):
    # login が通らない codex でも、使わないランは止まらない
    monkeypatch.setenv("FAKE_CODEX_LOGIN", "1")
    code, _, err = start(world)
    assert code == 4, err
    assert world.codex_invocations() == []


# --- copilot を選んだラン ---

COPILOT_CLASS = {"agent": "copilot", "model": "gpt-5.5", "effort": "high"}


def test_reviewをcopilotにしたランはreviewのステージだけを偽のcopilotで起こし残りは偽のclaudeで起こして0で終える(
    world: World,
):
    write_models(world, {"review": COPILOT_CLASS})
    code, _, err = start(world)
    assert code == 4, err
    answer_and_resume(world)

    copilot_roles = {call["role"] for call in world.copilot_calls()}
    claude_roles = {call["role"] for call in world.claude_calls()}
    assert "design-review" in copilot_roles
    assert copilot_roles <= {"design-review", "design-judge", "review"}
    assert {"plan", "supervisor-run", "write-overview"} <= claude_roles
    assert not claude_roles & copilot_roles
    # copilot は始めるときの argv（--resume ではない）で起こされ、決めたモデルが渡る
    review = next(call for call in world.copilot_calls() if call["role"] == "design-review")
    assert "--resume" not in review["argv"]
    assert review["argv"][review["argv"].index("--model") + 1] == "gpt-5.5"
    assert review["argv"][review["argv"].index("--reasoning-effort") + 1] == "high"
    code, out, _ = world.cli("events", "--name", NAME)
    assert "RunFinished" in [row["type"] for row in json.loads(out)]


def test_leadをcopilotにしたランは計画のaskで4で止まり回答の後にresumeで続けて0で終える(
    world: World,
):
    write_models(world, {"lead": COPILOT_CLASS})
    code, _, err = start(world)
    assert code == 4, err
    # 質問は 1 件だけ（ask の質問と、統括が聞き直した質問が 2 件に増えない）
    questions = sorted(world.paths.questions.glob("*.json"))
    assert [path.stem for path in questions] == [QUESTION]
    assert json.loads(questions[0].read_text("utf-8"))["status"] == "open"

    answer_and_resume(world)

    plans = [call for call in world.copilot_calls() if call["role"] == "plan"]
    assert [call["resumed"] for call in plans] == [False, True]
    first, second = plans
    assert first["argv"][first["argv"].index("--session-id") + 1] == first["session"]
    assert second["argv"][second["argv"].index("--resume") + 1] == first["session"]
    assert "--session-id" not in second["argv"]
    # 別プロセスの再開が通るよう、同じ COPILOT_HOME を使う
    assert first["copilot_home"] == second["copilot_home"] == str(world.copilot_source)
    assert not (world.paths.root / "sessions" / "copilot" / "home").exists()
    # 再開のプロンプトに回答と、ask を呼び直さない一言が入る
    assert "300 秒" in second["prompt"]
    assert ASK_ANSWERED_RULE in second["prompt"]
    assert json.loads(world.paths.answer(ASK_ID).read_text("utf-8")) == {"answer": "300 秒"}
    # lead は全部 copilot で起こされ、claude は計画にも統括にも呼ばれない
    assert {"plan", "supervisor-run"} <= {call["role"] for call in world.copilot_calls()}
    assert not {"plan", "supervisor-run"} & {call["role"] for call in world.claude_calls()}
    code, out, _ = world.cli("events", "--name", NAME)
    assert "RunFinished" in [row["type"] for row in json.loads(out)]


def test_implementをcopilotにして上限のquotaを流すとPanicで止まり別のエージェントで起こし直さない(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    write_models(world, {"implement": COPILOT_CLASS})
    monkeypatch.setenv("FAKE_CLAUDE_TASK", "1")
    code, _, err = start(world)
    assert code == 4, err
    code, _, err = world.cli("answer", "--name", NAME, "--question", QUESTION, "--answer", "300 秒")
    assert code == 0, err
    monkeypatch.setenv("FAKE_COPILOT_QUOTA", "1")
    code, _, err = world.cli("run", "--name", NAME)
    assert code == 3, err

    calls = world.copilot_calls()
    assert len(calls) == 1
    # implement のクラスのステージを、claude で起こし直していない
    implement_roles = {"test-gen", "impl", "expect", "fix", "test-fix", "resolve-conflict"}
    assert not implement_roles & {call["role"] for call in world.claude_calls()}
    assert calls[0]["role"] == "test-gen"
    code, out, _ = world.cli("events", "--name", NAME)
    assert "RunPanicked" in [row["type"] for row in json.loads(out)]


def test_copilotのクラスがありログインしていなければ走らずに1でログインしていない旨を出す(
    world: World,
):
    write_models(world, {"review": COPILOT_CLASS})
    (world.copilot_source / "config.json").write_text(json.dumps({"model": "x"}), "utf-8")
    code, _, err = start(world)
    assert code == 1
    assert "copilot" in err
    assert "ログイン" in err
    assert world.claude_calls() == []
    assert world.copilot_calls() == []
    assert not world.paths.events_db.exists()


def test_copilotのクラスが無ければcopilotを呼ばない(world: World):
    # ログインしていない copilot でも、使わないランは止まらない
    (world.copilot_source / "config.json").write_text("{}", "utf-8")
    code, _, err = start(world)
    assert code == 4, err
    assert world.copilot_invocations() == []
