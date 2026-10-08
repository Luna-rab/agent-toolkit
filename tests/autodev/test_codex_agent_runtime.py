"""CodexRuntime: 偽の codex（`fake_codex.py`）を起動して、JSONL の読み方と起動の仕方を確かめる。"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from executor_fakes import Env, make_env
from test_executor import ex, impl_task, reports

from autodev.adapters.agent.types import (
    AgentCall,
    AgentOutcome,
    DeferredToolUse,
    Ending,
    Progress,
)
from autodev.adapters.codex.agent_runtime import (
    CodexRuntime,
    argv_for,
    env_for,
    thread_path,
)
from autodev.adapters.codex.hook import (
    ASK_ANSWERED_RULE,
    HOOK_RECORD_ENV,
    PARK_ON_ASK,
    HookRecord,
)
from autodev.adapters.codex.schema import strict_schema
from autodev.domain.value_objects.model_class import AgentKind
from autodev.domain.value_objects.session_id import SessionId
from autodev.domain.value_objects.stage_exit import StageExit
from autodev.domain.value_objects.stage_kind import StageKind

FAKE = Path(__file__).with_name("fake_codex.py")
SESSION = SessionId("0b6f3c1e-9a8d-4c2b-8e7f-1a2b3c4d5e6f")
SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "note": {"type": "string"},
    },
    "required": ["findings"],
    "additionalProperties": False,
}
ASK_COMMAND = "autodev ask --question 'TTL は?'"
HOOKS_GUIDE = "/hooks"


@pytest.fixture
def codex(tmp_path: Path) -> str:
    wrapper = tmp_path / "codex"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return str(wrapper)


# --- 偽の codex が流す JSONL ---


def thread_started(thread_id: str = "T1") -> dict:
    return {"type": "thread.started", "thread_id": thread_id}


def turn_started() -> dict:
    return {"type": "turn.started"}


def message(text: str) -> dict:
    return {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "agent_message", "text": text},
    }


def command_item(kind: str = "item.completed") -> dict:
    return {
        "type": kind,
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "ls",
            "status": "completed",
        },
    }


def turn_completed() -> dict:
    return {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 10,
            "cached_input_tokens": 4,
            "output_tokens": 5,
            "reasoning_output_tokens": 2,
        },
    }


def turn_failed(text: str) -> dict:
    return {"type": "turn.failed", "error": {"message": text}}


def emit(event: dict) -> dict:
    return {"emit": event}


def hook(record: HookRecord) -> dict:
    return {"hook": asdict(record)}


def success_steps(text: str = '{"findings": []}') -> list[dict]:
    return [
        emit(thread_started()),
        emit(turn_started()),
        emit(message(text)),
        emit(turn_completed()),
    ]


def hanging_steps() -> list[dict]:
    return [emit(thread_started()), emit(turn_started()), {"hang": True}]


def ask_record(tool_use_id: str = "call_1") -> HookRecord:
    return HookRecord(PARK_ON_ASK, "Bash", True, tool_use_id, "TTL は?", ASK_COMMAND)


def denied_record() -> HookRecord:
    return HookRecord("deny-writes", "Bash", True)


def passed_record() -> HookRecord:
    return HookRecord("deny-writes", "Bash", False)


# --- 起動の道具 ---


class Fake:
    """偽の codex の台本と記録。"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.record = tmp_path / "record.jsonl"
        self.signals = tmp_path / "signals.txt"
        monkeypatch.setenv("FAKE_CODEX_RECORD", str(self.record))
        monkeypatch.setenv("FAKE_CODEX_SIGNALS", str(self.signals))

    def script(self, steps: list[dict], **extra: Any) -> None:
        path = self.tmp_path / "script.json"
        path.write_text(json.dumps({"steps": steps, **extra}, ensure_ascii=False), encoding="utf-8")
        self.monkeypatch.setenv("FAKE_CODEX_SCRIPT", str(path))

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
        "agent": AgentKind.CODEX,
        "run_dir": str(tmp_path / "run"),
        "system_append": "必須ルール",
    }
    values.update(fields)
    return AgentCall(**values)


def runtime(codex: str, **kwargs: Any) -> CodexRuntime:
    return CodexRuntime(codex, progress_interval=0.0, **kwargs)


def hooks_of(call: AgentCall) -> Path:
    return Path(f"{call.log_path}.hooks")


