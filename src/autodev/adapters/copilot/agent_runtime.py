"""GitHub Copilot CLI（`copilot -p --output-format json`）でステージと統括を起動する実装。

結果は Claude・Codex の実装と同じ「起きた事実」（`AgentOutcome`）で返す。起動のしかたで効く事実:

- プロンプトは `-p` の引数で渡す。Copilot には `--append-system-prompt` も出力スキーマのオプションも無いので、
  新規のときは `system_append`・返し方の決まり・スキーマの本文・`call.prompt` をつないで渡す。続けるとき
  （`--resume`）は `system_append` を付けない
- 起動のたびにプラグイン（ガードのフック）を書く。`COPILOT_HOME` を含む環境変数は呼び出し元のものを
  そのまま渡し、`call.env` とフックの記録（`AUTODEV_HOOK_RECORD`）だけを足す
- セッションの ID はこちらが決める（`--session-id`）。存在しない ID の `--resume` は、イベントを 1 つも出さず
  終了コード 1 で終わる（`initialized` が偽になり、既存の流れが新しいセッションで作り直す）
- interrupt は SIGINT。Copilot は `abort` を出して終了コード 0 で終わる
- 拒んだ呼び出しは JSONL に出ないので、フックが書く記録のファイル（`<log_path>.hooks`）を数える。記録は
  同じ実行・同じ統括の走りで使い回すので、起動時の行数を控え、そこから後ろだけを使う
- ask の呼び出しはフックが通さず、質問を記録に控えて拒む。ここで見つけたら SIGINT で止め、`deferred` に
  して返す。回答が来て続けるとき（`prompt=None`）は、回答を本文に入れたプロンプトで続ける
- 利用枠の上限の形は、Copilot CLI 1.0.92 の型定義から取った（実際に上限へ当てて見たものではない）。
  `session.error` の errorType が `rate_limit`・`quota` のものと、auto への切り替えの承認待ち
  （`auto_mode_switch.requested`）を、上限として扱って SIGINT で止める。型定義と違う形で出たときの控えに、
  正常に終わらなかった走りの本文・標準エラーも `limit_text` で見る
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
from collections.abc import Callable, Mapping
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
from ..codex.agent_runtime import answer_prompt, hooks_path
from ..codex.hook import HookRecord, count_records, read_records
from ..process import children
from ..process.command import run
from .launch import env_for, has_login, source_home, write_plugin

#: 結果の返し方。Copilot には出力の形を縛る旗が無いので、プロンプトの末尾にスキーマと並べて置く
#: （前に置くと、後ろの長い指示書を読むうちに崩れる）
COPILOT_RETURN_RULE = (
    "終わりに、最後のメッセージに次の JSON Schema に合う JSON の object を 1 つだけ書いて返す。"
    "前置き・説明・```json の囲みを付けない。"
)
#: プロセスが終わるのを待つ間、記録のファイルと期限を見る間隔
_POLL = 0.2
#: フックが必ず走るツール。`report_intent` など、フックが走らないツールでは数えない
_HOOKED_TOOLS = frozenset({"bash", "create", "edit", "apply_patch"})
#: 利用枠の上限を知らせる `session.error` の errorType
_LIMIT_ERROR_TYPES = frozenset({"rate_limit", "quota"})
NO_RECORDS = "フックの記録が 0 件。プラグインのフックが読み込まれていない"
LIMIT_INTERRUPT = "利用枠の上限のイベントを受けたため止めた"
_OBJECT_HEAD = re.compile(r"^[ \t]*\{", re.MULTILINE)


def argv_for(call: AgentCall, prompt: str, plugin_dir: str, copilot: str = "copilot") -> list[str]:
    argv = [
        copilot,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--allow-all-tools",
        "--allow-all-paths",
        "--no-ask-user",
        "--no-auto-update",
        "--plugin-dir",
        plugin_dir,
    ]
    if call.model:
        argv += ["--model", call.model]
    if call.effort:
        argv += ["--reasoning-effort", call.effort]
    argv += ["--resume" if call.resume else "--session-id", str(call.session)]
    return argv


def limit_event(event: Mapping[str, Any]) -> bool:
    """利用枠の上限を知らせるイベントか。"""
    kind = event.get("type")
    if kind == "auto_mode_switch.requested":
        return True
    if kind != "session.error":
        return False
    data = event.get("data")
    return isinstance(data, Mapping) and data.get("errorType") in _LIMIT_ERROR_TYPES


class CopilotProcess:
    """走っている `copilot -p` 1 つ。`wait` は 1 つのスレッドから呼ぶ。`interrupt`・`kill` は別の
    スレッドから呼んでよい。"""

    def __init__(
        self,
        call: AgentCall,
        *,
        copilot: str = "copilot",
        on_progress: Callable[[Progress], None] | None = None,
        progress_interval: float = PROGRESS_INTERVAL,
        interrupt_grace: float = INTERRUPT_GRACE,
    ) -> None:
        if call.run_dir is None:
            raise ValueError("Copilot の実装には AgentCall.run_dir が要る")
        self.call = call
        self._run_dir = call.run_dir
        self._on_progress = on_progress
        self._progress_interval = progress_interval
        self._interrupt_grace = interrupt_grace
        self._lock = threading.Lock()
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._state = _Collector()
        self._interrupted: str | None = None
        self._kill_at: float | None = None
        self._killed = False
        self._deadline = time.monotonic() + call.timeout if call.timeout is not None else None
        self._schema_text = _schema_text(call.json_schema)
        self._records = hooks_path(call)

        plugin = write_plugin(Path(call.run_dir) / "sessions" / "copilot" / "plugin")
        prompt = self._prompt(call)

        Path(call.log_path).parent.mkdir(parents=True, exist_ok=True)
        # 起動する時点の行数。この走りの記録は、ここから後ろに足されたものだけ
        self._start_line = count_records(self._records)
        self._log: IO[str] = open(call.log_path, "a", encoding="utf-8")  # noqa: SIM115
        self._err: IO[str] = open(f"{call.log_path}.err", "a", encoding="utf-8")  # noqa: SIM115
        self._err_offset = self._err.tell()
        argv = argv_for(call, prompt, str(plugin), copilot)
        self._record(
            {
                "type": "autodev/call",
                "argv": argv,
                "cwd": call.cwd,
                "prompt": prompt,
                "system_append": call.system_append,
            }
        )
        try:
            self._proc: subprocess.Popen[str] = subprocess.Popen(
                argv,
                cwd=call.cwd,
                env=env_for(call),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=self._err,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # kill するとき、copilot が起こしたツールのプロセスまでまとめて止める
                start_new_session=True,
            )
        except OSError:
            self._release()
            raise
        try:
            children.record(self._proc.pid, argv)
        except BaseException:
            children.stop_unwatched(self._proc)
            self._release()
            raise
        threading.Thread(target=self._pump, daemon=True).start()

    # --- 外から ---

    def interrupt(self, reason: str) -> None:
        """SIGINT をプロセスグループに送る。`interrupt_grace` 秒で終わらなければ kill する。"""
        with self._lock:
            if self._interrupted is not None:
                return
            self._interrupted = reason
            self._kill_at = time.monotonic() + self._interrupt_grace
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._proc.pid, signal.SIGINT)

    def kill(self) -> None:
        with self._lock:
            self._killed = True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._proc.pid, signal.SIGKILL)

    # --- 待つ ---

    def wait(self) -> AgentOutcome:
        proc = self._proc
        last_progress = 0.0
        last_denials = 0
        eof = False
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
            self._watch_asks()
            if eof:
                # 標準出力を閉じた後も、プロセスが終わるまで期限と kill の監視を続ける
                if proc.poll() is not None:
                    break
                time.sleep(_POLL)
                continue
            try:
                line = self._lines.get(timeout=_POLL)
            except queue.Empty:
                # 拒んだ呼び出しは JSONL に出ないので、イベントが来ない間も記録の数を知らせる
                denials = self._denials()
                if denials != last_denials:
                    last_denials = denials
                    self._notify()
                continue
            if line is None:
                eof = True
                continue
            event = _event(line)
            if event is None:
                continue
            with self._lock:
                self._state.take(event)
            if limit_event(event):
                self.interrupt(LIMIT_INTERRUPT)
            now = time.monotonic()
            denials = self._denials()
            if denials != last_denials or now - last_progress >= self._progress_interval:
                last_progress, last_denials = now, denials
                self._notify()
        code = proc.wait()
        children.forget(proc.pid)
        records = read_records(self._records, self._start_line)
        self._notify(records)
        stderr = _tail(Path(f"{self.call.log_path}.err"), self._err_offset)
        self._release()
        return self._state.outcome(
            call=self.call,
            exit_code=code,
            killed=self._killed,
            interrupted=self._interrupted,
            stderr=stderr,
            records=records,
        )

    # --- 中 ---

    def _prompt(self, call: AgentCall) -> str:
        body = answer_prompt(self._run_dir, self._records) if call.prompt is None else call.prompt
        parts = [body]
        if self._schema_text:
            parts += [COPILOT_RETURN_RULE, self._schema_text]
        if not call.resume:
            parts.insert(0, call.system_append or "")
        return "\n\n".join(part for part in parts if part)

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            # wait を呼ぶ前から、走りの様子がログで追えるように、ここで書く
            self._log.write(line)
            self._log.flush()
            self._lines.put(line)
        self._lines.put(None)

    def _denials(self) -> int:
        return _denials(read_records(self._records, self._start_line))

    def _watch_asks(self) -> None:
        """この走りで ask の記録が足されたら、SIGINT で止める。回答を返しただけの記録では止めない。"""
        if self._interrupted is not None:
            return
        asks = [r for r in read_records(self._records, self._start_line) if r.question is not None]
        if asks:
            self.interrupt(f"ask の質問を driver に渡すため止めた: {asks[-1].question}")

    def _release(self) -> None:
        for stream in (self._log, self._err):
            with contextlib.suppress(OSError):
                stream.close()

    def _record(self, payload: Mapping[str, Any]) -> None:
        self._log.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._log.flush()

    def _notify(self, records: list[HookRecord] | None = None) -> None:
        if self._on_progress is None:
            return
        denials = _denials(records) if records is not None else self._denials()
        progress = self._state.progress(denials)
        # 進み具合を受ける側の誤りで、ステージを止めない
        with contextlib.suppress(Exception):
            self._on_progress(progress)


class CopilotRuntime:
    """`copilot -p` を起こす。`copilot` は差し替えられる（検査では偽のコマンドを置く）。"""

    def __init__(
        self,
        copilot: str = "copilot",
        *,
        progress_interval: float = PROGRESS_INTERVAL,
        interrupt_grace: float = INTERRUPT_GRACE,
    ) -> None:
        self.copilot = copilot
        self.progress_interval = progress_interval
        self.interrupt_grace = interrupt_grace

    def start(
        self, call: AgentCall, on_progress: Callable[[Progress], None] | None = None
    ) -> CopilotProcess:
        return CopilotProcess(
            call,
            copilot=self.copilot,
            on_progress=on_progress,
            progress_interval=self.progress_interval,
            interrupt_grace=self.interrupt_grace,
        )

    def run(
        self, call: AgentCall, on_progress: Callable[[Progress], None] | None = None
    ) -> AgentOutcome:
        return self.start(call, on_progress).wait()

    def missing(self) -> list[str]:
        """走り出す前に足りないもの。モデルは呼ばない。"""
        if not run([self.copilot, "--version"]).ok:
            return ["copilot が起動できない（PATH にあるか、`copilot --version` が通るか）"]
        source = source_home(os.environ)
        try:
            logged_in = has_login(source)
        except ValueError as error:
            return [f"copilot の設定を読めない（{error}）"]
        if not logged_in:
            return [f"copilot にログインしていない（`copilot login` で入る。設定: {source}）"]
        return []


class _Collector:
    """流れてきたイベントから、事実を拾う。"""

    def __init__(self) -> None:
        self.events = 0
        self.result: Mapping[str, Any] | None = None
        self.aborted = False
        self.limit = False
        #: 最後の `session.error` の本文
        self.error: str | None = None
        self.last_message: str | None = None
        self.messages = 0
        self.last_tool: str | None = None
        self.used_tools = False

    def take(self, event: Mapping[str, Any]) -> None:
        self.events += 1
        kind = event.get("type")
        data = event.get("data")
        data = data if isinstance(data, Mapping) else {}
        if limit_event(event):
            self.limit = True
        if kind == "result":
            self.result = event
        elif kind == "abort":
            self.aborted = True
        elif kind == "session.error":
            message = data.get("message")
            self.error = message if isinstance(message, str) else ""
        elif kind == "assistant.message":
            content = data.get("content")
            if isinstance(content, str):
                self.last_message = content
                self.messages += 1
        elif kind == "tool.execution_start":
            name = data.get("toolName")
            if isinstance(name, str):
                self.last_tool = name
                if name in _HOOKED_TOOLS:
                    self.used_tools = True

    def progress(self, denials: int) -> Progress:
        return Progress(self.messages, self.last_tool, denials, self.events)

    def outcome(
        self,
        *,
        call: AgentCall,
        exit_code: int,
        killed: bool,
        interrupted: str | None,
        stderr: str,
        records: list[HookRecord],
    ) -> AgentOutcome:
        initialized = self.events > 0
        finished = self.result is not None
        if killed:
            ending = Ending.KILLED
        elif finished or (interrupted is not None and initialized):
            # こちらが止めても、Copilot は abort を出して 0 で終わり、セッションは開いていて続けられる
            ending = Ending.RESULT
        else:
            ending = Ending.NO_RESULT
        stopped = interrupted is not None
        normal = ending is Ending.RESULT and finished and exit_code == 0 and not stopped
        text = self.last_message or ""
        if self.error is not None and not normal:
            text = self.error or text
        is_error = not normal
        untrusted = self.used_tools and not records
        if untrusted:
            is_error = True
            text = f"{NO_RECORDS}\n{text}" if text else NO_RECORDS
        # フックが走っていない走り・止めた走りの判断は、統括が拾わないように結果を渡さない
        structured = None if stopped or killed or untrusted or not finished else self._structured()
        asks = [r for r in records if r.question is not None]
        deferred = None
        if asks:
            ask = asks[-1]
            deferred = DeferredToolUse(
                ask.tool_use_id or "", "Bash", {"command": ask.command or ""}
            )
        rate_limited = self.limit or (
            not normal and (limit_text(text) or limit_text(self.error or "") or limit_text(stderr))
        )
        return AgentOutcome(
            ending=ending,
            exit_code=exit_code,
            session=call.session,
            is_error=is_error,
            num_turns=self.messages,
            text=text,
            structured=structured,
            usage=Usage(),
            cost_usd=0.0,
            hook_denials=_denials(records),
            deferred=deferred,
            rate_limited=rate_limited,
            interrupted=interrupted,
            stderr=stderr,
            log_path=call.log_path,
            initialized=initialized,
        )

    def _structured(self) -> dict[str, Any] | None:
        if self.last_message is None:
            return None
        return _last_object(self.last_message.strip())


def _last_object(body: str) -> dict[str, Any] | None:
    """JSON だけを返せと書いても、モデルは前置きや ```json の囲みを付ける。前置きの中の例を拾わないよう
    末尾で閉じる object だけを採り、外側が壊れたときに入れ子の object を拾わないよう行頭の `{` からだけ読む。"""
    decoder = json.JSONDecoder()
    for head in _OBJECT_HEAD.finditer(body):
        try:
            value, end = decoder.raw_decode(body, head.end() - 1)
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict) and body[end:].strip() in ("", "```"):
            return value
    return None


def _denials(records: list[HookRecord]) -> int:
    """拒んだ数。park-on-ask が控えた ask と、回答を返した記録（`denied` が偽）は数えない。"""
    return sum(1 for r in records if r.denied and r.question is None)


def _schema_text(text: str | None) -> str:
    if not text:
        return ""
    try:
        loaded = json.loads(text)
    except ValueError:
        return text
    return json.dumps(loaded, ensure_ascii=False, indent=2)


def _event(line: str) -> dict[str, Any] | None:
    body = line.strip()
    if not body.startswith("{"):
        return None
    try:
        loaded = json.loads(body)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _tail(path: Path, offset: int, limit: int = 4000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")[-limit:].strip()
