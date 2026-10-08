"""AgentRuntime: `claude -p` の起動・再開・中断・プロセスを止める。

結果は「起きた事実」（終わり方・result の中身・使ったトークンと費用・フックに拒まれた回数・defer で
止まったか・利用枠の上限に当たったか）として返す。完了・失敗・やり直しのどれにするかは決めない
（`Task.handle` が証拠から決める）。

起動のしかたで効く事実:

- 環境変数は呼び出し元のものをそのまま渡す（`ANTHROPIC_*`・`GH_TOKEN` なども消さず、書き換えない）。
  足すのは `call.env`（ガードの `AUTODEV_GUARD` など）だけ
- プロンプトは標準入力から渡す。`--allowedTools` は次のオプションまで後ろの引数を全部取るので、
  ツールの一覧はカンマでつないで 1 引数にする
- `--input-format stream-json` で起動して標準入力を開いたままにすると、走行中に制御要求
  （`interrupt`）を送れる。送る user メッセージには uuid を振る
- `system/init` はターンごとに出る。`cancel_queued` は init の `capabilities` に
  `interrupt_cancel_queued_v1` があるときだけ付ける
- result を見たら標準入力を閉じる。閉じても終わらないことがあるので、待つ時間を決めて kill する。
  interrupt を送っても result が返らないことがあるので、一定時間で kill する
- 引数の誤りは標準エラーにだけ出て、JSONL を 1 行も出さずに終わる
- `--json-schema` の検証に失敗しても `subtype: success` のまま `structured_output` が空で返ることが
  ある。ここでは空を空のまま返し、形を確かめるのは実行器である
- defer で止まったセッションを再開するときはプロンプトを渡さない。呼んだ側が
  `prompt=None` で表す
- JSONL は走りながら 1 行ずつログに書く。渡したプロンプトも残す。同じステージを 2 度呼ぶことが
  あるので、上書きせず書き足す
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

from ..agent.types import (
    INTERRUPT_GRACE,
    PROGRESS_INTERVAL,
    AgentCall,
    AgentOutcome,
    DeferredToolUse,
    Ending,
    Progress,
    Usage,
    limit_text,
)
from ..process import children
from ..process.command import run

_BASE_ARGV = (
    "-p",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
    "--permission-mode",
    "bypassPermissions",
)
#: result を見て標準入力を閉じてから、終わるのを待つ時間
EXIT_GRACE = 30.0
CANCEL_QUEUED = "interrupt_cancel_queued_v1"
#: 結果の返し方。契約とプロンプトはツール名を書かないので、Claude の実装がここで足す
CLAUDE_RETURN_RULE = (
    "終わりに結果を StructuredOutput ツールで 1 つだけ返す。形はそのツールの入力スキーマにある。"
)
#: フックに拒まれた呼び出しの tool_result の本文の頭（Claude Code 2.1.281 で確かめた形）
_HOOK_DENIAL = re.compile(r"PreToolUse:\S+ hook error")


def argv_for(call: AgentCall, claude: str = "claude") -> list[str]:
    argv = [claude, *_BASE_ARGV]
    if call.json_schema:
        argv += ["--json-schema", call.json_schema]
    if call.model:
        argv += ["--model", call.model]
    if call.effort:
        argv += ["--effort", call.effort]
    if call.max_turns:
        argv += ["--max-turns", str(call.max_turns)]
    if call.settings:
        argv += ["--settings", call.settings]
    append = "\n".join(part for part in (call.system_append, CLAUDE_RETURN_RULE) if part)
    argv += ["--append-system-prompt", append]
    if call.allowed_tools:
        argv += ["--allowedTools", ",".join(call.allowed_tools)]
    if call.disallowed_tools:
        argv += ["--disallowedTools", ",".join(call.disallowed_tools)]
    argv += ["--resume" if call.resume else "--session-id", str(call.session)]
    return argv


def env_for(call: AgentCall) -> dict[str, str]:
    return {**os.environ, **call.env}


def user_message(prompt: str) -> str:
    """stream-json の入力 1 行。uuid を振らないと interrupt の応答の `still_queued` / `cancelled` が空になる。"""
    message = {
        "type": "user",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
        "uuid": str(uuid.uuid4()),
    }
    return json.dumps(message, ensure_ascii=False) + "\n"


def interrupt_request(cancel_queued: bool) -> str:
    request: dict[str, Any] = {"subtype": "interrupt"}
    if cancel_queued:
        request["cancel_queued"] = True
    body = {"type": "control_request", "request_id": str(uuid.uuid4()), "request": request}
    return json.dumps(body, ensure_ascii=False) + "\n"


class AgentProcess:
    """走っている `claude -p` 1 つ。`wait` は 1 つのスレッドから呼ぶ。`interrupt`・`kill` は別の
    スレッド（進み具合を受ける関数の中も含む）から呼んでよい。"""

    def __init__(
        self,
        call: AgentCall,
        *,
        claude: str = "claude",
        on_progress: Callable[[Progress], None] | None = None,
        progress_interval: float = PROGRESS_INTERVAL,
        exit_grace: float = EXIT_GRACE,
        interrupt_grace: float = INTERRUPT_GRACE,
    ) -> None:
        self.call = call
        self._on_progress = on_progress
        self._progress_interval = progress_interval
        self._exit_grace = exit_grace
        self._interrupt_grace = interrupt_grace
        self._lock = threading.Lock()
        #: 標準入力へ書くのは、待つスレッドと interrupt を呼ぶスレッドの両方
        self._stdin_lock = threading.Lock()
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._state = _Collector()
        self._interrupted: str | None = None
        #: これを過ぎたら kill する（interrupt の後・result の後）
        self._kill_at: float | None = None
        self._killed = False
        self._deadline = time.monotonic() + call.timeout if call.timeout is not None else None

        Path(call.log_path).parent.mkdir(parents=True, exist_ok=True)
        self._log: IO[str] = open(call.log_path, "a", encoding="utf-8")  # noqa: SIM115
        self._err: IO[str] = open(f"{call.log_path}.err", "a", encoding="utf-8")  # noqa: SIM115
        # 書き足すので、この呼び出しの標準エラーはここから後ろである
        self._err_offset = self._err.tell()
        argv = argv_for(call, claude)
        # 再開ではプロンプトを渡さないので、最初の指示はログにしか残らない
        self._record(
            {
                "type": "autodev/call",
                "argv": argv,
                "cwd": call.cwd,
                "prompt": call.prompt,
                "system_append": call.system_append,
            }
        )
        try:
            self._proc = subprocess.Popen(
                argv,
                cwd=call.cwd,
                env=env_for(call),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._err,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # kill するとき、claude が起こしたツールのプロセスまでまとめて止める
                start_new_session=True,
            )
        except OSError:
            self._log.close()
            self._err.close()
            raise
        try:
            children.record(self._proc.pid, argv)
        except BaseException:
            children.stop_unwatched(self._proc)
            self._log.close()
            self._err.close()
            raise
        threading.Thread(target=self._pump, daemon=True).start()
        if call.prompt is None:
            self._close_stdin()
        else:
            self._write(user_message(call.prompt))

    # --- 外から ---

    def interrupt(self, reason: str) -> None:
        """ターンを打ち切る。result が返れば usage と停止理由が残る。返らなければ kill する。

        制御要求を送るのも kill の期限を決めるのも、最初の 1 回だけ。呼び直すたびに期限を延ばすと、
        interrupt が効かないときにいつまでも kill しない。
        """
        with self._lock:
            if self._interrupted is not None:
                return
            self._interrupted = reason
            if self._state.final is not None:
                return
            stdin = self._proc.stdin
            if stdin is None or stdin.closed:
                # 標準入力を閉じた後（再開で何も送らなかった）は制御要求を送れない
                self._kill_at = time.monotonic()
                return
            self._kill_at = time.monotonic() + self._interrupt_grace
            cancel = CANCEL_QUEUED in self._state.capabilities
        self._write(interrupt_request(cancel))

    def kill(self) -> None:
        with self._lock:
            self._killed = True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._proc.pid, signal.SIGKILL)

    # --- 待つ ---

    def wait(self) -> AgentOutcome:
        last_progress = 0.0
        last_denials = 0
        while True:
            now = time.monotonic()
            if self._deadline is not None and now >= self._deadline:
                self._deadline = None
                self.interrupt(f"制限時間を超えた（{self.call.timeout} 秒）")
            with self._lock:
                kill_at = self._kill_at
            if kill_at is not None and now >= kill_at:
                self.kill()
                with self._lock:
                    self._kill_at = None
            try:
                line = self._lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            self._log.write(line)
            self._log.flush()
            event = _event(line)
            if event is None:
                continue
            with self._lock:
                self._state.take(event)
                is_result = event.get("type") == "result"
                if is_result:
                    # result の後も終わらないことがあるので、待つ時間を決めて kill する
                    self._kill_at = time.monotonic() + self._exit_grace
            if is_result:
                self._close_stdin()
            now = time.monotonic()
            if (
                self._state.hook_denials != last_denials
                or now - last_progress >= self._progress_interval
            ):
                last_progress, last_denials = now, self._state.hook_denials
                self._notify()
        code = self._proc.wait()
        children.forget(self._proc.pid)
        self._notify()
        self._log.close()
        self._err.close()
        return self._state.outcome(
            call=self.call,
            exit_code=code,
            killed=self._killed,
            interrupted=self._interrupted,
            stderr=_tail(Path(f"{self.call.log_path}.err"), self._err_offset),
        )

    # --- 中 ---

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _write(self, payload: str) -> None:
        with self._stdin_lock:
            stdin = self._proc.stdin
            if stdin is None or stdin.closed:
                return
            with contextlib.suppress(BrokenPipeError, ValueError, OSError):
                stdin.write(payload)
                stdin.flush()

    def _close_stdin(self) -> None:
        with self._stdin_lock:
            stdin = self._proc.stdin
            if stdin is not None and not stdin.closed:
                with contextlib.suppress(BrokenPipeError, OSError):
                    stdin.close()

    def _record(self, payload: Mapping[str, Any]) -> None:
        self._log.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._log.flush()

    def _notify(self) -> None:
        if self._on_progress is None:
            return
        # 進み具合を受ける側の誤りで、ステージを止めない
        with contextlib.suppress(Exception):
            self._on_progress(self._state.progress())


class AgentRuntime:
    """`claude -p` を起こす。`claude` は差し替えられる（検査では偽のコマンドを置く）。"""

    def __init__(
        self,
        claude: str = "claude",
        *,
        progress_interval: float = PROGRESS_INTERVAL,
        exit_grace: float = EXIT_GRACE,
        interrupt_grace: float = INTERRUPT_GRACE,
    ) -> None:
        self.claude = claude
        self.progress_interval = progress_interval
        self.exit_grace = exit_grace
        self.interrupt_grace = interrupt_grace

    def start(
        self, call: AgentCall, on_progress: Callable[[Progress], None] | None = None
    ) -> AgentProcess:
        return AgentProcess(
            call,
            claude=self.claude,
            on_progress=on_progress,
            progress_interval=self.progress_interval,
            exit_grace=self.exit_grace,
            interrupt_grace=self.interrupt_grace,
        )

    def run(
        self, call: AgentCall, on_progress: Callable[[Progress], None] | None = None
    ) -> AgentOutcome:
        return self.start(call, on_progress).wait()

    def available(self) -> bool:
        """`claude` が起動できるか（走り出す前に確かめる）。"""
        return run([self.claude, "--version"]).ok


class _Collector:
    """流れてきたイベントから、事実を拾う。読むのは result 1 つでよい。"""

    def __init__(self) -> None:
        self.final: dict[str, Any] | None = None
        self.capabilities: tuple[str, ...] = ()
        self.initialized = False
        self.events = 0
        self.turns = 0
        self.last_tool: str | None = None
        self.hook_denials = 0
        self.rate_limit_rejected = False
        #: ステージ自身（`parent_tool_use_id` が None）の最後の assistant の `error` と本文。上限かは
        #: 終わり方と合わせて決める（印の後にふつうの応答が続いたなら、上限で終わったのではない）
        self.last_error: str | None = None
        self.last_error_text = ""
        self._messages: set[str] = set()

    def take(self, event: Mapping[str, Any]) -> None:
        self.events += 1
        kind = event.get("type")
        if kind == "result":
            self.final = dict(event)
        elif kind == "system" and event.get("subtype") == "init":
            # init はターンごとに出る。最後のものを使う
            self.capabilities = tuple(str(c) for c in event.get("capabilities") or ())
            self.initialized = True
        elif kind == "assistant":
            if event.get("parent_tool_use_id") is None:
                error = event.get("error")
                self.last_error = error if isinstance(error, str) else None
                self.last_error_text = _message_text(event) if self.last_error else ""
            self._assistant(event)
        elif kind == "user":
            self.hook_denials += _hook_denials(event)
        elif kind == "rate_limit_event":
            # `overageStatus` は見ない。従量の超過を組織で切っていると、ふつうの呼び出しでも
            # `rejected` になる（段 6 の実測）
            info = event.get("rate_limit_info")
            if isinstance(info, Mapping) and info.get("status") == "rejected":
                self.rate_limit_rejected = True

    def _ended_on_limit(self, status: int | None) -> bool:
        """最後の応答が `error: "rate_limit"` で、上限の文言か 429 と重なる。この印は 529 の過負荷や
        `model_blocked` にも付くので、印だけでは上限にしない。"""
        return self.last_error == "rate_limit" and (
            status == 429 or limit_text(self.last_error_text)
        )

    def _assistant(self, event: Mapping[str, Any]) -> None:
        message = event.get("message")
        if not isinstance(message, Mapping):
            return
        # 1 つのメッセージが内容ごとに分かれて届くので、id で数える
        identifier = str(message.get("id") or event.get("uuid") or self.events)
        if identifier not in self._messages:
            self._messages.add(identifier)
            self.turns += 1
        for block in message.get("content") or ():
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                self.last_tool = str(block.get("name") or "") or self.last_tool

    def progress(self) -> Progress:
        return Progress(self.turns, self.last_tool, self.hook_denials, self.events)

    def outcome(
        self,
        *,
        call: AgentCall,
        exit_code: int,
        killed: bool,
        interrupted: str | None,
        stderr: str,
    ) -> AgentOutcome:
        final = self.final
        if final is None:
            ending = Ending.KILLED if killed else Ending.NO_RESULT
            return AgentOutcome(
                ending=ending,
                exit_code=exit_code,
                session=call.session,
                hook_denials=self.hook_denials,
                rate_limited=self.rate_limit_rejected
                or self._ended_on_limit(None)
                or limit_text(stderr),
                interrupted=interrupted,
                stderr=stderr,
                capabilities=self.capabilities,
                log_path=call.log_path,
                initialized=self.initialized,
            )
        structured = final.get("structured_output")
        text = str(final.get("result") or "") or _errors(final.get("errors"))
        is_error = final.get("is_error") is True
        api_status = final.get("api_error_status")
        status = (
            api_status if isinstance(api_status, int) and not isinstance(api_status, bool) else None
        )
        deferred = final.get("deferred_tool_use")
        return AgentOutcome(
            # result の後に終わらず kill しても、result は届いている
            ending=Ending.RESULT,
            exit_code=exit_code,
            session=call.session,
            subtype=_str_or_none(final.get("subtype")),
            is_error=is_error,
            stop_reason=_str_or_none(final.get("stop_reason")),
            terminal_reason=_str_or_none(final.get("terminal_reason")),
            num_turns=_int(final.get("num_turns")),
            text=text,
            structured=structured if isinstance(structured, Mapping) else None,
            usage=_usage(final.get("usage")),
            cost_usd=_float(final.get("total_cost_usd")),
            api_error_status=status,
            permission_denials=tuple(
                d for d in final.get("permission_denials") or () if isinstance(d, Mapping)
            ),
            hook_denials=self.hook_denials,
            deferred=(_deferred(deferred) if final.get("stop_reason") == "tool_deferred" else None),
            # 上限に当たったと見るのは、エラーで終わったときだけ。成功した result の後に届いた
            # rate_limit_event や、本文に出た「rate limit」の語では上限にしない
            rate_limited=is_error
            and (
                self.rate_limit_rejected
                or status == 429
                or limit_text(text)
                or self._ended_on_limit(status)
            ),
            interrupted=interrupted,
            stderr=stderr,
            capabilities=self.capabilities,
            log_path=call.log_path,
            initialized=self.initialized,
        )


def _hook_denials(event: Mapping[str, Any]) -> int:
    """PreToolUse のフックは、通したときは `type: attachment`（`hook_success`）のイベントを出すが、
    拒んだときは出さない（claude 2.1.288 で確かめた）。拒まれた呼び出しは次の user イベントの
    tool_result にだけ残る。`hook_response` の `exit_code` を数えると SessionStart のフックの失敗まで
    数える。"""
    message = event.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, Sequence) or isinstance(content, str):
        return 0
    return sum(
        1
        for block in content
        if isinstance(block, Mapping)
        and block.get("type") == "tool_result"
        and block.get("is_error") is True
        and _HOOK_DENIAL.match(_text(block.get("content")))
    )


def _text(content: Any) -> str:
    """tool_result の本文は、文字列のことも `{"type": "text"}` の並びのこともある。"""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        return "".join(str(b.get("text") or "") for b in content if isinstance(b, Mapping))
    return ""


def _message_text(event: Mapping[str, Any]) -> str:
    message = event.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    return _text(content)


def _errors(value: Any) -> str:
    """result の `errors`。`error_max_turns`・`error_during_execution` の result は `result` の欄を
    持たず、理由はここにだけ載る（claude 2.1.288 で確かめた）。interrupt で止めたときに載る
    `[ede_diagnostic]` の文は claude の中の診断で、失敗の理由ではないので外す。"""
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ""
    return "\n".join(
        e for e in value if isinstance(e, str) and e and not e.startswith("[ede_diagnostic]")
    )


def _deferred(value: Any) -> DeferredToolUse | None:
    if not isinstance(value, Mapping):
        return None
    tool_input = value.get("input")
    return DeferredToolUse(
        tool_use_id=str(value.get("id") or value.get("tool_use_id") or ""),
        name=str(value.get("name") or ""),
        input=tool_input if isinstance(tool_input, Mapping) else {},
    )


def _usage(value: Any) -> Usage:
    if not isinstance(value, Mapping):
        return Usage()
    return Usage(
        input_tokens=_int(value.get("input_tokens")),
        output_tokens=_int(value.get("output_tokens")),
        cache_read_input_tokens=_int(value.get("cache_read_input_tokens")),
        cache_creation_input_tokens=_int(value.get("cache_creation_input_tokens")),
    )


def _event(line: str) -> dict[str, Any] | None:
    body = line.strip()
    if not body.startswith("{"):
        return None
    try:
        loaded = json.loads(body)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _tail(path: Path, offset: int, limit: int = 4000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")[-limit:].strip()


# 利用枠の上限の見分け。`rate_limit_event` の欄の名前と置き場は claude 2.1.288 で実測した（ふつうの
# 呼び出しにも毎回出て、`status` は `allowed`）。上限に当てた形は実測していない。assistant の
# `error: "rate_limit"` は、実行ファイルの中の型の定義から読んだ。この印は 429 のほかに 529 の過負荷と
# `model_blocked` にも付くので、それだけでは上限にしない。上限の文言は、過負荷などの文言
# （`_NOT_RATE_LIMIT_TEXT`）に当たれば数えない。次のどれかで「当たった」とする:
# - result が `is_error` で、`rate_limit_info.status` が `rejected` の rate_limit_event を受けたか、
#   `api_error_status` が 429 か、本文に上限の文言がある
# - result が無く、`rejected` の rate_limit_event を受けたか、標準エラーに上限の文言がある
# - ステージ自身の最後の assistant が `error: "rate_limit"` で、その本文に上限の文言があるか 429 である
