"""CopilotRuntime: 偽の copilot（`fake_copilot.py`）を起動して、JSONL の読み方と起動の仕方を確かめる。"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from autodev.adapters.agent.types import (
    AgentCall,
    DeferredToolUse,
    Ending,
    Progress,
)
from autodev.adapters.codex.hook import ASK_ANSWERED_RULE, PARK_ON_ASK, HookRecord
from autodev.adapters.copilot.agent_runtime import (
    COPILOT_RETURN_RULE,
    CopilotRuntime,
    argv_for,
    limit_event,
)
from autodev.adapters.copilot.launch import source_home
from autodev.domain.value_objects.model_class import AgentKind
from autodev.domain.value_objects.session_id import SessionId

FAKE = Path(__file__).with_name("fake_copilot.py")
SESSION = SessionId("0b6f3c1e-9a8d-4c2b-8e7f-1a2b3c4d5e6f")
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
ASK_COMMAND = "autodev ask --question 'TTL は?'"
RATE_LIMIT_MESSAGE = "Sorry, you have been rate-limited."
QUOTA_MESSAGE = "You have no remaining premium requests."
AUTH = {
    "authTokens": {"github.com:u": "token-value"},
    "loggedInUsers": [{"host": "https://github.com", "login": "u"}],
    "lastLoggedInUser": {"host": "https://github.com", "login": "u"},
}
#: 認証のほかに、呼び出し元の home に在る個人の設定
PERSONAL = {"model": "claude-opus", "trustedFolders": ["/home/u"], "mcpServers": {"x": {}}}


@pytest.fixture
def copilot(tmp_path: Path) -> str:
    wrapper = tmp_path / "copilot"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return str(wrapper)


# --- 偽の copilot が流す JSONL ---


def session_start() -> dict:
    return {"type": "session.start", "data": {"sessionId": str(SESSION)}}


def message(content: str, phase: str | None = None) -> dict:
    data: dict[str, Any] = {"messageId": "m1", "content": content}
    if phase is not None:
        data["phase"] = phase
    return {"type": "assistant.message", "data": data}


def tool_start(name: str = "bash") -> dict:
    return {
        "type": "tool.execution_start",
        "data": {"toolCallId": "call_1", "toolName": name, "arguments": {"command": "ls"}},
    }


def tool_complete() -> dict:
    return {
        "type": "tool.execution_complete",
        "data": {"toolCallId": "call_1", "success": True, "result": {"content": "a.txt"}},
    }


def result(code: int = 0) -> dict:
    return {
        "type": "result",
        "sessionId": str(SESSION),
        "exitCode": code,
        "usage": {"premiumRequests": 1},
    }


def abort() -> dict:
    return {"type": "abort", "data": {"reason": "user_abort"}}


def session_error(error_type: str, code: str, text: str, status: int | None = None) -> dict:
    data: dict[str, Any] = {"errorType": error_type, "errorCode": code, "message": text}
    if status is not None:
        data["statusCode"] = status
    return {"type": "session.error", "data": data}


def auto_mode_switch(code: str = "user_model_rate_limited") -> dict:
    return {"type": "auto_mode_switch.requested", "data": {"requestId": "r1", "errorCode": code}}


def emit(event: dict) -> dict:
    return {"emit": event}


def hook(record: HookRecord) -> dict:
    return {"hook": asdict(record)}


def success_steps(content: str = '{"ok": true}') -> list[dict]:
    return [emit(session_start()), emit(message(content)), emit(result())]


def hanging_steps() -> list[dict]:
    return [emit(session_start()), {"hang": True}]


def ask_record(tool_use_id: str = "T") -> HookRecord:
    return HookRecord(PARK_ON_ASK, "Bash", True, tool_use_id, "TTL は?", ASK_COMMAND)


def denied_record() -> HookRecord:
    return HookRecord("deny-writes", "Bash", True)


def passed_record() -> HookRecord:
    return HookRecord("deny-writes", "Bash", False)


# --- 起動の道具 ---


class Fake:
    """偽の copilot の台本と記録。"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.record = tmp_path / "record.jsonl"
        self.signals = tmp_path / "signals.txt"
        self.source = tmp_path / "source-home"
        monkeypatch.setenv("FAKE_COPILOT_RECORD", str(self.record))
        monkeypatch.setenv("FAKE_COPILOT_SIGNALS", str(self.signals))
        self.login()

    def login(self, config: str | None = None) -> None:
        """元の COPILOT_HOME。既定はログイン済みで、個人の設定も入っている。"""
        self.source.mkdir(exist_ok=True)
        body = config if config is not None else json.dumps({**AUTH, **PERSONAL})
        (self.source / "config.json").write_text(body, encoding="utf-8")
        self.monkeypatch.setenv("COPILOT_HOME", str(self.source))

    def script(self, steps: list[dict], **extra: Any) -> None:
        path = self.tmp_path / "script.json"
        path.write_text(json.dumps({"steps": steps, **extra}, ensure_ascii=False), encoding="utf-8")
        self.monkeypatch.setenv("FAKE_COPILOT_SCRIPT", str(path))

    def started(self) -> list[dict]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text("utf-8").splitlines()]

    def got_sigint(self) -> bool:
        return self.signals.exists()


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    return Fake(tmp_path, monkeypatch)