def seed_records(call: AgentCall, *records: HookRecord) -> None:
    path = hooks_of(call)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in records))


def seed_thread(call: AgentCall, thread_id: str = "T1") -> None:
    path = thread_path(str(call.run_dir), call.session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(thread_id, encoding="utf-8")


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


# --- argv ---


def test_始めるときのargvはresumeを含まずサンドボックスとスキーマとモデルとeffortを渡す():
    argv = argv_for(make_call(Path("/w")), "/tmp/schema.json", None)
    assert argv[:3] == ["codex", "exec", "--json"]
    assert argv[-1] == "-"
    assert "resume" not in argv
    assert argv[argv.index("-s") + 1] == "danger-full-access"
    assert argv[argv.index("--output-schema") + 1] == "/tmp/schema.json"
    assert argv[argv.index("-m") + 1] == "gpt-5.5"
    assert "model_reasoning_effort=high" in [
        argv[i + 1] for i, word in enumerate(argv) if word == "-c"
    ]


def test_続けるときのargvはresumeにthread_idを渡しサンドボックスは_cで渡す():
    call = make_call(Path("/w"), resume=True)
    argv = argv_for(call, "/tmp/schema.json", "T1", "mycodex")
    assert argv[:3] == ["mycodex", "exec", "resume"]
    assert argv[-2:] == ["T1", "-"]
    assert argv[argv.index("-m") + 1] == "gpt-5.5"
    assert argv[argv.index("--output-schema") + 1] == "/tmp/schema.json"
    configs = [argv[i + 1] for i, word in enumerate(argv) if word == "-c"]
    assert "model_reasoning_effort=high" in configs
    assert 'sandbox_mode="danger-full-access"' in configs
    # codex-cli 0.160.1 の `exec resume` は -s・--sandbox を受けず、引数の誤りで落ちる
    assert "-s" not in argv
    assert "--sandbox" not in argv


def test_thread_pathはランディレクトリのsessions_codexの下にsession_idで置く(tmp_path: Path):
    assert thread_path(tmp_path, SESSION) == tmp_path / "sessions" / "codex" / str(SESSION)


# --- 起動と結果 ---


def test_偽のcodexが流したJSONLからAgentOutcomeを組みthread_idを控える(
    codex: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps())
    call = make_call(tmp_path)
    got = runtime(codex).run(call)
    assert got.ending is Ending.RESULT
    assert got.exit_code == 0
    assert not got.is_error
    assert got.structured == {"findings": []}
    assert got.usage.input_tokens == 10
    assert got.usage.cache_read_input_tokens == 4
    assert got.usage.output_tokens == 5
    assert got.initialized
    assert got.num_turns == 1
    assert got.cost_usd == 0.0
    assert got.interrupted is None
    assert got.deferred is None
    assert got.hook_denials == 0
    assert not got.rate_limited
    assert thread_path(str(call.run_dir), SESSION).read_text(encoding="utf-8").strip() == "T1"


def test_agent_messageが2つ流れたらstructuredは最後のものから読む(
    codex: str, fake: Fake, tmp_path: Path
):
    fake.script(
        [
            emit(thread_started()),
            emit(turn_started()),
            emit(message('{"findings": ["前置き"]}')),
            emit(message('{"findings": []}')),
            emit(turn_completed()),
        ]
    )
    got = runtime(codex).run(make_call(tmp_path))
    assert got.structured == {"findings": []}
    assert got.text == '{"findings": []}'


def test_スキーマで省ける欄のnullは消して返す(codex: str, fake: Fake, tmp_path: Path):
    fake.script(success_steps('{"findings": [], "note": null}'))
    got = runtime(codex).run(make_call(tmp_path))
    assert got.structured == {"findings": []}


def test_結果がJSONのobjectでなければstructuredはNone(codex: str, fake: Fake, tmp_path: Path):
    fake.script(success_steps("終わりました"))
    got = runtime(codex).run(make_call(tmp_path))
    assert got.structured is None
    assert not got.is_error


def test_resumeで控えが無ければcodexを起こさずinitializedなしで返す(
    codex: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps())
    got = runtime(codex).run(make_call(tmp_path, resume=True))
    assert got.ending is Ending.NO_RESULT
    assert not got.initialized
    assert got.structured is None
    assert fake.started() == []


def test_run_dirが無いAgentCallはValueErrorでcodexを起こさない(
    codex: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps())
    with pytest.raises(ValueError):
        runtime(codex).start(make_call(tmp_path, run_dir=None))
    assert fake.started() == []


def test_始めるときは実際にresumeなしのargvで起こし標準入力にsystem_appendと本文を渡す(
    codex: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps())
    runtime(codex).run(make_call(tmp_path))
    (seen,) = fake.started()
    argv = seen["argv"]
    assert argv[:2] == ["exec", "--json"]
    assert argv[-1] == "-"
    assert "resume" not in argv
    assert "必須ルール" in seen["stdin"]
    assert "やること" in seen["stdin"]


def test_続けるときは控えたthread_idでresumeを起こし本文を標準入力に渡す(
    codex: str, fake: Fake, tmp_path: Path
):
    fake.script(success_steps())
    call = make_call(tmp_path, resume=True, prompt="続きをやる")
    seed_thread(call, "T9")
    runtime(codex).run(call)
    (seen,) = fake.started()
    assert seen["argv"][:2] == ["exec", "resume"]
    assert seen["argv"][-2:] == ["T9", "-"]
    assert "続きをやる" in seen["stdin"]


def test_output_schemaのファイルの中身はstrict_schemaの結果(codex: str, fake: Fake, tmp_path: Path):
    fake.script(success_steps())
    runtime(codex).run(make_call(tmp_path))
    (seen,) = fake.started()
    assert seen["schema"] == strict_schema(SCHEMA)


CALLER_ENV = {
    "OPENAI_API_KEY": "k",
    "CODEX_API_KEY": "c",
    "GH_TOKEN": "gh-real",
    "GITHUB_TOKEN": "ghs-real",
    "GH_CONFIG_DIR": "/caller/gh",
}


def test_env_forは呼び出し元の環境変数をそのまま渡しフックの記録の場所を足す(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for name, value in CALLER_ENV.items():
        monkeypatch.setenv(name, value)
    call = make_call(tmp_path)
    env = env_for(call)
    for name, value in CALLER_ENV.items():
        assert env[name] == value
    assert env[HOOK_RECORD_ENV] == f"{call.log_path}.hooks"


def test_起動した子のプロセスは呼び出し元と同じ環境変数を受け取る(
    codex: str, fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    for name, value in CALLER_ENV.items():
        monkeypatch.setenv(name, value)
    fake.script(success_steps())
    call = make_call(tmp_path)
    runtime(codex).run(call)
    (seen,) = fake.started()
    for name, value in CALLER_ENV.items():
        assert seen["env"][name] == value
    assert "GIT_TERMINAL_PROMPT" not in seen["env"]
    assert seen["env"][HOOK_RECORD_ENV] == f"{call.log_path}.hooks"


def test_callのenvは偽のcodexに渡る(codex: str, fake: Fake, tmp_path: Path):
    fake.script(success_steps())
    runtime(codex).run(make_call(tmp_path, env={"AUTODEV_GUARD": '{"guard": 1}'}))
    (seen,) = fake.started()
    assert seen["env"]["AUTODEV_GUARD"] == '{"guard": 1}'


# --- 利用枠の上限 ---


@pytest.mark.parametrize(
    ("text", "limited"),
    [("You've hit your usage limit", True), ("stream disconnected", False)],
    ids=["上限", "一時的な切断"],
)
def test_turn_failedのerror_messageが利用枠の上限なら見分ける(
    codex: str, fake: Fake, tmp_path: Path, text: str, limited: bool
):
    fake.script([emit(thread_started()), emit(turn_started()), emit(turn_failed(text))], exit=1)
    got = runtime(codex).run(make_call(tmp_path))
    assert got.is_error
    assert got.rate_limited is limited
    assert got.text == text
    assert got.structured is None


# --- interrupt ---


def test_interruptはSIGINTで止め理由を残し結果は読まない(codex: str, fake: Fake, tmp_path: Path):
    fake.script(hanging_steps())
    call = make_call(tmp_path)
    process = runtime(codex).start(call)
    wait_until(lambda: log_has(call, "thread.started"), "thread.started を受けた")
    process.interrupt("止める")
    got = process.wait()
    assert fake.got_sigint()
    assert got.ending is Ending.RESULT
    assert got.is_error
    assert got.interrupted == "止める"
    assert got.initialized
    assert got.structured is None
    assert got.exit_code == 1


def test_SIGINTを無視するcodexはinterrupt_graceの後にkillされる(
    codex: str, fake: Fake, tmp_path: Path
):
    fake.script(hanging_steps(), sigint="ignore")
    call = make_call(tmp_path)
    process = runtime(codex, interrupt_grace=0.5).start(call)
    wait_until(lambda: log_has(call, "thread.started"), "thread.started を受けた")
    process.interrupt("止める")
    got = process.wait()
    assert got.ending is Ending.KILLED
    assert got.interrupted == "止める"


def test_timeoutで止めた走りの次にresume_Trueで呼ぶと控えたthread_idでresumeする(
    codex: str, fake: Fake, tmp_path: Path
):
    fake.script(hanging_steps())
    first = make_call(tmp_path, timeout=3)
    got = runtime(codex).run(first)
    assert got.ending is Ending.RESULT
    assert got.initialized
    assert got.interrupted is not None
    assert thread_path(str(first.run_dir), SESSION).read_text(encoding="utf-8").strip() == "T1"

    fake.script(success_steps())
    runtime(codex).run(make_call(tmp_path, resume=True, prompt="続き"))
    seen = fake.started()[-1]
    assert seen["argv"][:2] == ["exec", "resume"]
    assert seen["argv"][-2:] == ["T1", "-"]


# --- フックの記録 ---


def test_ツールを呼んだのに記録が0件ならフックの信頼を案内するエラーで返す(
    codex: str, fake: Fake, tmp_path: Path
):
    fake.script(
        [
            emit(thread_started()),
            emit(turn_started()),
            emit(command_item("item.started")),
            emit(command_item()),
            emit(message('{"findings": []}')),
            emit(turn_completed()),
        ]
    )
    got = runtime(codex).run(make_call(tmp_path))
    assert got.is_error
    assert HOOKS_GUIDE in got.text


def test_ツールを呼ばなければ記録が0件でもエラーにしない(codex: str, fake: Fake, tmp_path: Path):
    fake.script(success_steps())
    got = runtime(codex).run(make_call(tmp_path))
    assert not got.is_error


def test_ツールを呼んで記録が1件でもあればエラーにしない(codex: str, fake: Fake, tmp_path: Path):
    fake.script(
        [
            emit(thread_started()),
            emit(turn_started()),
            emit(command_item()),
            hook(passed_record()),
            emit(message('{"findings": []}')),
            emit(turn_completed()),
        ]
    )
    got = runtime(codex).run(make_call(tmp_path))
    assert not got.is_error
    assert got.structured == {"findings": []}


def test_記録が0件のエラーは実行器の証拠でStageExit_ERRORになり同じ文が入る(
    codex: str, fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake.script(
        [
            emit(thread_started()),
            emit(turn_started()),
            emit(command_item()),
            emit(message('{"findings": []}')),
            emit(turn_completed()),
        ]
    )
    (tmp_path / "exec").mkdir()
    env: Env = make_env(tmp_path / "exec", monkeypatch)
    impl_task(env)
    env.begin(ex(StageKind.IMPL))
    outcomes: list[AgentOutcome] = []

    def behave(call: AgentCall, process: object) -> AgentOutcome:
        got = runtime(codex).run(call)
        outcomes.append(got)
        return got

    env.runtime.behaviors.append(behave)
    env.executor.run(ex(StageKind.IMPL), env.world.inbox.expect(ex(StageKind.IMPL)))
    env.executor.join()
    (report,) = reports(env)
    assert report.evidence.exit is StageExit.ERROR
    assert HOOKS_GUIDE in (report.evidence.error or "")
    assert outcomes[0].text in (report.evidence.error or "")


def test_この走りの記録のdeniedだけがhook_denialsでaskは数えずSIGINTで止めて返す(
    codex: str, fake: Fake, tmp_path: Path
):
    fake.script(
        [
            emit(thread_started()),
            emit(turn_started()),
            emit(command_item()),
            hook(denied_record()),
            hook(denied_record()),
            hook(ask_record()),
            {"hang": True},
        ]
    )
    seen: list[Progress] = []
    got = runtime(codex).run(make_call(tmp_path, timeout=20), seen.append)
    assert fake.got_sigint()
    assert got.hook_denials == 2
    assert seen[-1].hook_denials == 2
    assert got.interrupted is not None
    assert got.ending is Ending.RESULT


def test_走っている間に記録へaskが足されたらSIGINTで止めdeferredを返す(
    codex: str, fake: Fake, tmp_path: Path
):
    fake.script(
        [
            emit(thread_started()),
            emit(turn_started()),
            emit(command_item()),
            hook(ask_record("call_1")),
            {"hang": True},
        ]
    )
    got = runtime(codex).run(make_call(tmp_path, timeout=20))
    assert fake.got_sigint()
    assert got.deferred == DeferredToolUse("call_1", "Bash", {"command": ASK_COMMAND})
    assert got.interrupted is not None
    assert got.initialized


def test_prompt無しの再開は届いた回答と呼び直さない一言をプロンプトに入れて続ける(
    codex: str, fake: Fake, tmp_path: Path
):
    call = make_call(tmp_path, resume=True, prompt=None)
    seed_thread(call, "T1")
    seed_records(call, ask_record("call_1"))
    answers = Path(str(call.run_dir)) / "answers"
    answers.mkdir(parents=True)
    (answers / "call_1.json").write_text(json.dumps({"answer": "300 秒"}), encoding="utf-8")
    fake.script(
        [
            emit(thread_started()),
            emit(turn_started()),
            emit(command_item()),
            hook(passed_record()),
            emit(message('{"findings": []}')),
            emit(turn_completed()),
        ]
    )
    got = runtime(codex).run(call)
    (seen,) = fake.started()
    assert seen["argv"][:2] == ["exec", "resume"]
    assert seen["argv"][-2:] == ["T1", "-"]
    assert "300 秒" in seen["stdin"]
    assert "TTL は?" in seen["stdin"]
    assert ASK_ANSWERED_RULE in seen["stdin"]
    # 前の走りの ask では止めず、deferred も埋めない
    assert got.deferred is None
    assert not got.is_error
    assert not fake.got_sigint()


def test_起動前からある記録は数えず記録0件の確かめにも使わない(
    codex: str, fake: Fake, tmp_path: Path
):
    call = make_call(tmp_path)
    seed_records(call, denied_record(), denied_record(), denied_record(), ask_record("call_0"))
    fake.script(
        [
            emit(thread_started()),
            emit(turn_started()),
            emit(command_item()),
            hook(passed_record()),
            emit(message('{"findings": []}')),
            emit(turn_completed()),
        ]
    )
    got = runtime(codex).run(call)
    assert got.hook_denials == 0
    assert got.deferred is None
    assert not got.is_error
    assert not fake.got_sigint()


def test_起動前の記録だけでこの走りに記録が無ければ記録0件のエラーになる(
    codex: str, fake: Fake, tmp_path: Path
):
    call = make_call(tmp_path)
    seed_records(call, passed_record(), passed_record())
    fake.script(
        [
            emit(thread_started()),
            emit(turn_started()),
            emit(command_item()),
            emit(message('{"findings": []}')),
            emit(turn_completed()),
        ]
    )
    got = runtime(codex).run(call)
    assert got.is_error
    assert HOOKS_GUIDE in got.text


def test_回答を返した記録だけが足されたらSIGINTで止めずdeferredもhook_denialsも空(
    codex: str, fake: Fake, tmp_path: Path
):
    answered = HookRecord(PARK_ON_ASK, "Bash", False, "call_2", answered_from="call_1")
    fake.script(
        [
            emit(thread_started()),
            emit(turn_started()),
            emit(command_item()),
            hook(answered),
            # 見張りが記録を読む時間を与える
            {"sleep": 1.0},
            emit(message('{"findings": []}')),
            emit(turn_completed()),
        ]
    )
    got = runtime(codex).run(make_call(tmp_path))
    assert not fake.got_sigint()
    assert got.deferred is None
    assert got.hook_denials == 0
    assert got.interrupted is None
    assert not got.is_error
    assert got.structured == {"findings": []}


def test_missingはloginが通らないと理由を返す(codex: str, fake: Fake):
    fake.script([], login=1)
    assert runtime(codex).missing()
    fake.script([], login=0)
    assert runtime(codex).missing() == []
