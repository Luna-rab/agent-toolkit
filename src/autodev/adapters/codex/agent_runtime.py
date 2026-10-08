"""Codex CLI（`codex exec --json`）でステージと統括を起動する実装。

結果は Claude の実装と同じ「起きた事実」（`AgentOutcome`）で返す。起動のしかたで効く事実:

- プロンプトは標準入力で渡して閉じる。Codex には `--append-system-prompt` が無いので、`system_append` は
  プロンプトの頭に付ける
- サンドボックスは `danger-full-access`。worktree の `.git` が worktree の外にあり、workspace-write では
  commit できない。`codex exec resume` は `-s` を受けないので、続けるときは `-c sandbox_mode=...` で渡す
- thread_id は Codex が決める。`thread.started` を受けたら `sessions/codex/<SessionId>` に控え、続けるときに
  引く。控えが無いのに続けるよう求められたら、起こさずに「開けなかった」として返す
- interrupt は SIGINT。Codex は `turn.completed` を出さずに終了コード 1 で終わるが、履歴は残り、続けられる
- 拒んだ呼び出しは JSONL に出ないので、フックが書く記録のファイル（`<log_path>.hooks`）を数える。記録は
  同じ実行・同じ統括の走りで使い回すので、起動時の行数を控え、そこから後ろだけを使う
- ask の呼び出しはフックが通さず、質問を記録に控えて拒む。ここで見つけたら SIGINT で止め、`deferred` に
  して返す。回答が来て続けるとき（`prompt=None`）は、回答を本文に入れたプロンプトで続ける
- 利用枠の上限に当てた形（`turn.failed` の `error.message`・標準エラー）は実測していない。Claude と同じ
  `limit_text` の文言で見分ける
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any

from ...domain.value_objects.session_id import SessionId
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
from .hook import (
    ASK_ANSWERED_RULE,
    HOOK_RECORD_ENV,
    HookRecord,
    _answer_text,  # フックが返す回答と同じ読み方にする
    count_records,
    read_records,
)
from .schema import from_strict, strict_schema

CODEX_RETURN_RULE = "終わりに、最後のメッセージに出力スキーマの形の JSON だけを書いて返す。"
#: プロセスが終わるのを待つ間、記録のファイルと期限を見る間隔
_POLL = 0.2
#: フックが必ず走る item の種類。計画の更新・検索など、フックが走らない item では数えない
_HOOKED_ITEMS = frozenset({"command_execution", "file_change"})
NO_RECORDS = "フックの記録が 0 件。/hooks で autodev のフックを信頼する"


def thread_path(run_dir: str | os.PathLike[str], session: SessionId) -> Path:
    return Path(run_dir) / "sessions" / "codex" / str(session)


def argv_for(
    call: AgentCall, schema_file: str, thread_id: str | None, codex: str = "codex"
) -> list[str]:
    resuming = thread_id is not None
    argv = [codex, "exec", *(["resume"] if resuming else []), "--json", "--skip-git-repo-check"]
    if not resuming:
        argv += ["-s", "danger-full-access"]
    if schema_file:
        argv += ["--output-schema", schema_file]
    if call.model:
        argv += ["-m", call.model]
    if call.effort:
        argv += ["-c", f"model_reasoning_effort={call.effort}"]
    if resuming:
        argv += ["-c", 'sandbox_mode="danger-full-access"', str(thread_id)]
    argv.append("-")
    return argv


def env_for(call: AgentCall) -> dict[str, str]:
    merged = {**os.environ, **call.env}
    merged[HOOK_RECORD_ENV] = hooks_path(call)
    return merged


def hooks_path(call: AgentCall) -> str:
    return f"{call.log_path}.hooks"


def answer_prompt(run_dir: str, records_path: str) -> str:
    """ask の再開のプロンプト。記録のファイル全体の最後の ask の質問と、届いた回答を入れる。"""
    asks = [r for r in read_records(records_path) if r.question is not None]
    if not asks or asks[-1].tool_use_id is None:
        return ""
    ask = asks[-1]
    try:
        answer = _answer_text(Path(run_dir) / "answers" / f"{ask.tool_use_id}.json")
    except OSError:
        # 回答が無いのに「呼び直さない」と言うと、モデルは回答を得られず聞き直せもしない
        return ""
    lines = [f"質問: {ask.question}", f"回答: {answer}", ASK_ANSWERED_RULE]
    return "\n".join(lines)


class CodexProcess:
    """走っている `codex exec` 1 つ。`wait` は 1 つのスレッドから呼ぶ。`interrupt`・`kill` は別の
    スレッドから呼んでよい。"""

    def __init__(
        self,
        call: AgentCall,
        *,
        codex: str = "codex",
        on_progress: Callable[[Progress], None] | None = None,
        progress_interval: float = PROGRESS_INTERVAL,
        interrupt_grace: float = INTERRUPT_GRACE,
    ) -> None:
        if call.run_dir is None:
            raise ValueError("Codex の実装には AgentCall.run_dir が要る")
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
        self._schema = _load_schema(call.json_schema)
        self._records = hooks_path(call)
        self._proc: subprocess.Popen[str] | None = None
        self._temp: list[str] = []

        thread_id: str | None = None
        if call.resume:
            thread_id = _read_thread(thread_path(call.run_dir, call.session))
            if thread_id is None:
                # 控えが無い: 起こさずに、セッションを開けなかったものとして返す
                return
        prompt = self._prompt(call, resuming=thread_id is not None)

        Path(call.log_path).parent.mkdir(parents=True, exist_ok=True)
        # 起動する時点の行数。この走りの記録は、ここから後ろに足されたものだけ
        self._start_line = count_records(self._records)
        self._log: IO[str] = open(call.log_path, "a", encoding="utf-8")  # noqa: SIM115
        self._err: IO[str] = open(f"{call.log_path}.err", "a", encoding="utf-8")  # noqa: SIM115
        self._err_offset = self._err.tell()
        schema_file = self._write_schema()
        argv = argv_for(call, schema_file, thread_id, codex)
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
                # kill するとき、codex が起こしたツールのプロセスまでまとめて止める
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
        self._send(prompt)

    # --- 外から ---

    def interrupt(self, reason: str) -> None:
        """SIGINT をプロセスグループに送る。`interrupt_grace` 秒で終わらなければ kill する。"""
        with self._lock:
            if self._interrupted is not None or self._proc is None:
                return
            self._interrupted = reason
            self._kill_at = time.monotonic() + self._interrupt_grace
            proc = self._proc
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGINT)

    def kill(self) -> None:
        with self._lock:
            self._killed = True
            proc = self._proc
        if proc is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

    # --- 待つ ---

    def wait(self) -> AgentOutcome:
        proc = self._proc
        if proc is None:
            return AgentOutcome(
                ending=Ending.NO_RESULT,
                exit_code=None,
                session=self.call.session,
                log_path=self.call.log_path,
                initialized=False,
            )
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
            self._watch_asks()
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
                break
            event = _event(line)
            if event is None:
                continue
            with self._lock:
                self._state.take(event)
            if event.get("type") == "thread.started":
                self._save_thread(event.get("thread_id"))
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
            schema=self._schema,
        )

    # --- 中 ---

    def _prompt(self, call: AgentCall, *, resuming: bool) -> str:
        if call.prompt is None:
            body = answer_prompt(self._run_dir, self._records)
            parts = [CODEX_RETURN_RULE, body]
        else:
            parts = [CODEX_RETURN_RULE, call.prompt]
        if not resuming:
            parts.insert(0, call.system_append or "")
        return "\n\n".join(part for part in parts if part)

    def _write_schema(self) -> str:
        if self._schema is None:
            return ""
        handle, path = tempfile.mkstemp(prefix="autodev-schema-", suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(strict_schema(self._schema), stream, ensure_ascii=False)
        self._temp.append(path)
        return path

    def _send(self, prompt: str) -> None:
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        with contextlib.suppress(BrokenPipeError, ValueError, OSError):
            proc.stdin.write(prompt)
            proc.stdin.flush()
        with contextlib.suppress(BrokenPipeError, OSError):
            proc.stdin.close()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            # wait を呼ぶ前から、走りの様子がログで追えるように、ここで書く
            self._log.write(line)
            self._log.flush()
            self._lines.put(line)
        self._lines.put(None)

    def _save_thread(self, thread_id: Any) -> None:
        if not isinstance(thread_id, str) or not thread_id:
            return
        path = thread_path(self._run_dir, self.call.session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(thread_id, encoding="utf-8")

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
        for path in self._temp:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    os.unlink(path)
        self._temp.clear()

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


class CodexRuntime:
    """`codex exec` を起こす。`codex` は差し替えられる（検査では偽のコマンドを置く）。"""

    def __init__(
        self,
        codex: str = "codex",
        *,
        progress_interval: float = PROGRESS_INTERVAL,
        interrupt_grace: float = INTERRUPT_GRACE,
    ) -> None:
        self.codex = codex
        self.progress_interval = progress_interval
        self.interrupt_grace = interrupt_grace

    def start(
        self, call: AgentCall, on_progress: Callable[[Progress], None] | None = None
    ) -> CodexProcess:
        return CodexProcess(
            call,
            codex=self.codex,
            on_progress=on_progress,
            progress_interval=self.progress_interval,
            interrupt_grace=self.interrupt_grace,
        )

    def run(
        self, call: AgentCall, on_progress: Callable[[Progress], None] | None = None
    ) -> AgentOutcome:
        return self.start(call, on_progress).wait()

    def missing(self) -> list[str]:
        """走り出す前に足りないもの。"""
        if not run([self.codex, "--version"]).ok:
            return ["codex が起動できない（PATH にあるか、`codex --version` が通るか）"]
        if not run([self.codex, "login", "status"]).ok:
            return ["codex にログインしていない（`codex login` で入る）"]
        return []


class _Collector:
    """流れてきたイベントから、事実を拾う。"""

    def __init__(self) -> None:
        self.events = 0
        self.thread_started = False
        self.turn_started = False
        self.turns_completed = 0
        self.completed = False
        self.failed: str | None = None
        #: 最上位の `error` イベントの本文。`turn.failed` が続かずに終わることもある
        self.error: str | None = None
        self.usage = Usage()
        self.last_message: str | None = None
        self.messages = 0
        self.last_tool: str | None = None
        self.used_tools = False

    def take(self, event: Mapping[str, Any]) -> None:
        self.events += 1
        kind = event.get("type")
        if kind == "thread.started":
            self.thread_started = True
        elif kind == "turn.started":
            self.turn_started = True
        elif kind == "turn.completed":
            self.completed = True
            self.turns_completed += 1
            self.usage = _usage(event.get("usage"))
        elif kind == "error":
            message = event.get("message")
            self.error = message if isinstance(message, str) else ""
        elif kind == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, Mapping) else None
            self.failed = message if isinstance(message, str) else ""
        elif kind in ("item.started", "item.completed"):
            item = event.get("item")
            if not isinstance(item, Mapping):
                return
            item_type = item.get("type")
            if item_type == "agent_message" and kind == "item.completed":
                text = item.get("text")
                if isinstance(text, str):
                    self.last_message = text
                    self.messages += 1
            elif isinstance(item_type, str) and item_type not in ("reasoning", "error"):
                self.last_tool = item_type
                if item_type in _HOOKED_ITEMS:
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
        schema: Mapping[str, Any] | None,
    ) -> AgentOutcome:
        initialized = self.thread_started or self.turn_started
        finished = self.completed or self.failed is not None
        if finished:
            ending = Ending.RESULT
        elif killed:
            ending = Ending.KILLED
        elif interrupted is not None and initialized:
            # Codex は SIGINT で turn.completed を出さずに終わるが、セッションは開いていて続けられる
            ending = Ending.RESULT
        else:
            ending = Ending.NO_RESULT
        stopped = ending is Ending.RESULT and not finished
        text = self.failed if self.failed is not None else (self.last_message or "")
        is_error = self.failed is not None or stopped
        if self.error is not None and not finished:
            is_error = True
            text = self.error or text
        untrusted = self.used_tools and not records
        if untrusted:
            is_error = True
            text = f"{NO_RECORDS}\n{text}" if text else NO_RECORDS
        # フックが走っていない走りの判断は、統括が拾わないように結果を渡さない
        stopped_midway = interrupted is not None and not finished
        structured = None if stopped_midway or untrusted else self._structured(schema)
        asks = [r for r in records if r.question is not None]
        deferred = None
        if asks:
            ask = asks[-1]
            deferred = DeferredToolUse(
                ask.tool_use_id or "", "Bash", {"command": ask.command or ""}
            )
        failed = self.failed is not None or ending is not Ending.RESULT
        return AgentOutcome(
            ending=ending,
            exit_code=exit_code,
            session=call.session,
            is_error=is_error,
            num_turns=self.turns_completed,
            text=text,
            structured=structured,
            usage=self.usage,
            cost_usd=0.0,
            hook_denials=_denials(records),
            deferred=deferred,
            rate_limited=failed and (limit_text(text) or limit_text(stderr)),
            interrupted=interrupted,
            stderr=stderr,
            log_path=call.log_path,
            initialized=initialized,
        )

    def _structured(self, schema: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if self.last_message is None:
            return None
        try:
            value = json.loads(self.last_message)
        except ValueError:
            return None
        if not isinstance(value, dict):
            return None
        restored = from_strict(value, schema) if schema is not None else value
        return restored if isinstance(restored, dict) else None


def _denials(records: list[HookRecord]) -> int:
    """拒んだ数。park-on-ask が控えた ask と、回答を返した記録（`denied` が偽）は数えない。"""
    return sum(1 for r in records if r.denied and r.question is None)


def _load_schema(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    loaded = json.loads(text)
    return loaded if isinstance(loaded, dict) else None


def _read_thread(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _usage(value: Any) -> Usage:
    if not isinstance(value, Mapping):
        return Usage()
    return Usage(
        input_tokens=_int(value.get("input_tokens")),
        output_tokens=_int(value.get("output_tokens")),
        cache_read_input_tokens=_int(value.get("cached_input_tokens")),
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


def _tail(path: Path, offset: int, limit: int = 4000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")[-limit:].strip()