def make_call(tmp_path: Path, **fields: Any) -> AgentCall:
    values: dict[str, Any] = {
        "prompt": "やること",
        "cwd": str(tmp_path),
        "session": SESSION,
        "resume": False,
        "log_path": str(tmp_path / "run" / "logs" / "task1-Impl-r0-a1.jsonl"),
        "json_schema": json.dumps(SCHEMA),
        "model": "gpt-5.5",
        "effort": "high",
        "agent": AgentKind("copilot"),
        "run_dir": str(tmp_path / "run"),
        "system_append": "必須ルール",
    }
    values.update(fields)
    return AgentCall(**values)


def runtime(copilot: str, **kwargs: Any) -> CopilotRuntime:
    return CopilotRuntime(copilot, progress_interval=0.0, **kwargs)


def hooks_of(call: AgentCall) -> Path:
    return Path(f"{call.log_path}.hooks")


def seed_records(call: AgentCall, *records: HookRecord) -> None:
    path = hooks_of(call)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in records))


def wait_until(condition: Callable[[], bool], what: str, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"{what} にならなかった")


def log_has(call: AgentCall, text: str) -> bool:
    path = Path(call.log_path)
    return path.exists() and text in path.read_text(encoding="utf-8")


def json_objects(text: str) -> list[Any]:
    """文章の中に埋まっている JSON の object を、見つかった順に全部読む。"""
    decoder = json.JSONDecoder()
    found: list[Any] = []
    start = text.find("{")
    while start >= 0:
        try:
            body, _ = decoder.raw_decode(text, start)
        except ValueError:
            body = None
        if isinstance(body, dict):
            found.append(body)
        start = text.find("{", start + 1)
    return found


# --- argv ---


def test_新規のargvはプロンプトと固定の旗とプラグインとモデルとeffortとsession_idを渡す():
    argv = argv_for(make_call(Path("/w")), "PROMPT", "/tmp/plugin")
    assert argv[0] == "copilot"
    assert argv[argv.index("-p") + 1] == "PROMPT"
    assert argv[argv.index("--output-format") + 1] == "json"
    for flag in (
        "--allow-all-tools",
        "--allow-all-paths",
        "--no-ask-user",
    ):
        assert flag in argv
    assert "--disable-builtin-mcps" not in argv
    assert argv[argv.index("--plugin-dir") + 1] == "/tmp/plugin"
    assert argv[argv.index("--model") + 1] == "gpt-5.5"
    assert argv[argv.index("--reasoning-effort") + 1] == "high"
    assert argv[argv.index("--session-id") + 1] == str(SESSION)
    assert "--resume" not in argv


def test_続きのargvはresumeにsessionを渡しsession_idを含まない():
    argv = argv_for(make_call(Path("/w"), resume=True), "PROMPT", "/tmp/plugin", "mycopilot")
    assert argv[0] == "mycopilot"
    assert argv[argv.index("--resume") + 1] == str(SESSION)
    assert "--session-id" not in argv
    assert argv[argv.index("--model") + 1] == "gpt-5.5"
    assert argv[argv.index("--plugin-dir") + 1] == "/tmp/plugin"


# --- 起動と結果 ---


