"""AgentRuntime: 偽の claude（`fake_claude.py`）を起動して、起きた事実の読み方を確かめる。"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from autodev.adapters.agent.types import (
    AgentCall,
    Ending,
    Progress,
    Usage,
)
from autodev.adapters.claude.agent_runtime import AgentRuntime, argv_for, env_for
from autodev.domain.value_objects.session_id import SessionId

FAKE = Path(__file__).with_name("fake_claude.py")
SESSION = SessionId("0b6f3c1e-9a8d-4c2b-8e7f-1a2b3c4d5e6f")


@pytest.fixture
def claude(tmp_path: Path) -> str:
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return str(wrapper)


def make_call(tmp_path: Path, **fields: Any) -> AgentCall:
    values: dict[str, Any] = {
        "prompt": "やること",
        "cwd": str(tmp_path),
        "session": SESSION,
        "resume": False,
        "log_path": str(tmp_path / "logs" / "task1-Impl-r0-a1.jsonl"),
    }
    values.update(fields)
    return AgentCall(**values)


def runtime(claude: str, **kwargs: Any) -> AgentRuntime:
    return AgentRuntime(claude, progress_interval=0.0, **kwargs)


def scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> Path:
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", name)
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    return record


def test_ツールの一覧はカンマでつないで1引数にしプロンプトは引数に置かない():
    """--allowedTools は次のオプションまで後ろの引数を全部取る。"""
    call = AgentCall(
        prompt="やること",
        cwd="/w",
        session=SESSION,
        resume=False,
        log_path="/l",
        json_schema='{"type": "object"}',
        model="opus",
        effort="high",
        max_turns=40,
        settings="/run/guard.json",
        system_append="必須ルール",
        allowed_tools=("Read", "Edit"),
        disallowed_tools=("WebFetch", "Task"),
    )
    argv = argv_for(call)
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--allowedTools") + 1] == "Read,Edit"
    assert argv[argv.index("--disallowedTools") + 1] == "WebFetch,Task"
    assert argv[argv.index("--json-schema") + 1] == '{"type": "object"}'
    assert argv[argv.index("--settings") + 1] == "/run/guard.json"
    assert argv[argv.index("--max-turns") + 1] == "40"
    assert argv[argv.index("--session-id") + 1] == str(SESSION)
    assert "やること" not in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_system_appendの後ろにStructuredOutputの返し方を足して渡す():
    call = AgentCall(
        prompt="x", cwd="/w", session=SESSION, resume=False, log_path="/l", system_append="X"
    )
    argv = argv_for(call)
    value = argv[argv.index("--append-system-prompt") + 1]
    assert value.startswith("X")
    assert "StructuredOutput" in value


def test_system_appendが無くても返し方の一言は渡す():
    call = AgentCall(prompt="x", cwd="/w", session=SESSION, resume=False, log_path="/l")
    argv = argv_for(call)
    assert "StructuredOutput" in argv[argv.index("--append-system-prompt") + 1]


def test_再開はresumeでセッションを続ける():
    call = AgentCall(prompt=None, cwd="/w", session=SESSION, resume=True, log_path="/l")
    argv = argv_for(call)
    assert argv[argv.index("--resume") + 1] == str(SESSION)
    assert "--session-id" not in argv


def test_resultから事実を拾う(claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    record = scenario(monkeypatch, tmp_path, "success")
    seen: list[Progress] = []
    got = runtime(claude).run(make_call(tmp_path), seen.append)
    assert got.ending is Ending.RESULT
    assert got.exit_code == 0
    assert got.subtype == "success"
    assert not got.is_error
    assert got.structured == {"ok": True}
    assert got.text == "done"
    assert got.num_turns == 2
    assert got.usage == Usage(10, 5, 100, 7)
    assert got.cost_usd == 0.25
    assert got.hook_denials == 2
    assert got.deferred is None
    assert not got.rate_limited
    assert got.capabilities == ("interrupt_cancel_queued_v1",)
    # 進み具合: 同じ id のメッセージは 1 ターンに数え、直前のツールを知らせる
    assert seen[-1].turns == 2
    assert seen[-1].last_tool == "Bash"
    assert seen[-1].hook_denials == 2
    # プロンプトは標準入力から、uuid を振って渡す
    sent = json.loads(json.loads(record.read_text(encoding="utf-8"))["stdin"][0])
    assert sent["type"] == "user"
    assert sent["message"]["content"] == "やること"
    assert sent["uuid"]


CALLER_ENV = {
    "ANTHROPIC_API_KEY": "k",
    "ANTHROPIC_AUTH_TOKEN": "t",
    "ANTHROPIC_BASE_URL": "https://example.test",
    "GH_TOKEN": "gh-real",
    "GITHUB_TOKEN": "ghs-real",
    "GH_CONFIG_DIR": "/caller/gh",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "a.b",
    "GIT_CONFIG_VALUE_0": "1",
}


def set_caller_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            monkeypatch.delenv(name)
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    for name, value in CALLER_ENV.items():
        monkeypatch.setenv(name, value)


def test_env_forは呼び出し元の環境変数を値を変えずに渡し_call_envを足す(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    set_caller_env(monkeypatch)
    env = env_for(make_call(tmp_path, env={"AUTODEV_GUARD": "{}"}))
    for name, value in CALLER_ENV.items():
        assert env[name] == value
    assert env["AUTODEV_GUARD"] == "{}"
    assert "GIT_TERMINAL_PROMPT" not in env


def test_課金とGitHubの変数と_git設定は呼び出し元のまま子のプロセスに届く(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    record = scenario(monkeypatch, tmp_path, "success")
    set_caller_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    runtime(claude).run(make_call(tmp_path, env={"AUTODEV_GUARD": "{}"}))
    seen = json.loads(record.read_text(encoding="utf-8"))
    for name, value in CALLER_ENV.items():
        assert seen["env"][name] == value
    assert seen["env"]["AUTODEV_GUARD"] == "{}"
    assert "GIT_TERMINAL_PROMPT" not in seen["env"]
    assert seen["oauth"] == "oauth"


def test_構造化出力が空でもsuccessのまま空を返す(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """形を確かめて失敗にするのは実行器と Task で、ここでは空を空のまま返す。"""
    scenario(monkeypatch, tmp_path, "empty-structured")
    got = runtime(claude).run(make_call(tmp_path))
    assert got.subtype == "success"
    assert got.structured is None
    assert got.text == "スキーマを満たせない"


def test_deferで止まった呼び出しを返す(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """再開ではプロンプトを渡さない。止まった呼び出しは result に載る。"""
    record = scenario(monkeypatch, tmp_path, "deferred")
    got = runtime(claude).run(make_call(tmp_path, prompt=None, resume=True))
    assert got.subtype == "success"
    assert got.exit_code == 0
    assert got.deferred is not None
    assert got.deferred.tool_use_id == "toolu_01"
    assert got.deferred.name == "Bash"
    assert "ask" in got.deferred.input["command"]
    assert json.loads(record.read_text(encoding="utf-8"))["stdin"] == []


def test_resultが無ければ標準エラーを返す(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """引数の誤りは標準エラーにだけ出る。"""
    scenario(monkeypatch, tmp_path, "no-result")
    got = runtime(claude).run(make_call(tmp_path))
    assert got.ending is Ending.NO_RESULT
    assert got.exit_code == 1
    assert "unknown option" in got.stderr
    assert got.subtype is None
    assert not got.initialized


def test_initを受けてから落ちたらセッションを開いた事実を返す(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scenario(monkeypatch, tmp_path, "init-then-crash")
    got = runtime(claude).run(make_call(tmp_path, resume=True))
    assert got.ending is Ending.NO_RESULT
    assert got.initialized


@pytest.mark.parametrize("prompt", [None, "止めたところから続けて"])
def test_見つからないセッションを続けるとinitを受けずにresultで終わった事実を返す(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prompt: str | None
):
    """claude 2.1.288 は init を出さず、`error_during_execution` の result を返す（段 6 の実測）。"""
    scenario(monkeypatch, tmp_path, "session-not-found")
    got = runtime(claude).run(make_call(tmp_path, prompt=prompt, resume=True))
    assert got.ending is Ending.RESULT
    assert got.exit_code == 1
    assert got.subtype == "error_during_execution" and got.is_error
    assert got.num_turns == 0
    assert not got.initialized
    # result の欄は無く、理由は `errors` に載る
    assert got.text == f"No conversation found with session ID: {SESSION}"


def test_ターンの上限で終わった事実を返す(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scenario(monkeypatch, tmp_path, "max-turns")
    got = runtime(claude).run(make_call(tmp_path))
    assert got.exit_code == 1
    assert got.subtype == "error_max_turns"
    assert got.terminal_reason == "max_turns"
    assert got.text == "Reached maximum number of turns (1)"


def test_resultの本文があればerrorsより本文を返す(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scenario(monkeypatch, tmp_path, "result-and-errors")
    got = runtime(claude).run(make_call(tmp_path))
    assert got.text == "本文"


def test_利用枠の上限に当たった事実を返す(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scenario(monkeypatch, tmp_path, "rate-limited")
    got = runtime(claude).run(make_call(tmp_path))
    assert got.rate_limited
    assert got.api_error_status == 429


LIMIT_TEXT = "You've hit your limit · resets 5pm"
OVERLOAD_TEXT = "API Error: Opus is experiencing high load, please use /model to switch to Sonnet"


@pytest.mark.parametrize(
    ("shape", "limited"),
    [
        # 上限の文言と重なれば上限（result が無くても）
        ({"text": LIMIT_TEXT}, True),
        ({"text": LIMIT_TEXT, "result": True}, True),
        # 529 の過負荷にも同じ印が付く。上限ではないので、パニックにせず普通の失敗にする
        ({"text": OVERLOAD_TEXT}, False),
        ({"text": OVERLOAD_TEXT, "result": True}, False),
        # 「usage limit」の語を含むが、上限ではないと言っている文言
        ({"text": "Server is temporarily limiting requests (not your usage limit)"}, False),
        # 上限の文言に当たらない印だけでは、上限にしない
        ({"text": "API Error: model is blocked"}, False),
        # サブエージェントの印は、ステージの終わり方ではない
        ({"text": LIMIT_TEXT, "parent": "toolu_9"}, False),
        # 印の後にふつうの応答が続いてから落ちたのは、上限で終わったのではない
        ({"text": LIMIT_TEXT, "recovered": True}, False),
    ],
)
def test_assistantのrate_limitの印は上限の文言と重なり最後の応答のときだけ上限にする(
    claude: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: dict[str, Any],
    limited: bool,
):
    scenario(monkeypatch, tmp_path, "assistant-error")
    monkeypatch.setenv("FAKE_CLAUDE_ASSISTANT", json.dumps(shape))
    got = runtime(claude).run(make_call(tmp_path))
    assert got.api_error_status is None
    assert got.rate_limited is limited


def test_overageStatusがrejectedでも利用枠の上限に当たっていない(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """従量の超過を組織で切っていると、ふつうの呼び出しでも `overageStatus` は `rejected` になる。"""
    scenario(monkeypatch, tmp_path, "overage-rejected-then-error")
    got = runtime(claude).run(make_call(tmp_path))
    assert got.is_error
    assert not got.rate_limited


def test_interruptを送るとresultが返る(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """kill と違って usage と停止理由が残る。cancel_queued は capability を見て付ける。"""
    record = scenario(monkeypatch, tmp_path, "interruptible")
    process = runtime(claude).start(make_call(tmp_path))
    threading.Timer(0.3, process.interrupt, args=("止める",)).start()
    got = process.wait()
    assert got.ending is Ending.RESULT
    assert got.subtype == "error_during_execution"
    assert got.interrupted == "止める"
    # `errors` の診断の文は、失敗の理由にしない
    assert got.text == ""
    sent = [json.loads(line) for line in json.loads(record.read_text(encoding="utf-8"))["stdin"]]
    control = [m for m in sent if m["type"] == "control_request"]
    assert control[0]["request"] == {"subtype": "interrupt", "cancel_queued": True}


def test_成功で終わったら上限の知らせが届いても上限に当たっていない(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scenario(monkeypatch, tmp_path, "rate-event-then-success")
    got = runtime(claude).run(make_call(tmp_path))
    assert got.subtype == "success"
    assert not got.rate_limited


def test_interruptは何度呼んでも制御要求を1回だけ送る(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """呼び直すたびに kill の期限を延ばすと、interrupt が効かないときにいつまでも kill しない。"""
    record = scenario(monkeypatch, tmp_path, "stubborn")
    process = runtime(claude, interrupt_grace=0.6).start(make_call(tmp_path))
    for delay in (0.2, 0.4, 0.6):
        threading.Timer(delay, process.interrupt, args=(f"止める {delay}",)).start()
    started = time.monotonic()
    got = process.wait()
    assert got.ending is Ending.KILLED
    assert got.interrupted == "止める 0.2"
    # 最初の interrupt から 0.6 秒で kill する（最後の呼び出しから数えない）
    assert time.monotonic() - started < 1.6
    sent = [json.loads(line) for line in json.loads(record.read_text(encoding="utf-8"))["stdin"]]
    assert sum(1 for m in sent if m["type"] == "control_request") == 1


def test_interruptが効かなければ一定時間でkillする(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """interrupt を送っても result が返らないことがある。"""
    scenario(monkeypatch, tmp_path, "stubborn")
    process = runtime(claude, interrupt_grace=0.3).start(make_call(tmp_path))
    threading.Timer(0.3, process.interrupt, args=("止める",)).start()
    got = process.wait()
    assert got.ending is Ending.KILLED
    assert got.interrupted == "止める"


def test_制限時間を過ぎたらinterruptを送る(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scenario(monkeypatch, tmp_path, "interruptible")
    got = runtime(claude).run(make_call(tmp_path, timeout=0.3))
    assert got.subtype == "error_during_execution"
    assert got.interrupted is not None
    assert "制限時間" in got.interrupted


def test_resultの後に終わらなければkillしてもresultは残る(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """result を見たら標準入力を閉じ、待つ時間を決めて kill する。"""
    scenario(monkeypatch, tmp_path, "lingering")
    got = runtime(claude, exit_grace=0.3).run(make_call(tmp_path))
    assert got.ending is Ending.RESULT
    assert got.subtype == "success"


def test_ログは上書きせず書き足しプロンプトも残す(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """同じステージを 2 度呼ぶことがある。再開ではプロンプトを渡さないので、最初の指示はログにしか残らない。"""
    scenario(monkeypatch, tmp_path, "success")
    call = make_call(tmp_path)
    runtime(claude).run(call)
    runtime(claude).run(call)
    lines = [json.loads(x) for x in Path(call.log_path).read_text(encoding="utf-8").splitlines()]
    headers = [line for line in lines if line["type"] == "autodev/call"]
    assert len(headers) == 2
    assert headers[0]["prompt"] == "やること"
    assert sum(1 for line in lines if line["type"] == "result") == 2


def test_進み具合を受ける関数が落ちてもステージは止めない(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """監視の誤りでステージを止めない。"""
    scenario(monkeypatch, tmp_path, "success")

    def broken(_: Progress) -> None:
        raise RuntimeError("監視の誤り")

    assert runtime(claude).run(make_call(tmp_path), broken).subtype == "success"


def test_進み具合は間隔を空けて知らせる(
    claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """数百のイベントのたびには知らせない。フックに拒まれた回数が変わったときは知らせる。"""
    scenario(monkeypatch, tmp_path, "success")
    seen: list[Progress] = []
    AgentRuntime(claude, progress_interval=3600).run(make_call(tmp_path), seen.append)
    assert 1 <= len(seen) <= 3
    assert seen[-1].hook_denials == 2
