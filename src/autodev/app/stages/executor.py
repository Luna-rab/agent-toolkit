"""ステージの実行器。

**実行器は判断しない。** ステージを起動し、証拠を集めて `ReportStageResult` にするだけで、完了・失敗・
エスカレーションのどれにするかは `Task.handle` が決める。何を集めるかは
`StageSpec` の宣言（`produces`・`may_produce`・`expects`・`result`）と `VerifySelector` が決める。

```mermaid
sequenceDiagram
    participant T as Task（メインループ）
    participant R as 反応（段 5b1）
    participant X as 実行器
    participant S as ステージ（claude -p・決定的な中身）
    T->>R: StageRequested
    R->>X: begin(execution, 札)
    X-->>T: BeginStage（HEAD・セッション id）
    T->>R: StageStarted（再開の ResumeStage・ResumeInterrupted でも）
    R->>X: run(execution, 札)
    X->>S: 起動（cwd・プロンプト・ガード・スキーマ）
    S-->>X: 終わった
    X-->>T: ReportStageResult（証拠）／Panic
```

- `begin`・`run`・`interrupt`・`restart` は、反応の中（メインループのスレッド）で呼ぶ。集約を写し取るのは
  呼ばれたその場だけで、別のスレッドにはスナップショット（`StageContext`）だけを渡す
- `begin`・`restart`・`abort_rebase` は、worktree ごとの順番待ちのスレッドで、呼ばれた順に処理する。
  走らせ直す実行の worktree を戻す（`restart`）のを、新しい実行の HEAD を取る（`begin`）より先に済ませる
  ためである。止めた実行が終わるのを待つのも worktree ごとで、ほかの worktree のタスクは待たせない
- `run` は実行ごとに 1 本のスレッドで走る（ReviewLoop の見る役は並列に走る）
- 始められなかった実行（worktree を戻せない・HEAD が取れない・止めた実行が終わらない）は、
  `ReportBeginFailure` で Task に渡す。やり直すか上げるかは Task が決める
- 呼び直し（配り直した StageRequested・StageStarted）で、もう始めた・済んだ実行を走らせない。実行の
  状態が求めるものと違えば、札を返さずに cancel する
- 利用枠の上限に当たったら、証拠ではなく `Panic` を返す（インフラのエラー）
- `--resume` で起こしたか・claude が init を出したか・result を返さずに自分で終わったかを、証拠で
  返す。続けられなかったか（作り直すか）は Task が決める
- プロンプトを組む・走らせる途中で実行器が落ちても、エラーの証拠を札で返す（札が返らないと、
  メインループは結果を待ち続ける）
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ...adapters.agent.types import AgentCall, AgentOutcome, Ending, Progress
from ...adapters.claude.guard import ask_question, guard_context, stage_env, write_hook_settings
from ...adapters.claude.schema import load_schema, normalize_nulls, violations
from ...adapters.github.forge import Forge
from ...adapters.github.git import Git
from ...adapters.process.command import StopScope, stoppable
from ...adapters.process.process import ProcessRunner
from ...domain import codec
from ...domain.aggregates.base import Aggregate
from ...domain.aggregates.task import ExecutionStatus
from ...domain.commands.base import Command
from ...domain.commands.run import Panic
from ...domain.commands.task import BeginStage, ReportBeginFailure, ReportStageResult
from ...domain.guard import cut_off_by_denials
from ...domain.stages.catalog import StageSpec
from ...domain.stages.kinds import BodyTarget, ResultField, StageMode
from ...domain.stages.results import has_report
from ...domain.value_objects.artifact_kind import ArtifactKind
from ...domain.value_objects.artifact_ref import ArtifactRef
from ...domain.value_objects.base import InvalidValue
from ...domain.value_objects.command_id import CommandId
from ...domain.value_objects.commit_sha import CommitSha
from ...domain.value_objects.cut_point import CutPoint
from ...domain.value_objects.deferred_call import DeferredCall
from ...domain.value_objects.evidence import Evidence
from ...domain.value_objects.execution_id import ExecutionId
from ...domain.value_objects.issuer import Issuer
from ...domain.value_objects.overview_pr_title import OverviewPrTitle
from ...domain.value_objects.pointers import Pointers
from ...domain.value_objects.session_id import SessionId
from ...domain.value_objects.stage_exit import StageExit
from ...domain.value_objects.stream_id import StreamId
from ...domain.value_objects.task_id import TaskId
from ...domain.value_objects.verify_result import VerifyResult
from ...infra.files import utc_now, write_atomic
from ...infra.status.status import remove_progress, write_progress
from ..driving.mainloop import Ticket
from . import files, outputs
from .programs.common import (
    ProgramOutcome,
    Tools,
    VerifyRunner,
    own_commits,
    resolve_cut_point,
    verify_tree,
)
from .programs.registry import run_program
from .prompts.assets import asset_name, package_root
from .prompts.render import render_shape_rejection
from .stage_context import ResumeMode, RunSetting, StageContext, StagePrompt, run_setting, snapshot

if TYPE_CHECKING:
    from ..driving.driver import DriverParts

log = logging.getLogger(__name__)

_A = ArtifactKind
_F = ResultField


# --- 受け口 ---


class StageExecutor(Protocol):
    """反応（段 5b1）が呼ぶ実行器の受け口。どれもメインループのスレッドで呼ぶ。札は反応が
    `inbox.expect(execution)` で取って渡す。"""

    def begin(self, execution: ExecutionId, ticket: Ticket) -> None:
        """StageRequested を受けて。HEAD とセッション id を集め、BeginStage を札で返す。"""
        ...

    def run(self, execution: ExecutionId, ticket: Ticket) -> None:
        """StageStarted を受けて。走らせ、ReportStageResult（利用枠の上限なら Panic）を札で返す。"""
        ...

    def interrupt(self, execution: ExecutionId) -> None:
        """StageInterrupted を受けて。claude に interrupt を送り、一定時間で返らなければ kill する。
        止めた実行の結果は返さない（その札は cancel する）。"""
        ...

    def restart(self, execution: ExecutionId, start_commit: CommitSha | None) -> None:
        """ExecutionRestarted を受けて。worktree を始めた時点のコミットへ戻す。続く begin より先に済む。"""
        ...

    def abort_rebase(self, task: TaskId) -> None:
        """IntegrationFailed を受けて。そのタスクの worktree の途中の rebase を取りやめる。続く begin より先に済む。"""
        ...

    def join(self, timeout: float = 30.0) -> None:
        """driver が終わる前に、止めた実行の子プロセスが終わるまで待つ（メインループのスレッドで呼ぶ）。"""
        ...


class StagePrompts(Protocol):
    """プロンプトの組み立て（段 5b1）。実行器が run の中で、メインループのスレッドで呼ぶ。"""

    def prompt(
        self, context: StageContext, aggregates: Mapping[StreamId, Aggregate]
    ) -> StagePrompt: ...

    def continuation(self, context: StageContext) -> str:
        """interrupt で止めた実行を `--resume` で続けるときの、短い続きの指示（実測が無い）。"""
        ...


class AgentProcessLike(Protocol):
    def wait(self) -> AgentOutcome: ...
    def interrupt(self, reason: str) -> None: ...
    def kill(self) -> None: ...


class AgentStarter(Protocol):
    """`adapters.agent.router.AgentRouter` の受け口（検査で偽物に差し替える）。"""

    def start(
        self, call: AgentCall, on_progress: Callable[[Progress], None] | None = None
    ) -> AgentProcessLike: ...


# --- 実行器 ---


#: 止めた実行が終わるのを、同じ worktree の次の仕事が 1 回に待つ上限（秒）
STOP_WAIT_SECONDS = 120.0
#: 形だけが違う結果を、1 回の試みの中で返し直させる上限。使い切ったら試みを失敗にし、ドメインのやり直しへ回す
SHAPE_CORRECTIONS = 2
#: begin が止めた実行を待つ回数。待ち切れなければ始めずに頼み直し（順番待ちの後ろへ回す）、この回数を
#: 使い切ったら始められなかったことを Task に渡す（ReportBeginFailure）
STOP_WAIT_ROUNDS = 3


@dataclass(eq=False)
class _Live:
    """走っている実行 1 つ（同じ実行を止めた後に再開すると、別のものになる）。"""

    #: ステージの cwd
    tree: Path
    process: AgentProcessLike | None = None
    #: interrupt で止めた（結果を返さない）
    stopped: bool = False
    #: 走り終えて、走っている実行から外した
    done: threading.Event = field(default_factory=threading.Event)
    #: この実行のスレッドが流す子プロセスを止める口
    scope: StopScope = field(default_factory=StopScope)


class Executor:
    """`StageExecutor` の実装。"""

    def __init__(
        self,
        *,
        setting: RunSetting | Callable[[], RunSetting],
        aggregates: Callable[[], Mapping[StreamId, Aggregate]],
        prompts: StagePrompts,
        runtime: AgentStarter,
        git: Git | None = None,
        forge: Forge | None = None,
        runner: VerifyRunner | None = None,
        clock: Callable[[], str] = utc_now,
        new_session: Callable[[], SessionId] = lambda: SessionId(str(uuid.uuid4())),
        skill: Path | None = None,
    ) -> None:
        self._setting = setting
        self._resolved: RunSetting | None = setting if isinstance(setting, RunSetting) else None
        self._aggregates = aggregates
        self._prompts = prompts
        self._runtime = runtime
        self._given_git = git
        self._forge = forge or Forge()
        self._runner: VerifyRunner = runner or ProcessRunner()
        self._clock = clock
        self._new_session = new_session
        self._skill = skill or package_root()
        self._lock = threading.Lock()
        #: 走っている実行（止めたものは _stopping へ移す。同じ実行を再開できるように）
        self._live: dict[ExecutionId, _Live] = {}
        #: 止めて、まだ終わっていない実行
        self._stopping: list[_Live] = []
        #: 実行を止めた後に、残った `index.lock` をまだ確かめていない worktree。止めた走りは終わると
        #: すぐ `_stopping` から外れるので、それだけでは次の仕事が lock を消すべきかを知れない
        self._unswept: set[Path] = set()
        self._threads: list[threading.Thread] = []
        #: worktree ごとの順番待ち
        self._lanes: dict[Path, queue.Queue[Callable[[], None] | None]] = {}
        self._lane_threads: dict[Path, threading.Thread] = {}

    @property
    def setting(self) -> RunSetting:
        """ランの間変わらない値。RunStarted が確定するまで組めないので、初めて要るときに組む。"""
        resolved = self._resolved
        if resolved is None:
            source = self._setting
            resolved = source if isinstance(source, RunSetting) else source()
            self._resolved = resolved
        return resolved

    @property
    def _git(self) -> Git:
        """対象リポジトリの Git。渡されなければ、RunStarted の対象リポジトリで組む。"""
        if self._given_git is None:
            self._given_git = Git(self.setting.repository)
        return self._given_git

    @property
    def _tools(self) -> Tools:
        return Tools(
            setting=self.setting,
            git=self._git,
            forge=self._forge,
            runner=self._runner,
            clock=self._clock,
        )

    # --- 受け口 ---

    def begin(self, execution: ExecutionId, ticket: Ticket) -> None:
        context = self._snapshot(execution)
        if context is None or context.status is not ExecutionStatus.REQUESTED:
            # 写し取れなかった実行は requested のまま残り、呼び直した driver が始めるのを頼み直す
            ticket.cancel()
            return
        self._enqueue(context.tree, lambda: self._begin(context, ticket))

    def run(self, execution: ExecutionId, ticket: Ticket) -> None:
        started = self.setting.paths.stage_log(execution).is_file()
        context = self._snapshot(execution, agent_started=started)
        if context is None or context.status is not ExecutionStatus.RUNNING:
            ticket.cancel()
            return
        with self._lock:
            if execution in self._live:
                # 同じ実行がもう走っている（配り直した StageStarted）
                ticket.cancel()
                return
            live = self._live[execution] = _Live(context.tree)
        try:
            prompt = self._prompt(context)
        except Exception as error:
            # プロンプトを組めない実行も、札を返して Task に失敗を決めさせる（札が返らないと、
            # メインループは結果を待ち続ける）
            log.exception("%s のプロンプトを組めなかった", execution)
            self._settle(execution, live)
            with ticket:
                ticket.submit(self._report(context, _error(error), None, llm=True))
            return
        thread = threading.Thread(
            target=self._run,
            args=(context, prompt, ticket, live),
            name=f"stage-{execution}",
            daemon=True,
        )
        with self._lock:
            self._threads.append(thread)
        thread.start()

    def interrupt(self, execution: ExecutionId) -> None:
        with self._lock:
            live = self._live.pop(execution, None)
            if live is None:
                return
            # 走っている実行から外す。止めた後に同じ実行を再開したら（ResumeStage）、新しく走らせる
            live.stopped = True
            self._stopping.append(live)
            self._unswept.add(live.tree)
            process = live.process
        if process is not None:
            process.interrupt("driver が止めた")
        # 決定的なステージ（Gate・統合検査・Rebase など）の子プロセスも止める。止め終えるまで、同じ
        # worktree の次の仕事は待つ（`_wait_stopped`）
        live.scope.stop()

    def restart(self, execution: ExecutionId, start_commit: CommitSha | None) -> None:
        context = self._snapshot(execution)
        if context is None or start_commit is None:
            return
        self._enqueue(context.tree, lambda: self._restart(context, start_commit))

    def abort_rebase(self, task: TaskId) -> None:
        tree = self.setting.paths.tree_of(task)
        self._enqueue(tree, lambda: self._abort_rebase(tree))

    # --- 検査と終わり方のため ---

    def join(self, timeout: float = 30.0) -> None:
        """順番待ちと、走っている実行が終わるまで待つ。`timeout` は全体の上限で、待つものの数だけ
        延ばさない（driver が終わる前の待ちが、走っていた実行の数に比例して延びない）。"""
        deadline = time.monotonic() + timeout
        with self._lock:
            trees = list(self._lanes)
        marks = []
        for tree in trees:
            mark = threading.Event()
            self._enqueue(tree, mark.set)
            marks.append(mark)
        for mark in marks:
            mark.wait(max(0.0, deadline - time.monotonic()))
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def interrupt_all(self) -> None:
        """パニックのとき。走っている実行をすべて止める。"""
        with self._lock:
            running = list(self._live)
        for execution in running:
            self.interrupt(execution)

    # --- 中 ---

    def _snapshot(
        self, execution: ExecutionId, *, agent_started: bool = False
    ) -> StageContext | None:
        """写し取れなければ None（呼んだ側が札を返す）。"""
        try:
            return snapshot(
                self._aggregates(), execution, self.setting, agent_started=agent_started
            )
        except KeyError:
            log.warning("%s は Task に無いので走らせない", execution)
        except Exception:
            log.exception("%s の事実を写し取れなかった", execution)
        return None

    def _prompt(self, context: StageContext) -> StagePrompt | None:
        """LLM のステージに渡すプロンプト。defer から続けるときは渡さない。"""
        if context.spec.mode is not StageMode.LLM or context.resume is ResumeMode.DEFERRED:
            return None
        if context.resume is ResumeMode.INTERRUPTED:
            return StagePrompt(self._prompts.continuation(context))
        return self._prompts.prompt(context, self._aggregates())

    def _settle(self, execution: ExecutionId, live: _Live) -> None:
        """走り終えた。同じ worktree の次の仕事が、止め終えるのを待っている。"""
        with self._lock:
            if self._live.get(execution) is live:
                del self._live[execution]
            self._stopping = [other for other in self._stopping if other is not live]
        live.done.set()

    def _enqueue(self, tree: Path, work: Callable[[], None]) -> None:
        """`tree` の順番待ちに足す。worktree ごとに 1 本のスレッドが、足した順に処理する。"""
        with self._lock:
            lane = self._lanes.setdefault(tree, queue.Queue())
            thread = self._lane_threads.get(tree)
            if thread is None or not thread.is_alive():
                thread = threading.Thread(
                    target=self._drain, args=(lane,), name=f"executor-lane-{tree.name}", daemon=True
                )
                self._lane_threads[tree] = thread
                thread.start()
        lane.put(work)

    def _drain(self, lane: queue.Queue[Callable[[], None] | None]) -> None:
        while True:
            work = lane.get()
            if work is None:
                return
            try:
                work()
            except Exception:
                # 監視の誤りでドライバを止めない。札は work の中で返す
                log.exception("実行器の順番待ちの仕事で落ちた")

    def _issuer(self, execution: ExecutionId) -> Issuer:
        return Issuer.executor(execution)

    def _command_id(self, execution: ExecutionId, what: str) -> CommandId:
        # 結果は配り直されないので、再開のたびに新しい id にする（同じ id は 2 回目が捨てられる）
        return CommandId(f"{execution}/{what}/{uuid.uuid4().hex[:12]}")

    def _begin(self, context: StageContext, ticket: Ticket, waited: int = 0) -> None:
        execution = context.execution
        if not self._wait_stopped(context.tree):
            # 止めた実行がまだ終わっていない worktree では始めない。順番待ちの後ろへ回して待ち直し、
            # 使い切ったら始められなかったことを Task に渡す
            if waited + 1 < STOP_WAIT_ROUNDS:
                log.warning("%s の worktree で止めた実行が終わらないので、待ち直す", execution)
                self._enqueue(context.tree, lambda: self._begin(context, ticket, waited + 1))
                return
            seconds = int(STOP_WAIT_SECONDS * STOP_WAIT_ROUNDS)
            with ticket:
                ticket.submit(
                    self._begin_failure(
                        context, f"同じ worktree で止めた実行が {seconds} 秒で終わらない"
                    )
                )
            return
        with ticket:
            try:
                if context.spec.abandons_rebase and (context.tree / ".git").exists():
                    # 途中の rebase の HEAD（切り離した、載せ直しかけのコミット）を始めた時点にすると、
                    # restores_start が流す前にそこへ戻し、タスクのコミットをブランチから落とす
                    self._git.rebase_abort(context.tree)
                if context.reset_to is not None:
                    # 作り直した実行の次の試み。restart の反応が戻す前に driver が落ちていても、戻して
                    # から HEAD を取る（戻していない HEAD から始めない）
                    self._reset(context, context.reset_to)
                head = self._head(context)
            except Exception as error:
                log.exception("%s を始められなかった", execution)
                ticket.submit(self._begin_failure(context, f"{type(error).__name__}: {error}"))
                return
            session: SessionId | None = None
            if context.spec.mode is StageMode.LLM:
                session = context.session or self._new_session()
            ticket.submit(
                BeginStage(
                    command_id=CommandId(f"{execution}/begin"),
                    issuer=self._issuer(execution),
                    task=execution.task,
                    execution=execution,
                    head=head,
                    session=session,
                )
            )

    def _begin_failure(self, context: StageContext, error: str) -> ReportBeginFailure:
        execution = context.execution
        return ReportBeginFailure(
            command_id=CommandId(f"{execution}/begin-failed"),
            issuer=self._issuer(execution),
            task=execution.task,
            execution=execution,
            error=error,
        )

    def _head(self, context: StageContext) -> CommitSha:
        """ステージの cwd の HEAD。まだ無い（CutBranch がこれから切る）なら、切る元のコミット。"""
        git = self._git
        if (context.tree / ".git").exists():
            return git.head(context.tree)
        point = context.job.cut_point if context.job is not None else None
        if point is not None:
            found = git.rev_parse(git.repo, resolve_cut_point(self._tools, point))
            if found is not None:
                return found
        for rev in (*CutPoint(self.setting.base, run_base=True).refs(), "HEAD"):
            if (found := git.rev_parse(git.repo, rev)) is not None:
                return found
        raise RuntimeError(f"{context.execution} の HEAD が決まらない")

    def _reset(self, context: StageContext, start_commit: CommitSha) -> None:
        tree = context.tree
        if not (tree / ".git").exists():
            return
        git = self._git
        if git.rebase_in_progress(tree):
            # rebase の途中（git 管理タスクの ResolveConflict）は、解きかけを衝突の状態に戻すだけ
            git.restore_conflicts(tree, context.listed)
        else:
            git.reset_hard(tree, str(start_commit))

    def _restart(self, context: StageContext, start_commit: CommitSha) -> None:
        # 待ち切れなければ戻さない。次の試みの begin が、始める前に戻し直す（Task.reset_before_start）
        if self._wait_stopped(context.tree):
            self._reset(context, start_commit)
        else:
            log.warning(
                "%s の worktree で止めた実行が終わらないので、まだ戻さない", context.execution
            )

    def _abort_rebase(self, tree: Path) -> None:
        # 待ち切れなければ取りやめない。Rebase・CutBranch が、流す前に取りやめる（abandons_rebase）
        if not self._wait_stopped(tree):
            log.warning("%s で止めた実行が終わらないので、まだ rebase を取りやめない", tree)
        elif (tree / ".git").exists():
            self._git.rebase_abort(tree)

    def _stopped(self, live: _Live) -> bool:
        with self._lock:
            return live.stopped

    def _wait_stopped(self, tree: Path, besides: _Live | None = None) -> bool:
        """同じ worktree で止めた実行が終わるまで待つ。待ち切れなければ偽。新しいフローのステージを、止めた
        ステージ（決定的なステージの子プロセスを含む）と同じ worktree で同時に走らせない。

        止めた後にまだ確かめていない worktree（`_unswept`）なら、残った `index.lock` を消す。SIGKILL で
        止めた git は lock を片付けずに終わり、残すと次の git がどれも落ちる。確かめたら印を下ろし、その
        後に現れた lock は消さない。同じ worktree でほかの実行（`besides` は待っている当の実行）が走って
        いるか止めている途中なら、その git の lock かもしれないので消さず、印も残す。
        """
        with self._lock:
            # 走り出した直後に止めた実行は、自分を待つ前に `_stopping` に入っている。自分の終わりは
            # 自分が待ちを抜けるまで来ないので、待つと上限まで待ち、同じ worktree の次の仕事も待たせる
            waiting = [live for live in self._stopping if live.tree == tree and live is not besides]
        for live in waiting:
            if not live.done.wait(STOP_WAIT_SECONDS):
                log.warning("%s で止めた実行が %s 秒で終わらなかった", tree, STOP_WAIT_SECONDS)
                return False
        with self._lock:
            busy = any(
                live.tree == tree and live is not besides
                for live in (*self._live.values(), *self._stopping)
            )
            # 止めた当の走りは片付けない。まだ自分の子プロセスが lock を持っているかもしれず、印は
            # 自分が走り終えた後の次の仕事が下ろす
            itself_stopped = besides is not None and besides.stopped
            sweep = tree in self._unswept and not busy and not itself_stopped
            if sweep:
                self._unswept.discard(tree)
        if sweep and (tree / ".git").exists():
            try:
                if self._git.remove_index_lock(tree):
                    log.warning("%s で止めた実行が残した index.lock を消した", tree)
            except Exception:
                log.exception("%s の index.lock を確かめられなかった", tree)
                with self._lock:
                    self._unswept.add(tree)
        return True

    # --- パス ---

    def _log_path(self, context: StageContext) -> Path:
        return self.setting.paths.stage_log(context.execution)

    def _result_path(self, context: StageContext) -> Path:
        return self.setting.paths.task_results(context.task) / f"{context.execution}.json"

    def _relative(self, path: Path) -> str | None:
        root = self.setting.paths.root
        return path.relative_to(root).as_posix() if path.is_relative_to(root) else None

    def _pointers(self, context: StageContext, *, llm: bool) -> Pointers:
        paths = self.setting.paths
        return Pointers(
            task_dir=paths.relative(paths.task_dir(context.task)),
            result=self._relative(self._result_path(context)),
            log=self._relative(self._log_path(context)) if llm else None,
            tree=self._relative(context.tree),
            session=context.session,
        )

    # --- 走らせる ---

    def _run(
        self, context: StageContext, prompt: StagePrompt | None, ticket: Ticket, live: _Live
    ) -> None:
        execution = context.execution
        llm = context.spec.mode is StageMode.LLM
        try:
            with ticket:
                self._progress(context, {"stage": execution.stage.value, "state": "running"})
                command: Command | None = None
                if not self._wait_stopped(context.tree, besides=live):
                    # 止めた実行（止めた後に再開したこの実行の前の走りを含む）が、同じ worktree で
                    # まだ終わっていない。並べて走らせない
                    error = RuntimeError("同じ worktree で止めた実行が終わらない")
                    command = self._report(context, _error(error), None, llm=llm)
                elif self._stopped(live):
                    # 待つ前か待つ間に止めた。走らせると claude を余計に起こしてから止めることになり、
                    # 起こした跡（ログ）が残って再開の起こし方（`Task.how_to_start`）を変える
                    pass
                else:
                    try:
                        # interrupt が、このスレッドが流す子プロセス（git・検証コマンド）を止められる
                        with stoppable(live.scope):
                            if llm:
                                command = self._run_llm(context, prompt, live)
                            else:
                                command = self._run_program(context)
                    except Exception as error:
                        log.exception("%s を走らせる途中で落ちた", execution)
                        command = self._report(context, _error(error), None, llm=llm)
                if command is not None and not self._stopped(live):
                    ticket.submit(command)
        finally:
            self._settle(execution, live)
            with self._lock:
                # 止めた後に同じ実行を再開していれば、その進み具合は消さない
                rerun = execution in self._live
            if not rerun:
                remove_progress(self.setting.paths, execution)

    def _progress(self, context: StageContext, body: Mapping[str, Any]) -> None:
        # 進み具合の書き損じでステージを止めない
        with contextlib.suppress(OSError):
            write_progress(self.setting.paths, context.execution, {**body, "updated": utc_now()})

    def _report(
        self,
        context: StageContext,
        evidence: Evidence,
        result: Mapping[str, Any] | None,
        *,
        llm: bool,
        valid: bool = True,
    ) -> ReportStageResult:
        """結果の JSON は、形が違っても results/ に残す（調べる先）。Task へは形が合うものだけを渡す。"""
        if result is not None:
            outputs.write_result(self._result_path(context), result)
        if not valid:
            result = None
        return ReportStageResult(
            command_id=self._command_id(context.execution, "report"),
            issuer=self._issuer(context.execution),
            task=context.task,
            execution=context.execution,
            evidence=evidence,
            pointers=self._pointers(context, llm=llm),
            result=dict(result) if result is not None else None,
        )

    def _commits(self, context: StageContext) -> int | None:
        if not (context.tree / ".git").exists() or context.base_commit is None:
            return None
        return len(own_commits(context, self._tools, context.tree))

    def _head_and_dirty(
        self, context: StageContext
    ) -> tuple[CommitSha | None, tuple[str, ...] | None]:
        """作業ツリーの HEAD とコミットしていない変更のあるファイル。取れなければ (None, None)。"""
        if not (context.tree / ".git").exists():
            return None, None
        try:
            return self._git.head(context.tree), tuple(self._git.dirty_files(context.tree))
        except Exception:
            log.exception("%s の HEAD と変更を調べられなかった", context.execution)
            return None, None

    # --- 決定的なステージ ---

    def _run_program(self, context: StageContext) -> Command:
        try:
            outcome = run_program(context, self._tools)
        except Exception as error:
            log.exception("%s の中身が落ちた", context.execution)
            return self._report(context, _error(error), None, llm=False)
        return self._report(
            context, self._program_evidence(context, outcome), outcome.result, llm=False
        )

    def _program_evidence(self, context: StageContext, outcome: ProgramOutcome) -> Evidence:
        commits = outcome.commits if outcome.commits is not None else self._commits(context)
        return Evidence(
            StageExit.OK,
            result_valid=True,
            products=outcome.products,
            commits=commits,
            verify=outcome.verify,
            gate=outcome.gate,
            union=outcome.union,
            conflicts=outcome.conflicts,
        )

    # --- LLM のステージ ---

    def _call(self, context: StageContext, prompt: StagePrompt | None) -> AgentCall:
        spec, setting = context.spec, self.setting
        guard = spec.guard
        assert guard is not None and spec.model_class is not None and context.session is not None
        choice = setting.models.of(spec.model_class)
        where = guard_context(
            tree=context.tree,
            run_dir=setting.paths.root,
            target_repo=setting.repository,
            test_globs=setting.test_globs,
            listed=context.listed,
        )
        schema = self._skill / "schemas" / f"{asset_name(context.execution.stage)}.json"
        if not setting.paths.guard.is_file():
            # フックの設定はランの頭で 1 回書けばよい（driver も起動時に書く）
            write_hook_settings(setting.paths.guard)
        return AgentCall(
            prompt=prompt.text if prompt is not None else None,
            cwd=str(context.tree),
            session=context.session,
            resume=context.session_started,
            log_path=str(self._log_path(context)),
            json_schema=schema.read_text(encoding="utf-8"),
            agent=choice.agent,
            run_dir=str(setting.paths.root),
            model=choice.model.value,
            effort=choice.effort.value,
            max_turns=spec.max_turns,
            settings=str(setting.paths.guard),
            system_append=prompt.system_append if prompt is not None else None,
            env=stage_env(guard, where),
        )

    def _run_llm(self, context: StageContext, prompt: StagePrompt | None, live: _Live) -> Command:
        execution = context.execution
        try:
            baseline = self._baseline(context) if context.spec.checks_after else None
            call = self._call(context, prompt)
            process = self._launch(context, call, live)
        except Exception as error:
            log.exception("%s の claude を起こせなかった", execution)
            return self._report(context, _error(error), None, llm=True)
        outcome = self._corrected(context, call, process.wait(), live)
        if outcome.rate_limited:
            return Panic(
                command_id=self._command_id(execution, "panic"),
                issuer=Issuer.driver(),
                cause=f"利用枠の上限に当たった（{execution}）: {(outcome.text or outcome.stderr)[:300]}",
            )
        evidence, result = self._llm_evidence(context, outcome, resumed=call.resume)
        if context.spec.checks_after:
            evidence = self._with_checks_after(context, evidence, result, baseline)
        if context.spec.can_keep:
            # 軽い検査が作業ツリーを変えることがあるので、その後に集める
            head, dirty = self._head_and_dirty(context)
            evidence = replace(evidence, head=head, dirty=dirty)
        return self._report(context, evidence, result, llm=True, valid=evidence.result_valid)

    def _corrected(
        self, context: StageContext, call: AgentCall, outcome: AgentOutcome, live: _Live
    ) -> AgentOutcome:
        """形だけが違う結果を、同じセッションに違いを伝えて返し直させる。返し直しが正しく終わらなければ
        最初の outcome を返す（返し直しの outcome だと、続けられなかったセッションとして作り直される）。"""
        for _ in range(SHAPE_CORRECTIONS):
            if not outcome.initialized or outcome.rate_limited or not _settled(outcome):
                return outcome
            _, problems = self._shape(context, outcome)
            with self._lock:
                stopped = live.stopped
            if not problems or stopped:
                return outcome
            fixed = self._resend(
                context, replace(call, prompt=render_shape_rejection(problems)), live
            )
            if fixed is None:
                return outcome
            # フックの打ち切りはプロセスごとの数で決まるので、足さずに多いほうを残す
            merged = replace(
                fixed,
                initialized=True,
                num_turns=outcome.num_turns + fixed.num_turns,
                hook_denials=max(outcome.hook_denials, fixed.hook_denials),
            )
            # 上限と ask は、最初の結果より先に driver へ渡す（ask は回答の後に同じセッションで続く）
            if fixed.rate_limited or fixed.deferred is not None:
                return merged
            if not _settled(fixed):
                return outcome
            outcome = merged
        return outcome

    def _launch(self, context: StageContext, call: AgentCall, live: _Live) -> AgentProcessLike:
        process = self._runtime.start(call, lambda p: self._on_progress(live, p, context))
        with self._lock:
            live.process = process
            stopped = live.stopped
        if stopped:
            process.interrupt("driver が止めた")
        return process

    def _resend(self, context: StageContext, call: AgentCall, live: _Live) -> AgentOutcome | None:
        try:
            return self._launch(context, replace(call, resume=True), live).wait()
        except Exception:
            log.exception("%s の結果を返し直させられなかった", context.execution)
            return None

    def _baseline(self, context: StageContext) -> tuple[VerifyResult, ...] | None:
        """ステージを始める前の軽い検査の結果。取れていなければ None。

        同じ位置の最初の試み（`baseline_of` が自分）を初めて起こすときだけ、claude を起こす前に流して
        ファイルに書く。差し戻した後の試みと続きからの起動は、最初の試みのファイルを読む。取り直すと、
        差し戻した試みが入れた落ちが「始める前から落ちていた」に入る。
        """
        paths = self.setting.paths
        first = context.baseline_of
        if first is None:
            return None
        if first == context.execution and context.resume is ResumeMode.FRESH:
            results = verify_tree(context, self._tools, context.tree)
            write_atomic(
                paths.baseline(first), json.dumps(codec.to_json(results), ensure_ascii=False)
            )
            return results
        try:
            data = json.loads(paths.baseline(first).read_text(encoding="utf-8"))
            return codec.from_json(tuple[VerifyResult, ...], data)
        except (OSError, ValueError):
            return None

    def _with_checks_after(
        self,
        context: StageContext,
        evidence: Evidence,
        result: Mapping[str, Any] | None,
        baseline: tuple[VerifyResult, ...] | None,
    ) -> Evidence:
        """終わった後の軽い検査を、正しい形の結果で、報告も defer も無いときだけ流して証拠に載せる。"""
        verify = evidence.verify
        if (
            evidence.result_valid
            and evidence.deferred is None
            and result is not None
            and not has_report(context.spec, result)
        ):
            verify = verify_tree(context, self._tools, context.tree)
        return replace(evidence, verify=verify, baseline=baseline)

    def _on_progress(self, live: _Live, progress: Progress, context: StageContext) -> None:
        if live.stopped:
            # 止めた走りの進み具合で、止めた後に再開した同じ実行の進み具合を上書きしない
            return
        self._progress(
            context,
            {
                "stage": context.execution.stage.value,
                "state": "running",
                "turns": progress.turns,
                "lastTool": progress.last_tool,
                "hookDenials": progress.hook_denials,
                "events": progress.events,
            },
        )
        if cut_off_by_denials(progress.hook_denials):
            with self._lock:
                process = live.process
            if process is not None:
                process.interrupt(f"フックに {progress.hook_denials} 回止められた")

    def _llm_evidence(
        self, context: StageContext, outcome: AgentOutcome, *, resumed: bool = False
    ) -> tuple[Evidence, dict[str, Any] | None]:
        """証拠と、結果の JSON（無ければ None）。形が違っても結果は返し、results/ に残す。

        `resumed` は `--resume` で起こしたか。続けられなかったか（作り直すか）は、これと init を受けたか・
        こちらが止めたか・result のターンの数・defer で止まったかから Task が決める。
        """
        result, problems = self._shape(context, outcome)
        finished = (
            outcome.ending is Ending.RESULT and not outcome.is_error and outcome.interrupted is None
        )
        deferred: DeferredCall | None = None
        if outcome.deferred is not None and outcome.deferred.tool_use_id:
            question = ask_question(outcome.deferred.input) or ""
            deferred = DeferredCall(outcome.deferred.tool_use_id, question)
        error: str | None = None
        if not finished:
            error = (
                outcome.interrupted
                or (outcome.text if outcome.is_error else None)
                or outcome.stderr
                or f"claude が {outcome.ending.value}（{outcome.subtype}）で終わった"
            )
        elif problems:
            error = "結果の形が違う: " + "; ".join(problems[:5])
        valid = finished and not problems
        products: tuple[ArtifactRef, ...] = ()
        if valid and deferred is None and result is not None:
            products = self._written(context, result)
        evidence = Evidence(
            StageExit.OK if finished else StageExit.ERROR,
            result_valid=valid,
            products=products,
            commits=self._commits(context),
            deferred=deferred,
            error=error,
            hook_denials=outcome.hook_denials,
            resumed=resumed,
            initialized=outcome.initialized,
            stopped_by_us=outcome.interrupted is not None or outcome.ending is Ending.KILLED,
            num_turns=outcome.num_turns if outcome.ending is Ending.RESULT else None,
        )
        return evidence, result

    def _shape(
        self, context: StageContext, outcome: AgentOutcome
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """結果の JSON（無ければ None）と、出力スキーマや後のステージに合わない所。"""
        schema = load_schema(
            self._skill / "schemas" / f"{asset_name(context.execution.stage)}.json"
        )
        if outcome.structured is None:
            return None, ["構造化出力が無い"]
        normalized = normalize_nulls(dict(outcome.structured), schema)
        result = normalized if isinstance(normalized, dict) else None
        return result, violations(result, schema) or _unusable(context.spec, result)

    def _written(self, context: StageContext, result: Mapping[str, Any]) -> tuple[ArtifactRef, ...]:
        """結果の本文を書き出し、作った成果物の実物を集める。何を見るかは StageSpec が宣言した欄と成果物。"""
        spec, paths, task = context.spec, self.setting.paths, context.task
        declared = spec.produces | spec.may_produce
        found: list[ArtifactRef] = []
        if (design := _text(spec, result, _F.DESIGN)) is not None:
            version = outputs.next_design_version(paths)
            files.write_design(paths, version, design)
            if _A.PROPOSAL in declared:
                found.append(ArtifactRef(_A.PROPOSAL, str(version)))
        if (codemap := _text(spec, result, _F.CODEMAP)) is not None and _A.CODEMAP in declared:
            outputs.write_codemap(paths, codemap)
            found.append(ArtifactRef(_A.CODEMAP, paths.relative(paths.codemap)))
        if (body := _text(spec, result, _F.BODY)) is not None:
            if spec.body is BodyTarget.TASK_PR:
                outputs.write_pr_body(paths, task, body)
                found.append(ArtifactRef(_A.PR_BODY, paths.relative(paths.pr_body(task))))
            elif spec.body is BodyTarget.OVERVIEW_PR:
                outputs.write_overview_body(paths, body)
        if (title := _text(spec, result, _F.TITLE)) is not None:
            outputs.write_overview_title(paths, title)
        awaiting = result.get(_F.AWAITING_EXPECTATIONS.value)
        if (
            _F.AWAITING_EXPECTATIONS in spec.result
            and _A.AWAITING_EXPECTATIONS in declared
            and isinstance(awaiting, list)
            and awaiting
        ):
            outputs.write_awaiting(paths, task, awaiting)
            where = paths.relative(paths.awaiting_expectations(task))
            found.append(ArtifactRef(_A.AWAITING_EXPECTATIONS, where))
        committed = sorted((kind for kind in declared if kind.committed), key=str)
        if committed and context.start_commit is not None and (context.tree / ".git").exists():
            head = self._git.head(context.tree)
            if head != context.start_commit:
                found += [ArtifactRef(kind, str(head)) for kind in committed]
        return tuple(found)


def _settled(outcome: AgentOutcome) -> bool:
    """結果を返して終わった。defer で止まったのは、結果がまだ無いので返し直させない。"""
    return (
        outcome.ending is Ending.RESULT
        and not outcome.is_error
        and outcome.interrupted is None
        and outcome.deferred is None
    )


def _unusable(spec: StageSpec, result: Mapping[str, Any] | None) -> list[str]:
    """スキーマには合うが、後のステージで使えない欄。ここで形の誤りにすれば、書いたステージへ差し戻せる
    （後の CreateOverviewPR・RefreshOverview で落とすと、直すステージが走らない）。"""
    if result is None or _F.TITLE not in spec.result:
        return []
    try:
        OverviewPrTitle.for_run(str(result.get(_F.TITLE.value, "")))
    except InvalidValue as error:
        return [f"$.{_F.TITLE.value}: {error}"]
    return []


def _error(error: Exception) -> Evidence:
    """実行器の中で落ちた証拠。失敗に数えるかは Task が決める。"""
    return Evidence(StageExit.ERROR, result_valid=False, error=f"{type(error).__name__}: {error}")


def _text(spec: StageSpec, result: Mapping[str, Any], field: ResultField) -> str | None:
    """宣言した欄の、空でない本文。"""
    value = result.get(field.value)
    if field in spec.result and isinstance(value, str) and value.strip():
        return value
    return None


def from_parts(parts: DriverParts, runtime: AgentStarter, **repository: Any) -> Executor:
    """組み立ての根から使う形: `Driver(paths, executor=lambda parts: from_parts(parts, runtime))`。

    対象リポジトリ・base・指示は RunStarted から読む（`stage_context.run_setting`）。`repository` は
    リポジトリごとの設定（`quick_checks`・`regression_tests`・`test_globs`・`protected_globs`・`untested_globs`）。
    """
    return Executor(
        setting=lambda: run_setting(parts.paths, parts.history(), **repository),
        aggregates=parts.aggregates,
        prompts=parts.prompts,
        runtime=runtime,
    )