def test_偽のcopilotがassistant_messageとresultを流して0で終わるとAgentOutcomeに読む(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps())
    got = runtime(copilot).run(make_call(tmp_path))
    assert got.ending is Ending.RESULT
    assert got.exit_code == 0
    assert not got.is_error
    assert got.text == '{"ok": true}'
    assert got.structured == {"ok": True}
    assert got.initialized
    assert got.interrupted is None
    assert got.deferred is None
    assert got.hook_denials == 0
    assert not got.rate_limited
    assert got.cost_usd == 0.0


@pytest.mark.parametrize(
    ("first_phase", "last_phase"),
    [("commentary", None), ("final_answer", None), (None, "final_answer")],
    ids=["途中は解説・最後はphase無し", "途中が最終・最後はphase無し", "最後が最終"],
)
def test_assistant_messageが2つ流れたらtextとstructuredは最後のものから読む(
    copilot: str, fake: Fake, tmp_path: Path, first_phase: str | None, last_phase: str | None
):
    fake.script(
        [
            emit(session_start()),
            emit(message('{"ok": false}', first_phase)),
            emit(message('{"ok": true}', last_phase)),
            emit(result()),
        ]
    )
    got = runtime(copilot).run(make_call(tmp_path))
    assert got.text == '{"ok": true}'
    assert got.structured == {"ok": True}


_DESIGN = json.dumps(
    {"ok": True, "design": "## 設計\n\n```python\nx = 1\n```\n"}, ensure_ascii=False
)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('```json\n{"ok": true}\n```', {"ok": True}),
        (f"```json\n{_DESIGN}\n```", json.loads(_DESIGN)),
        ('判定です。\n\n```json\n{"ok": true}\n```', {"ok": True}),
        (
            "判定です。\n\n" + json.dumps({"ok": True, "items": [{"a": 1}]}, indent=2),
            {"ok": True, "items": [{"a": 1}]},
        ),
        ('Both findings are still open.\n\n{"ok": true}', {"ok": True}),
        ('例:\n```json\n{"ok": false}\n```\n答え:\n```json\n{"ok": true}\n```', {"ok": True}),
    ],
    ids=[
        "囲み1つ",
        "囲みの中にコードブロック",
        "前置きと囲み",
        "前置きと字下げしたJSON",
        "前置きと囲み無しのJSON",
        "囲み2つなら末尾のもの",
    ],
)
def test_最後のメッセージの末尾で閉じるJSONのobjectをstructuredに読む(
    copilot: str, fake: Fake, tmp_path: Path, content: str, expected: dict[str, Any]
):
    fake.script(success_steps(content))
    got = runtime(copilot).run(make_call(tmp_path))
    assert got.structured == expected
    assert got.ending is Ending.RESULT
    assert not got.is_error


@pytest.mark.parametrize(
    "content",
    [
        '形は {"ok": true} のようになるが、まだ終わっていない',
        '{"ok": true, "detail": {"a": 1}',
        "{" + '"a": ' * 1 + "[" * 100_000,
    ],
    ids=["前置きの中の例だけ", "外側が閉じず入れ子が末尾で閉じる", "深すぎる入れ子"],
)
def test_末尾で閉じる最上位のobjectが無ければstructuredはNone(
    copilot: str, fake: Fake, tmp_path: Path, content: str
):
    fake.script(success_steps(content))
    got = runtime(copilot).run(make_call(tmp_path))
    assert got.structured is None
    assert got.ending is Ending.RESULT


def test_最後のメッセージがJSONでなければstructuredはNoneでendingはRESULTのまま(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps("終わりました"))
    got = runtime(copilot).run(make_call(tmp_path))
    assert got.structured is None
    assert got.ending is Ending.RESULT
    assert got.text == "終わりました"


def test_resultを流さずに0で終わるとNO_RESULTでエラー(copilot: str, fake: Fake, tmp_path: Path):
    fake.script([emit(session_start()), emit(message('{"ok": true}'))])
    got = runtime(copilot).run(make_call(tmp_path))
    assert got.ending is Ending.NO_RESULT
    assert got.is_error
    assert got.structured is None
    assert got.exit_code == 0


