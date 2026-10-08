"""LLM の統括（ラン統括と実装タスクの統括）を `claude -p` で起こし、返った判断をコマンドにして返す。

- 統括 1 つにつき、ターンは 1 つずつ順に走らせる。同じセッションを 2 つ同時に `--resume` しない。
  走っている間に届いた知らせは並べておき、前のターンの判断をメインループが処理し終えてから
  （`Ticket.submit` の `settled`）次のターンを始める。前の判断が確定する前の状態で次を決めさせない
- セッション id は `sessions/<名前>.json` に控え、2 回目からは `--resume` で続きから起こす
  （日をまたいでも続く）。続けたセッションで MAX_ATTEMPTS 回続けて落ちたら、
  新しいセッションに替えて、知らせからもう 1 回起こす
- 判断の `decision` と中身の欄が合わない・読めない・結果を返さずに終わったなら、その
  理由で同じセッションに差し戻す。置き換えたコマンドを集約が拒んだら、`settled` で理由が届くので、
  同じく差し戻す
- 差し戻しと呼び直しを使い切ったら、知らせを捨てずに `ReportSupervisorFailure` で Run に渡す。
  どこへ上げるか（タスク統括ならラン統括、ラン統括なら `/autodev`）と、答えたら同じ知らせで
  起こし直すことはドメインが決める（`EscalationRouter`・`wake_for` の RETRY）
- 利用枠の上限に当たったら、Panic を返す（アプリケーション層がパニックにする）。
  パニックを返したら、ほかの統括の次のターンも起こさない。Panic の CommandId は知らせと Run が
  呼び直された回数で決める（呼び直した後に同じ知らせでまた当たったら、新しいパニックにする）
- 判断の CommandId は、起こした知らせだけで決める（差し戻した回数を入れない）。拒まれた判断は
  イベントを出さないので id が残らず、直した判断が同じ id で通る。通った判断がある知らせで、呼び
  直した driver が起こし直しても、その判断は `has_command` で弾かれる（知らせ 1 つに判断は 1 つ）
- 呼び直した driver は、もう応じなかったと Run が受けた知らせ（`Run.reported_failure`）では
  起こし直さない

状態（並べた知らせ・走っているか）を読み書きするのはメインループのスレッドだけで、別のスレッドは
`claude -p` を待って札で返すだけである。別のスレッドはメインループの状態（イベントの列・集約）を
読まない。要る値（対象リポジトリ・呼び直された回数）は、起こす時点でメインループのスレッドで取って
ターンに持たせる。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ...adapters.agent.types import AgentCall, AgentOutcome, Ending
from ...adapters.claude.guard import guard_context, stage_env
from ...domain.commands.base import Command
from ...domain.commands.run import Panic, ReportSupervisorFailure
from ...domain.supervision import SUPERVISOR_GUARD, Supervisor
from ...domain.value_objects.command_id import CommandId
from ...domain.value_objects.event_id import EventId
from ...domain.value_objects.issuer import Issuer
from ...domain.value_objects.model_class import ModelClasses
from ...domain.value_objects.session_id import SessionId
from ...domain.value_objects.task_id import TaskId
from ...infra.files import write_atomic
from ...infra.paths import RunPaths
from ..driving.mainloop import Inbox, Ticket
from ..stages.prompts.assets import schema_text
from ..stages.prompts.render import render_rejection
from ..stages.stage_context import StagePrompt
from .decisions import RUN_DECISIONS, TASK_DECISIONS, DecisionError, to_command

log = logging.getLogger(__name__)

#: 1 つの知らせに、形の合わない・拒まれた判断を差し戻す回数。超えたら、1 段上へ上げる
#: （統括が同じ誤りを繰り返すと、利用枠を使い切るまで回り続ける）
MAX_CORRECTIONS = 3
#: 続けたセッションで、結果を返さずに落ちたときに呼び直す回数（1 回は一時的な誤りとみなす）
MAX_ATTEMPTS = 2
#: 続けたセッションで落ち続けたら、新しいセッションに替えて起こす回数
FRESH_ATTEMPTS = 1


class AgentProcessLike(Protocol):
    def wait(self) -> AgentOutcome: ...

    def interrupt(self, reason: str) -> None: ...


class AgentRuntimeLike(Protocol):
    def start(self, call: AgentCall) -> AgentProcessLike: ...


@dataclass(frozen=True)
class SupervisorSetting:
    """統括を起こすときの、ランごとの値。"""

    paths: RunPaths
    #: 対象リポジトリの手元の checkout（ガードが書き込みを止める場所の 1 つ）。メインループの
    #: スレッドで、起こす時点に呼ぶ
    repository: Callable[[], str]
    #: Run が呼び直された回数（`Run.resumes`）。メインループのスレッドで、起こす時点に呼ぶ
    resumes: Callable[[], int] = lambda: 0
    #: ランが記録したクラスごとのモデルと effort（`Run.models`）。メインループのスレッドで、起こす時点に呼ぶ
    models: Callable[[], ModelClasses] = ModelClasses.default
    max_turns: int | None = None
    #: 1 ターンの上限（秒）。過ぎたら interrupt を送る
    timeout: float | None = None


@dataclass
class _Turn:
    #: 知らせのプロンプト。新しいセッションに替えたら、これから起こし直す
    notice: StagePrompt
    #: 起こしたイベント。判断が確定したら、起こし終えたものとして控える（落ちた後に起こし直さない）
    source: EventId
    #: 対象リポジトリ（起こす時点にメインループのスレッドで取った値）
    repository: str
    #: 次に送るプロンプト。差し戻しなら理由だけ（同じセッションの続きなので、知らせは覚えている）
    prompt: StagePrompt
    #: 起こす時点に Run が呼び直されていた回数（Panic の id に入れる）
    resumes: int = 0
    #: 起こす時点にメインループのスレッドで取った、クラスごとのモデルと effort
    models: ModelClasses = field(default_factory=ModelClasses.default)
    corrections: int = 0


@dataclass(frozen=True)
class _GaveUp:
    """差し戻しと呼び直しを使い切った。1 段上へ上げる理由。"""

    reason: str


@dataclass
class _State:
    queue: deque[_Turn] = field(default_factory=deque)
    busy: bool = False


def _answered(outcome: AgentOutcome) -> bool:
    """ターンが終わったか（判断の形は問わない）。result が来て誤りでなければ、構造化された結果が
    無くても終わったものとし、落ちたとはみなさない（差し戻して返させる）。"""
    return (
        outcome.rate_limited
        or outcome.structured is not None
        or (outcome.ending is Ending.RESULT and not outcome.is_error)
    )


class Sessions:
    """`sessions/<名前>.json` の控え。セッション id と、起こし終えたイベント。"""

    def __init__(self, paths: RunPaths) -> None:
        self._paths = paths
        self._lock = threading.Lock()

    def _read(self, supervisor: Supervisor) -> dict[str, Any]:
        path = self._paths.supervisor_session(supervisor.name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, supervisor: Supervisor, data: Mapping[str, Any]) -> None:
        path = self._paths.supervisor_session(supervisor.name)
        write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def session(self, supervisor: Supervisor) -> tuple[SessionId, bool]:
        """使うセッションと、続きから起こすか。無ければ新しく決めて控える。"""
        with self._lock:
            data = self._read(supervisor)
            if isinstance(data.get("session"), str):
                return SessionId(data["session"]), bool(data.get("started"))
            session = SessionId(str(uuid.uuid4()))
            self._write(supervisor, {**data, "session": session.value, "started": False})
            return session, False

    def started(self, supervisor: Supervisor, session: SessionId) -> None:
        with self._lock:
            data = self._read(supervisor)
            if data.get("session") == session.value:
                self._write(supervisor, {**data, "started": True})

    def replace(self, supervisor: Supervisor) -> None:
        """今のセッションを捨てる。次は新しいセッションで起こす。"""
        with self._lock:
            data = self._read(supervisor)
            data.pop("session", None)
            data.pop("started", None)
            self._write(supervisor, data)

    # --- 知らせ ---
    #
    # 統括を起こした反応は、ターンを別のスレッドに渡してすぐ返り、チェックポイントが進む。ターンの
    # 途中で driver が落ちる・パニックすると、そのイベントはもう配り直されないので、起こした知らせを
    # 控えておき（pending）、判断が確定したか 1 段上へ上げたら起こし終えた（notified）に移す。
    # 呼び直された driver は、控えに残った知らせで起こし直す（`SupervisorRunner.restore`）

    def notified(self, supervisor: Supervisor, source: EventId) -> bool:
        with self._lock:
            return source.value in self._read(supervisor).get("notified", [])

    def add_pending(self, supervisor: Supervisor, source: EventId) -> None:
        with self._lock:
            data = self._read(supervisor)
            pending = data.get("pending", [])
            if source.value not in pending:
                self._write(supervisor, {**data, "pending": [*pending, source.value]})

    def mark_notified(self, supervisor: Supervisor, source: EventId) -> None:
        with self._lock:
            data = self._read(supervisor)
            pending = [p for p in data.get("pending", []) if p != source.value]
            done = [*data.get("notified", []), source.value]
            self._write(supervisor, {**data, "pending": pending, "notified": done})

    def pending(self) -> list[tuple[Supervisor, EventId]]:
        """控えに残った知らせ（統括ごとに、起こした順）。"""
        found: list[tuple[Supervisor, EventId]] = []
        for path in sorted(self._paths.sessions.glob("*.json")):
            supervisor = _supervisor_named(path.stem)
            if supervisor is None:
                continue
            with self._lock:
                pending = self._read(supervisor).get("pending", [])
            found += [(supervisor, EventId(p)) for p in pending]
        return found


def _supervisor_named(name: str) -> Supervisor | None:
    if name == Supervisor.run().name:
        return Supervisor.run()
    if name.startswith("task-"):
        try:
            return Supervisor.of(TaskId(name.removeprefix("task-")))
        except ValueError:
            return None
    return None


class SupervisorRunner:
    def __init__(
        self,
        runtime: AgentRuntimeLike,
        inbox: Inbox,
        setting: SupervisorSetting,
    ) -> None:
        self._runtime = runtime
        self._inbox = inbox
        self._setting = setting
        self.sessions = Sessions(setting.paths)
        self._states: dict[Supervisor, _State] = {}
        #: `_running` と `_closed` を守る。閉じたかを見てからプロセスを始めるまでを 1 つにする
        self._lock = threading.Lock()
        #: 走っている `claude -p`（id → プロセス）。止めるときに使う
        self._running: dict[int, AgentProcessLike] = {}
        self._closed = False
        #: ターンを回すスレッド。daemon なので、driver が待たずに終わると統括の claude が残る（`join`）
        self._threads: list[threading.Thread] = []
        #: この driver で起こした（並べた）知らせ
        self._queued: set[EventId] = set()

    # --- メインループのスレッド ---

    def wake(self, supervisor: Supervisor, prompt: StagePrompt, source: EventId) -> None:
        """統括を起こす。走っている間なら、前のターンの判断が確定してから起こす。

        同じイベントで 2 回起こさない（落ちた後の配り直しで、もう判断を返した知らせを受けた・控えから
        起こし直した知らせを、配り直しでもう一度受けた）。
        """
        if source in self._queued or self.sessions.notified(supervisor, source):
            return
        self._queued.add(source)
        self.sessions.add_pending(supervisor, source)
        state = self._states.setdefault(supervisor, _State())
        state.queue.append(
            _Turn(
                prompt,
                source,
                self._setting.repository(),
                prompt,
                self._setting.resumes(),
                self._setting.models(),
            )
        )
        self._next(supervisor)

    def restore(
        self,
        prompt: Callable[[Supervisor, EventId], StagePrompt | None],
        reported: Callable[[EventId], bool] = lambda _: False,
    ) -> None:
        """控えに残った知らせで起こし直す。`prompt` は、そのイベントの知らせのプロンプトを組む。

        `reported` は、その知らせに応じなかったことを Run がもう受けたか（`Run.reported_failure`）。
        受けていれば、上げ終えた後の始末（`_gave_up`）の前に落ちたので、起こさずに始末だけする。
        """
        for supervisor, source in self.sessions.pending():
            if reported(source):
                self.sessions.mark_notified(supervisor, source)
                self.sessions.replace(supervisor)
                continue
            built = prompt(supervisor, source)
            if built is not None:
                self.wake(supervisor, built, source)

    def _next(self, supervisor: Supervisor) -> None:
        state = self._states[supervisor]
        with self._lock:
            if state.busy or not state.queue or self._closed:
                return
            turn = state.queue.popleft()
            state.busy = True
        ticket = self._inbox.expect()
        thread = threading.Thread(
            target=self._turn,
            args=(supervisor, turn, ticket),
            name=f"supervisor-{supervisor.name}",
            daemon=True,
        )
        with self._lock:
            self._threads.append(thread)
        thread.start()

    def _settled(self, supervisor: Supervisor, turn: _Turn, reason: str | None) -> None:
        """判断を置き換えたコマンドを、メインループが処理し終えた。"""
        state = self._states[supervisor]
        state.busy = False
        if reason is None:
            self._done(supervisor, turn)
        elif turn.corrections < MAX_CORRECTIONS:
            # 拒まれた判断は、同じセッションに理由を返して直させる。ほかの知らせより先に
            turn.corrections += 1
            turn.prompt = StagePrompt(render_rejection(reason))
            state.queue.appendleft(turn)
        else:
            state.busy = True
            failure = f"判断が {MAX_CORRECTIONS} 回差し戻しても拒まれた: {reason}"
            self._report(supervisor, turn, failure, self._inbox.expect())
            return
        self._next(supervisor)

    def _gave_up(self, supervisor: Supervisor, turn: _Turn, reason: str | None) -> None:
        """1 段上へ上げ終えた。知らせは上げたエスカレーションが持つので、起こし終えたと控える。"""
        if reason is not None:
            log.error("%s が応じなかったことを上げられなかった: %s", supervisor.name, reason)
        self._done(supervisor, turn)
        # 起こし直すときは、新しいセッションで知らせから始める
        self.sessions.replace(supervisor)
        self._release(supervisor)

    def _done(self, supervisor: Supervisor, turn: _Turn) -> None:
        self.sessions.mark_notified(supervisor, turn.source)

    def _release(self, supervisor: Supervisor) -> None:
        """判断を出さずにターンを終えた（止めた・パニックした・上げた）。控えは残すことがある。"""
        self._states[supervisor].busy = False
        self._next(supervisor)

    def shutdown(self) -> None:
        """走っている統括を止める（パニック・driver の終わり）。札は返さない。"""
        with self._lock:
            self._closed = True
            running = list(self._running.values())
        for process in running:
            try:
                process.interrupt("driver を止める")
            except Exception:  # 止める途中の誤りで、ほかを止め損ねない
                log.exception("統括のプロセスを止められなかった")

    def join(self, timeout: float) -> None:
        """`shutdown` の後、ターンを回すスレッド（とその claude）が終わるまで待つ。`timeout` は全体の上限。"""
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    # --- 別のスレッド ---

    def _turn(self, supervisor: Supervisor, turn: _Turn, ticket: Ticket) -> None:
        try:
            decided = self._decide(supervisor, turn)
        except Exception as error:
            log.exception("%s を起こす途中で落ちた", supervisor.name)
            decided = _GaveUp(f"起こす途中で落ちた: {type(error).__name__}: {error}")
        if decided is None:
            # driver を止めた。控えは残し、呼び直した driver が起こし直す
            ticket.cancel(settled=lambda _: self._release(supervisor))
        elif isinstance(decided, _GaveUp):
            self._report(supervisor, turn, decided.reason, ticket)
        elif isinstance(decided, Panic):
            with self._lock:
                # パニックの後は、どの統括の次のターンも起こさない
                self._closed = True
            ticket.submit(decided, settled=lambda _: self._release(supervisor))
        else:
            ticket.submit(decided, settled=lambda reason: self._settled(supervisor, turn, reason))

    def _report(self, supervisor: Supervisor, turn: _Turn, reason: str, ticket: Ticket) -> None:
        """応じなかったことを Run に渡す（どこへ上げるかは Run が決める）。"""
        log.error("%s が知らせ %s に応じなかった: %s", supervisor.name, turn.source, reason)
        command = ReportSupervisorFailure(
            # 呼び直された回数は入れない。同じ知らせで 2 度目に届いたら弾く（上げは 1 つでよい）。答えた
            # 後に起こし直すのは回答のイベントが知らせになるので、id は重ならない
            command_id=CommandId.derived(turn.source, f"supervisor-failed-{supervisor.name}"),
            issuer=Issuer.driver(),
            supervisor=supervisor.task,
            notice=turn.source,
            reason=reason,
        )
        ticket.submit(command, settled=lambda refused: self._gave_up(supervisor, turn, refused))

    def _decide(self, supervisor: Supervisor, turn: _Turn) -> Command | _GaveUp | None:
        """判断を 1 つ得てコマンドにする。形が合わなければ、同じセッションに差し戻して聞き直す。

        None なら driver を止めた。
        """
        while True:
            outcome = self._call(supervisor, turn)
            if outcome is None or isinstance(outcome, _GaveUp):
                return outcome
            if outcome.rate_limited:
                return Panic(
                    command_id=CommandId.derived(
                        turn.source, f"supervisor-panic-{supervisor.name}", turn.resumes
                    ),
                    issuer=Issuer.driver(),
                    cause=f"利用枠の上限（{supervisor.name}）",
                )
            try:
                return self._command(supervisor, turn, outcome)
            except DecisionError as error:
                turn.corrections += 1
                if turn.corrections > MAX_CORRECTIONS:
                    return _GaveUp(f"判断の形が {MAX_CORRECTIONS} 回差し戻しても合わない: {error}")
                turn.prompt = StagePrompt(render_rejection(str(error)))

    def _call(self, supervisor: Supervisor, turn: _Turn) -> AgentOutcome | _GaveUp | None:
        """ターンを終えるまで走らせる。None なら driver を止めた。

        続けたセッションで MAX_ATTEMPTS 回続けて落ちたら、新しいセッションに替えて、知らせから
        FRESH_ATTEMPTS 回起こす。それでも落ちたら諦める。
        """
        reason = ""
        for fresh, attempts in ((False, MAX_ATTEMPTS), (True, FRESH_ATTEMPTS)):
            if fresh:
                self.sessions.replace(supervisor)
                turn.prompt = turn.notice
            for attempt in range(1, attempts + 1):
                outcome = self._run_once(supervisor, turn)
                if outcome is None:
                    return None
                if _answered(outcome):
                    return outcome
                # 実行器（`_llm_evidence`）と同じく、こちらが止めた理由を先に見る
                reason = (
                    outcome.interrupted or outcome.text or outcome.stderr or outcome.ending.value
                )
                log.warning(
                    "%s が判断を返さずに落ちた（%s %d 回目）: %s",
                    supervisor.name,
                    "新しいセッションで" if fresh else "",
                    attempt,
                    reason,
                )
        return _GaveUp(f"判断を返さずに落ち続けた（新しいセッションでも）: {reason}")

    def _run_once(self, supervisor: Supervisor, turn: _Turn) -> AgentOutcome | None:
        paths = self._setting.paths
        session, resume = self.sessions.session(supervisor)
        choice = turn.models.of(supervisor.model_class)
        call = AgentCall(
            prompt=turn.prompt.text,
            cwd=str(paths.root),
            session=session,
            resume=resume,
            log_path=str(paths.supervisor_log(supervisor.name)),
            json_schema=schema_text(
                "supervisor-run" if supervisor.task is None else "supervisor-task"
            ),
            agent=choice.agent,
            run_dir=str(paths.root),
            model=choice.model.value,
            effort=choice.effort.value,
            max_turns=self._setting.max_turns,
            settings=str(paths.guard),
            system_append=turn.prompt.system_append,
            env=stage_env(
                SUPERVISOR_GUARD,
                guard_context(tree=paths.root, run_dir=paths.root, target_repo=turn.repository),
            ),
            timeout=self._setting.timeout,
        )
        with self._lock:
            # 止めた後に始めない。閉じたかを見るのと、止める相手に足すのを同じ錠の中で行う
            if self._closed:
                return None
            process = self._runtime.start(call)
            self._running[id(process)] = process
        try:
            outcome = process.wait()
        finally:
            with self._lock:
                self._running.pop(id(process), None)
        if outcome.ending is not Ending.NO_RESULT or resume:
            self.sessions.started(supervisor, session)
        with self._lock:
            if self._closed:
                return None
        return outcome

    def _command(self, supervisor: Supervisor, turn: _Turn, outcome: AgentOutcome) -> Command:
        session = outcome.session
        command_id = CommandId.derived(turn.source, f"supervisor-{supervisor.name}")
        if supervisor.task is None:
            return to_command(
                RUN_DECISIONS,
                outcome.structured,
                command_id=command_id,
                issuer=Issuer.run_supervisor(session),
            )
        return to_command(
            TASK_DECISIONS,
            outcome.structured,
            command_id=command_id,
            issuer=Issuer.task_supervisor(supervisor.task, session),
            task=supervisor.task,
        )