def test_イベントを流さず1で終わると_存在しないUUIDのresumeの形_NO_RESULTでinitializedなし(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script([], exit=1, stderr="Error: session not found")
    got = runtime(copilot).run(make_call(tmp_path, resume=True))
    assert got.ending is Ending.NO_RESULT
    assert not got.initialized
    assert got.structured is None
    assert got.exit_code == 1
    assert not got.rate_limited


def test_run_dirが無いAgentCallはValueErrorでcopilotを起こさない(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps())
    with pytest.raises(ValueError):
        runtime(copilot).start(make_call(tmp_path, run_dir=None))
    assert fake.started() == []


def test_新規の起動のプロンプトにsystem_appendとスキーマの本文とcall_promptが入る(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps())
    runtime(copilot).run(make_call(tmp_path))
    (seen,) = fake.started()
    argv = seen["argv"]
    assert "--resume" not in argv
    assert argv[argv.index("--session-id") + 1] == str(SESSION)
    assert "必須ルール" in seen["prompt"]
    assert "やること" in seen["prompt"]
    assert SCHEMA in json_objects(seen["prompt"])


@pytest.mark.parametrize("resume", [False, True], ids=["新規", "再開"])
def test_プロンプトは返し方の決まりとスキーマで終わる(
    copilot: str, fake: Fake, tmp_path: Path, resume: bool
):
    fake.script(success_steps())
    runtime(copilot).run(make_call(tmp_path, resume=resume))
    (seen,) = fake.started()
    prompt: str = seen["prompt"]
    rule = prompt.index(COPILOT_RETURN_RULE)
    assert prompt.index("やること") < rule
    assert json.loads(prompt[rule + len(COPILOT_RETURN_RULE) :]) == SCHEMA


def test_スキーマが無ければプロンプトに返し方の決まりを入れない(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps())
    runtime(copilot).run(make_call(tmp_path, json_schema=None))
    (seen,) = fake.started()
    assert COPILOT_RETURN_RULE not in seen["prompt"]
    assert seen["prompt"].endswith("やること")


def test_再開の起動のプロンプトにsystem_appendは入らずcall_promptが入る(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps())
    runtime(copilot).run(make_call(tmp_path, resume=True, prompt="続きをやる"))
    (seen,) = fake.started()
    argv = seen["argv"]
    assert argv[argv.index("--resume") + 1] == str(SESSION)
    assert "--session-id" not in argv
    assert "必須ルール" not in seen["prompt"]
    assert "続きをやる" in seen["prompt"]


# --- COPILOT_HOME と環境変数 ---


def test_起動した偽のcopilotは呼び出し元のCOPILOT_HOMEを受け取りラン専用のhomeは作らない(
    copilot: str, fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    caller = tmp_path / "caller-home"
    fake.login()
    (caller).mkdir()
    (caller / "config.json").write_text(json.dumps({**AUTH, **PERSONAL}), encoding="utf-8")
    monkeypatch.setenv("COPILOT_HOME", str(caller))
    fake.script(success_steps())
    call = make_call(tmp_path)
    runtime(copilot).run(call)
    (seen,) = fake.started()
    assert seen["env"]["COPILOT_HOME"] == str(caller)
    assert seen["home_config"] == {**AUTH, **PERSONAL}
    assert not (tmp_path / "run" / "sessions" / "copilot" / "home").exists()


def test_1回目と2回目の起動は呼び出し元と同じCOPILOT_HOMEを使う(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps())
    runtime(copilot).run(make_call(tmp_path))
    runtime(copilot).run(make_call(tmp_path, resume=True, prompt="続き"))
    first, second = fake.started()
    assert first["env"]["COPILOT_HOME"] == second["env"]["COPILOT_HOME"] == str(fake.source)


def test_親の環境の外部APIの設定とGitHubのトークンは偽のcopilotにそのまま渡る(
    copilot: str, fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    caller = {
        "COPILOT_PROVIDER_API_KEY": "secret",
        "COPILOT_PROVIDER_BASE_URL": "https://example.invalid",
        "COPILOT_MODEL": "auto",
        "GH_TOKEN": "real-token",
        "COPILOT_GITHUB_TOKEN": "cp-token",
        "GH_CONFIG_DIR": "/caller/gh",
    }
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    for name, value in caller.items():
        monkeypatch.setenv(name, value)
    fake.script(success_steps())
    call = make_call(tmp_path)
    runtime(copilot).run(call)
    (seen,) = fake.started()
    for name, value in caller.items():
        assert seen["env"][name] == value
    assert "GIT_TERMINAL_PROMPT" not in seen["env"]
    assert seen["env"]["AUTODEV_HOOK_RECORD"] == f"{call.log_path}.hooks"


def test_callのenvは偽のcopilotに渡る(copilot: str, fake: Fake, tmp_path: Path):
    fake.script(success_steps())
    runtime(copilot).run(make_call(tmp_path, env={"AUTODEV_GUARD": '{"guard": 1}'}))
    (seen,) = fake.started()
    assert seen["env"]["AUTODEV_GUARD"] == '{"guard": 1}'


# --- interrupt と時間切れ ---


def test_interruptはSIGINTで止めabortで0で終わった走りは理由を残して結果は読まない(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script([emit(session_start()), emit(message('{"ok": true}')), {"hang": True}])
    call = make_call(tmp_path)
    process = runtime(copilot).start(call)
    wait_until(lambda: log_has(call, "assistant.message"), "メッセージを受けた")
    process.interrupt("止める")
    got = process.wait()
    assert fake.got_sigint()
    assert got.ending is Ending.RESULT
    assert got.is_error
    assert got.interrupted == "止める"
    assert got.initialized
    assert got.structured is None
    assert got.exit_code == 0


def test_SIGINTを無視するcopilotはinterrupt_graceの後にkillされる(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(hanging_steps(), sigint="ignore")
    call = make_call(tmp_path)
    process = runtime(copilot, interrupt_grace=0.5).start(call)
    wait_until(lambda: log_has(call, "session.start"), "session.start を受けた")
    process.interrupt("止める")
    got = process.wait()
    assert got.ending is Ending.KILLED
    assert got.interrupted == "止める"


def test_timeoutを過ぎると黙り続ける偽のcopilotは約1秒でSIGINTを受けて理由に制限時間が入る(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(hanging_steps())
    started = time.monotonic()
    got = runtime(copilot).run(make_call(tmp_path, timeout=1))
    elapsed = time.monotonic() - started
    assert fake.got_sigint()
    assert got.interrupted is not None
    assert "制限時間" in got.interrupted
    assert elapsed < 8
    assert got.is_error


# --- 利用枠の上限 ---


def test_rate_limitのsession_errorを流して待つ偽のcopilotはSIGINTで止めrate_limitedで文面を残す(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(
        [
            emit(session_start()),
            emit(
                session_error(
                    "rate_limit", "user_weekly_rate_limited", RATE_LIMIT_MESSAGE, status=429
                )
            ),
            {"hang": True},
        ]
    )
    started = time.monotonic()
    got = runtime(copilot).run(make_call(tmp_path, timeout=30))
    assert time.monotonic() - started < 15
    assert fake.got_sigint()
    assert got.rate_limited
    assert RATE_LIMIT_MESSAGE in got.text
    assert got.structured is None
    # 時間切れではなく、上限のイベントで止めた
    assert got.interrupted is not None
    assert "制限時間" not in got.interrupted


def test_quotaのsession_errorでresult無しに終わると文面がlimit_textに当たらなくてもrate_limited(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(
        [
            emit(session_start()),
            emit(session_error("quota", "quota_exceeded", QUOTA_MESSAGE)),
        ],
        exit=1,
    )
    got = runtime(copilot).run(make_call(tmp_path))
    assert got.rate_limited
    assert got.is_error
    assert got.structured is None
    assert QUOTA_MESSAGE in got.text


def test_auto_mode_switchの承認待ちで止まった偽のcopilotはSIGINTで止めrate_limited(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script([emit(session_start()), emit(auto_mode_switch()), {"hang": True}])
    started = time.monotonic()
    got = runtime(copilot).run(make_call(tmp_path, timeout=30))
    assert time.monotonic() - started < 15
    assert fake.got_sigint()
    assert got.rate_limited
    assert got.interrupted is not None
    assert "制限時間" not in got.interrupted


@pytest.mark.parametrize("error_type", ["context_limit", "query"])
def test_上限ではないsession_errorはrate_limitedにしない(
    copilot: str, fake: Fake, tmp_path: Path, error_type: str
):
    fake.script(
        [emit(session_start()), emit(session_error(error_type, "x", "コンテキストが大きすぎる"))],
        exit=1,
    )
    got = runtime(copilot).run(make_call(tmp_path))
    assert got.is_error
    assert not got.rate_limited


def test_正常にresultで終わったときは本文にrate_limitとあってもrate_limitedにしない(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps('{"ok": true} rate limit の説明をした'))
    got = runtime(copilot).run(make_call(tmp_path))
    assert not got.is_error
    assert not got.rate_limited


def test_既知のイベントが無くてもresult無しで終わり標準エラーにrate_limitとあればrate_limited(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script([emit(session_start())], exit=1, stderr="Error: rate limit exceeded")
    got = runtime(copilot).run(make_call(tmp_path))
    assert got.is_error
    assert got.rate_limited


def test_標準エラーが上限の文面でなければrate_limitedにしない(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script([emit(session_start())], exit=1, stderr="Error: network unreachable")
    got = runtime(copilot).run(make_call(tmp_path))
    assert got.is_error
    assert not got.rate_limited


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (session_error("rate_limit", "rate_limited", "x"), True),
        (session_error("quota", "quota_exceeded", "x"), True),
        (auto_mode_switch(), True),
        (session_error("authentication", "x", "x"), False),
        (session_error("context_limit", "x", "x"), False),
        (message("rate limit"), False),
        (result(), False),
    ],
    ids=[
        "rate_limit",
        "quota",
        "auto_mode_switch",
        "authentication",
        "context_limit",
        "assistant.message",
        "result",
    ],
)
def test_limit_eventは上限を知らせるイベントだけが真(event: dict, expected: bool):
    assert limit_event(event) is expected


# --- フックの記録 ---


@pytest.mark.parametrize("tool", ["bash", "create", "edit", "apply_patch"])
def test_フックが走るツールを呼んだのに今回の記録が0件ならエラーで構造化の結果は捨てる(
    copilot: str, fake: Fake, tmp_path: Path, tool: str
):
    fake.script(
        [
            emit(session_start()),
            emit(tool_start(tool)),
            emit(tool_complete()),
            emit(message('{"ok": true}')),
            emit(result()),
        ]
    )
    got = runtime(copilot).run(make_call(tmp_path))
    assert got.is_error
    assert got.structured is None


def test_フックが走らないツールだけなら記録が0件でもエラーにしない(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(
        [
            emit(session_start()),
            emit(tool_start("report_intent")),
            emit(message('{"ok": true}')),
            emit(result()),
        ]
    )
    got = runtime(copilot).run(make_call(tmp_path))
    assert not got.is_error
    assert got.structured == {"ok": True}


def test_ツールを呼んで今回の記録が1件でもあればエラーにしない(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(
        [
            emit(session_start()),
            emit(tool_start()),
            hook(passed_record()),
            emit(tool_complete()),
            emit(message('{"ok": true}')),
            emit(result()),
        ]
    )
    got = runtime(copilot).run(make_call(tmp_path))
    assert not got.is_error
    assert got.structured == {"ok": True}


def test_起動前の記録だけでこの走りに記録が無ければ記録0件のエラーになる(
    copilot: str, fake: Fake, tmp_path: Path
):
    call = make_call(tmp_path)
    seed_records(call, passed_record(), passed_record())
    fake.script(
        [
            emit(session_start()),
            emit(tool_start()),
            emit(message('{"ok": true}')),
            emit(result()),
        ]
    )
    got = runtime(copilot).run(call)
    assert got.is_error
    assert got.structured is None


def test_走っている間に記録へaskが足されたらSIGINTで止めdeferredを返しaskは数えない(
    copilot: str, fake: Fake, tmp_path: Path
):
    fake.script(
        [
            emit(session_start()),
            emit(tool_start()),
            hook(ask_record("T")),
            {"hang": True},
        ]
    )
    started = time.monotonic()
    got = runtime(copilot).run(make_call(tmp_path, timeout=30))
    assert time.monotonic() - started < 15
    assert fake.got_sigint()
    assert got.deferred == DeferredToolUse("T", "Bash", {"command": ASK_COMMAND})
    assert got.hook_denials == 0
    assert got.interrupted is not None
    assert got.initialized
    assert not got.rate_limited


def test_prompt無しの再開は届いた回答と呼び直さない一言をプロンプトに入れて続ける(
    copilot: str, fake: Fake, tmp_path: Path
):
    call = make_call(tmp_path, resume=True, prompt=None)
    seed_records(call, ask_record("T"))
    answers = Path(str(call.run_dir)) / "answers"
    answers.mkdir(parents=True)
    (answers / "T.json").write_text(json.dumps({"answer": "300 秒"}), encoding="utf-8")
    fake.script(
        [
            emit(session_start()),
            emit(tool_start()),
            hook(passed_record()),
            emit(message('{"ok": true}')),
            emit(result()),
        ]
    )
    got = runtime(copilot).run(call)
    (seen,) = fake.started()
    argv = seen["argv"]
    assert argv[argv.index("--resume") + 1] == str(SESSION)
    assert "300 秒" in seen["prompt"]
    assert "TTL は?" in seen["prompt"]
    assert ASK_ANSWERED_RULE in seen["prompt"]
    # 前の走りの ask では止めず、deferred も埋めない
    assert got.deferred is None
    assert not got.is_error
    assert not fake.got_sigint()


def test_起動前からある記録は数えずこの走りのdeniedだけがhook_denialsになる(
    copilot: str, fake: Fake, tmp_path: Path
):
    call = make_call(tmp_path)
    seed_records(call, denied_record(), denied_record(), denied_record(), ask_record("T0"))
    fake.script(
        [
            emit(session_start()),
            emit(tool_start()),
            hook(denied_record()),
            hook(denied_record()),
            emit(message('{"ok": true}')),
            emit(result()),
        ]
    )
    seen: list[Progress] = []
    got = runtime(copilot).run(call, seen.append)
    assert got.hook_denials == 2
    assert seen[-1].hook_denials == 2
    assert got.deferred is None
    assert not got.is_error
    assert not fake.got_sigint()


# --- missing ---


def test_missingはcopilot_versionが落ちれば起動できない旨の1件を返す(copilot: str, fake: Fake):
    fake.script([], version=1)
    found = runtime(copilot).missing()
    assert len(found) == 1
    assert "copilot" in found[0]
    assert "起動" in found[0]


def test_missingは認証の項目が無ければログインしていない旨の1件を返す(copilot: str, fake: Fake):
    fake.script([])
    fake.login(json.dumps({"model": "x"}))
    found = runtime(copilot).missing()
    assert len(found) == 1
    assert "ログイン" in found[0]


def test_missingはconfig_jsonが無くてもログインしていない旨の1件を返す(copilot: str, fake: Fake):
    fake.script([])
    (fake.source / "config.json").unlink()
    found = runtime(copilot).missing()
    assert len(found) == 1
    assert "ログイン" in found[0]


@pytest.mark.parametrize(
    "config",
    [
        '{"authTokens": {"github.com:u": "t"}}',
        '{"loggedInUsers": [{"login": "u"}]}',
        '{"authTokens": {}, "loggedInUsers": [{"login": "u"}]}',
    ],
)
def test_missingは認証の項目のどちらかが空でなければ空(copilot: str, fake: Fake, config: str):
    fake.script([])
    fake.login(config)
    assert runtime(copilot).missing() == []


def test_missingは認証の項目がどちらも空ならログインしていない旨の1件を返す(
    copilot: str, fake: Fake
):
    fake.script([])
    fake.login(json.dumps({"authTokens": {}, "loggedInUsers": []}))
    found = runtime(copilot).missing()
    assert len(found) == 1
    assert "ログイン" in found[0]


def test_missingはCOPILOT_HOMEが無ければホームの_copilotを見る(
    copilot: str, fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake.script([])
    monkeypatch.delenv("COPILOT_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    assert source_home(os.environ) == tmp_path / "user" / ".copilot"
    assert len(runtime(copilot).missing()) == 1
    (tmp_path / "user" / ".copilot").mkdir(parents=True)
    (tmp_path / "user" / ".copilot" / "config.json").write_text(json.dumps(AUTH), encoding="utf-8")
    assert runtime(copilot).missing() == []


def test_missingは起動できてログイン済みなら空(copilot: str, fake: Fake):
    fake.script([])
    assert runtime(copilot).missing() == []
    # コメント付きの config.json でも読める
    fake.login('// copilot の設定\n{"loggedInUsers": [{"login": "u"}]}\n')
    assert runtime(copilot).missing() == []
