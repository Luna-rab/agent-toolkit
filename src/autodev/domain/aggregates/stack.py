"""Stack: スタックの形と、git 管理タスクの仕事の順番待ちの列。

ストリームは `stack`。変えるのは git 管理タスクだけ（コマンドの土台が名乗りで確かめる）。

**git 管理タスクの仕事は、種類を問わず（切る・概要 PR を作る・積む・破棄する・stack-top を切り直す・
仕上げ）この 1 本の列に並び、1 つずつ取り出される**。列を Stack に置くのは、
取り出してよいかと、取り出した仕事の相手（base・閉じる所）が、スタックの形（概要 PR があるか・
一番上・破棄の後に閉じ終えたか）で決まるからである。git 管理タスクの統括（プログラム）は、取り
出した仕事（GitJobTaken）から決まった並び（`flow.git_job_flow`）を組むだけで、判断はここに残る。

不変条件:

- 一番下は概要 PR（`overview`）で、その base はランの base。概要 PR を相手にする仕事・一番上に
  載せる仕事（`GitJobKind.needs_overview`）は、概要 PR を作った後でないと取り出さない
- 積むのはスタックの一番上だけ。`entries[n]` の base は `entries[n-1]` のブランチ（`entries[0]` は
  概要ブランチ）。一番上に載せる仕事の base は、取り出したときの一番上
- 処理中の仕事は同時に 1 つ。処理中でなくなるのは、git 管理タスクのフローが終わった・捨てられた
  とき（FinishGitJob）だけ。積んだ・統合に失敗した・外した、では処理中のまま
- **取り下げでも統合の失敗でもない、済ませないとランが終わらない仕事（`GitJobKind.must_finish`）の
  フローが捨てられたら、その仕事を列の先頭へ戻す。** ステージが落ち続けて上げたエスカレーションを
  ラン統括が閉じても、仕事（破棄した所を閉じる・概要 PR を作る など）はまだ済んでいない。消すと、
  閉じ終えるのを待つ数が減らず、ランが終わらない。戻すのは MAX_JOB_RETURNS 回までで、超えたら仕事を
  止め（STUCK）、ラン統括が続ける（RetryGitJob）かやめる（DropGitJob）かを決めるまで、後ろの仕事も
  取り出さない
- 統合の失敗の印は、その失敗のエスカレーションが開いている間だけ立てる。回答が届いて同じ仕事で
  統合をやり直すなら下ろす（RetryIntegration）
- **破棄したら、閉じ終えるまで（UnstackFrom）一番上に載せる仕事（切る・積む）を取り出さない。**
  破棄の仕事は、列の前にある載せる仕事より先に取り出す。閉じる所より上に積み直すタスクは、
  閉じた後に積む列へ戻ってくる（Run の TasksReturnedToQueue）
- **再計画が進んでいる間（PauseStacking から ResumeStacking まで）は、積む仕事（積む・積み直す）を
  取り出さない。** 再計画は stack-top（その時の一番上）を読んで書くので、その間に積むと、読んだ形と
  反映する時の形がずれる。積む列で待っているタスクの範囲が再計画で変わったら、積まずに列から外して
  始め直せる（Run の TasksPlanned.withdraw）。stack-top を切り直す仕事は再計画そのものに要るので止めない
- 積んだ `StackEntry` を変えるのは、破棄したタスクから上を閉じるとき（UnstackFrom）だけ。閉じた所より
  下は変えない
- 一度積んだブランチと PR の番号は、積み直しにも使わない。積み直すタスクは push 済みのブランチを
  force push で書き換えず、新しい名前で切り直す。前に積んだブランチは仕事に載せる
- ステージの結果（概要 PR・積んだ 1 本・閉じた所）の中身の問題は、拒否ではなく ResultRefused で
  返す。拒否にするのは、処理中でない仕事への知らせのような取り違えだけである
"""

from __future__ import annotations

from dataclasses import replace

from ..commands.stack import (
    AppendEntry,
    DropGitJob,
    EnqueueGitJob,
    EnqueueStack,
    FinishGitJob,
    PauseStacking,
    RecordConflict,
    RecordOverview,
    RejectRequest,
    ResumeStacking,
    RetryGitJob,
    RetryIntegration,
    TakeNextGitJob,
    UnstackFrom,
    WithdrawRequest,
)
from ..events.base import Event
from ..events.review_ledger import ResultReceived, ResultRefused
from ..events.stack import (
    GitJobDropped,
    GitJobFinished,
    GitJobQueued,
    GitJobRetried,
    GitJobTaken,
    GitJobWithdrawn,
    IntegrationFailed,
    IntegrationRetried,
    OverviewRecorded,
    RebaseConflicted,
    StackCutBack,
    StackingPaused,
    StackingResumed,
    TaskStacked,
)
from ..value_objects.branch_name import BranchName
from ..value_objects.execution_id import ExecutionId
from ..value_objects.flow_ending import FlowEnding
from ..value_objects.git_job import GitJob
from ..value_objects.git_job_kind import GitJobKind
from ..value_objects.git_job_outcome import GitJobOutcome
from ..value_objects.limits import MAX_JOB_RETURNS
from ..value_objects.pr_number import PrNumber
from ..value_objects.stack_entry import StackEntry
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId
from ..value_objects.task_kind import TaskKind
from .base import Aggregate, Rejected, applies, handles

_J = GitJobKind

#: 相手のタスクを持つ仕事（止めたタスクなら列から外す）
_TASK_JOBS = frozenset({_J.CUT_TASK, _J.STACK})
#: 概要ブランチとランの base を相手にする仕事（取り出したときに、概要ブランチを切った仕事から埋める）
_OVERVIEW_JOBS = frozenset({_J.OPEN_OVERVIEW, _J.REWRITE_OVERVIEW, _J.FINISH})
#: 再計画が進んでいる間は取り出さない仕事（積む・積み直す）
_PAUSED_BY_REPLAN = frozenset({_J.STACK})


class Stack(Aggregate):
    NAME = "Stack"

    def __init__(self, stream: StreamId) -> None:
        super().__init__(stream)
        #: 概要 PR。スタックの一番下
        self.overview: StackEntry | None = None
        #: 概要ブランチとランの base（概要ブランチを切る仕事から）
        self.overview_branch: BranchName | None = None
        self.run_base: BranchName | None = None
        #: 積んだタスク（下から）。概要 PR は入れない
        self.entries: list[StackEntry] = []
        #: 順番待ちの列（前から取り出す。取り出せない仕事は飛ばす）
        self.queue: list[GitJob] = []
        #: 処理中の仕事と、その仕事で衝突したファイル・止めたタスクの仕事か・統合に失敗したか・
        #: 結果（積んだ 1 本・概要 PR・閉じた所）をもう受けたか
        self.current: GitJob | None = None
        self.current_conflicts: tuple[str, ...] = ()
        self.current_withdrawn = False
        self.current_failed = False
        self.current_delivered = False
        #: 列に入れた仕事の数（次の仕事の番号）
        self.jobs = 0
        #: 破棄して、まだ閉じ終えていない数
        self.cuts_pending = 0
        #: 再計画が進んでいて、積む仕事を取り出さない
        self.stacking_paused = False
        #: 仕事ごとの、フローを捨てて列へ戻した回数
        self.returns: dict[int, int] = {}
        #: 戻した回数が上限に達して止めた仕事。ラン統括が決めるまで、後ろの仕事も取り出さない
        self.parked: GitJob | None = None
        #: 閉じた中にあったタスクの、前に積んだブランチ（積み直す仕事に載せる）
        self.cut_branches: dict[TaskId, BranchName] = {}
        #: 一度でも積んだブランチと PR の番号
        self.used_branches: set[BranchName] = set()
        self.used_prs: set[PrNumber] = set()

    # --- 読む ---

    @property
    def top(self) -> BranchName | None:
        """スタックの一番上のブランチ。概要 PR がまだ無ければ None。"""
        if self.entries:
            return self.entries[-1].branch
        return self.overview.branch if self.overview is not None else None

    def entry_of(self, task: TaskId) -> StackEntry | None:
        """そのタスクを今スタックに積んである 1 本。積んでいない・閉じた所より上にあったなら None。"""
        return next((entry for entry in self.entries if entry.task == task), None)

    def _eligible(self, job: GitJob) -> bool:
        """今取り出してよい仕事か（名前の付いた規則）。"""
        if job.kind.needs_overview and self.overview is None:
            return False
        # 再計画が進んでいる間は積まない（再計画が読んだ一番上と、反映する時の一番上がずれる）
        if job.kind in _PAUSED_BY_REPLAN and self.stacking_paused:
            return False
        # 破棄した後は、閉じ終えるまで一番上に載せない（閉じる所より上に載ってしまう）
        return not (job.kind.builds_on_top and self.cuts_pending)

    def _filled(self, job: GitJob) -> GitJob:
        """取り出す仕事に、そのときのスタックの形から決まる相手を埋める。"""
        if job.kind.builds_on_top:
            return replace(job, base=self.top)
        if job.kind in _OVERVIEW_JOBS:
            return replace(job, branch=self.overview_branch, base=self.run_base)
        if job.kind is _J.DISCARD:
            cut = [i for i, entry in enumerate(self.entries) if entry.task in job.discarded]
            return replace(job, cut_from=self.entries[min(cut)] if cut else None)
        return job

    def _processing(self, kind: GitJobKind, task: TaskId | None = None) -> GitJob:
        """処理中の仕事が `kind` の仕事（`task` を相手にする）か。違えば知らせの取り違え。"""
        job = self.current
        if job is None or job.kind is not kind or (task is not None and job.task != task):
            now = f"{job.kind.value}（{job.task}）" if job is not None else "無い"
            raise Rejected(f"処理中の仕事は {kind.value}（{task}）ではない（{now}）")
        return job

    def _unused(self, entry: StackEntry) -> str | None:
        if entry.branch in self.used_branches:
            return f"{entry.branch} は一度積んだブランチ。積み直すなら新しい名前で切る"
        if entry.pr in self.used_prs:
            return f"PR #{entry.pr} は一度積んだ PR"
        return None

    @staticmethod
    def _answered(events: list[Event], source: ExecutionId | None) -> list[Event]:
        """受けた。結果を返したステージがあれば、受けたことを知らせる。"""
        return [*events, ResultReceived(source)] if source is not None else events

    @staticmethod
    def _refused(source: ExecutionId | None, reason: str) -> list[Event]:
        if source is None:
            raise Rejected(reason)
        return [ResultRefused(source, reason)]

    # --- 列に入れる・取り出す ---

    @handles(EnqueueStack)
    def _enqueue(self, command: EnqueueStack) -> list[Event]:
        task = command.task
        if task.kind is not TaskKind.IMPLEMENTATION:
            raise Rejected(f"積めるのは実装タスクだけ（{task}）")
        if any(j.kind is _J.STACK and j.task == task for j in self._jobs()):
            raise Rejected(f"{task} はすでに列にある")
        if any(entry.task == task for entry in self.entries):
            raise Rejected(f"{task} はすでに積んである")
        if command.branch in self.used_branches:
            raise Rejected(f"{command.branch} は一度積んだブランチ。積み直すなら新しい名前で切る")
        job = GitJob(
            self.jobs + 1,
            _J.STACK,
            task=task,
            branch=command.branch,
            previous=self.cut_branches.get(task),
        )
        return [GitJobQueued(job)]

    @handles(EnqueueGitJob)
    def _enqueue_job(self, command: EnqueueGitJob) -> list[Event]:
        kind = command.kind
        if kind is _J.STACK:
            raise Rejected("積む仕事は EnqueueStack で入れる")
        if kind is _J.CUT_OVERVIEW:
            if command.branch is None or command.base is None:
                raise Rejected("概要ブランチを切る仕事は、概要ブランチとランの base を持つ")
            if self.overview_branch is not None:
                raise Rejected("概要ブランチを切る仕事は、ランに 1 つだけ")
        if kind is _J.CUT_TASK and (command.task is None or command.branch is None):
            raise Rejected("タスクのブランチを切る仕事は、タスクとブランチを持つ")
        if kind is _J.DISCARD and not command.discarded:
            raise Rejected("破棄する仕事は、破棄したタスクを持つ")
        task = TaskId.planning() if kind is _J.CUT_STACK_TOP else command.task
        job = GitJob(
            self.jobs + 1,
            kind,
            task=task,
            branch=command.branch,
            base=command.base,
            discarded=command.discarded,
            ready_overview=command.ready_overview,
        )
        return [GitJobQueued(job)]

    def _jobs(self) -> list[GitJob]:
        held = [job for job in (self.current, self.parked) if job is not None]
        return [*self.queue, *held]

    @handles(TakeNextGitJob)
    def _take(self, command: TakeNextGitJob) -> list[Event]:
        # 取り出すきっかけ（仕事を入れた・終えた）を受けたポリシーは、取り出せるかを知らずに出す
        if self.current is not None or self.parked is not None:
            return []
        for job in self.queue:
            if self._eligible(job):
                return [GitJobTaken(self._filled(job))]
        return []

    @handles(FinishGitJob)
    def _finish(self, command: FinishGitJob) -> list[Event]:
        if self.current is None or self.current.id != command.job:
            # もう終えた（フローの終わりと捨てたことの両方が届いた、など）
            return []
        return [GitJobFinished(self.current, self._outcome(command.ending))]

    def _outcome(self, ending: FlowEnding) -> GitJobOutcome:
        # 結果をもう受けた仕事を戻すと、同じ結果をもう一度受けることになる（閉じた数を 2 回減らす など）
        if ending is FlowEnding.FINISHED or self.current_delivered:
            return GitJobOutcome.DONE
        if self.current_withdrawn:
            return GitJobOutcome.WITHDRAWN
        if self.current_failed:
            return GitJobOutcome.INTEGRATION_FAILED
        assert self.current is not None
        if not self.current.kind.must_finish:
            return GitJobOutcome.DROPPED
        if self.returns.get(self.current.id, 0) >= MAX_JOB_RETURNS:
            return GitJobOutcome.STUCK
        return GitJobOutcome.ABANDONED

    @handles(RetryGitJob)
    def _retry(self, command: RetryGitJob) -> list[Event]:
        return [GitJobRetried(self.parked)] if self.parked is not None else []

    @handles(DropGitJob)
    def _drop(self, command: DropGitJob) -> list[Event]:
        parked = self.parked
        if parked is None:
            return []
        if not parked.kind.can_drop:
            raise Rejected(
                f"仕事 {parked.id}（{parked.kind.value}）はやめるとランが終わらない。続けるなら答える"
            )
        return [GitJobDropped(parked)]

    @handles(RetryIntegration)
    def _retry_integration(self, command: RetryIntegration) -> list[Event]:
        current = self.current
        if current is None or not self.current_failed or current.task is None:
            return []
        return [IntegrationRetried(current.task)]

    @handles(WithdrawRequest)
    def _withdraw(self, command: WithdrawRequest) -> list[Event]:
        # 止めたタスクを受けたポリシーは、列にあるかを知らずに出す。無ければ外すものが無い。
        # 止めた仕事（parked）も外す
        waiting = [*self.queue, *([self.parked] if self.parked is not None else [])]
        events: list[Event] = [
            GitJobWithdrawn(job, in_progress=False)
            for job in waiting
            if job.kind in _TASK_JOBS and job.task == command.task
        ]
        current = self.current
        if (
            current is not None
            and current.kind in _TASK_JOBS
            and current.task == command.task
            and not self.current_withdrawn
        ):
            events.append(GitJobWithdrawn(current, in_progress=True))
        return events

    @handles(PauseStacking)
    def _pause(self, command: PauseStacking) -> list[Event]:
        # 確定した提案を退けて頼み直した再計画は、進んでいる再計画の続きで、もう止めてある
        return [] if self.stacking_paused else [StackingPaused()]

    @handles(ResumeStacking)
    def _resume(self, command: ResumeStacking) -> list[Event]:
        # 初めての計画の反映は、止めていない
        return [StackingResumed()] if self.stacking_paused else []

    # --- 処理中の仕事の結果 ---

    @handles(RecordOverview)
    def _record_overview(self, command: RecordOverview) -> list[Event]:
        self._processing(_J.OPEN_OVERVIEW)
        if command.entry.task != TaskId.git():
            raise Rejected("概要 PR の持ち主は git 管理タスク")
        if self.overview is not None:
            return self._refused(command.source, f"概要 PR はすでにある（#{self.overview.pr}）")
        if refused := self._unused(command.entry):
            return self._refused(command.source, refused)
        return self._answered([OverviewRecorded(command.entry)], command.source)

    @handles(RecordConflict)
    def _record_conflict(self, command: RecordConflict) -> list[Event]:
        self._processing(_J.STACK, command.task)
        if not command.files:
            raise Rejected("衝突したファイルが無い")
        return [RebaseConflicted(command.task, command.files)]

    @handles(AppendEntry)
    def _append(self, command: AppendEntry) -> list[Event]:
        entry = command.entry
        job = self._processing(_J.STACK, entry.task)
        if self.current_withdrawn:
            return self._refused(command.source, f"{entry.task} は止めたタスクで、積まない")
        if self.overview is None:
            return self._refused(command.source, "概要 PR がまだ無い")
        if entry.branch != job.branch:
            return self._refused(
                command.source, f"頼まれたブランチは {job.branch}（{entry.branch} が来た）"
            )
        if entry.base != self.top:
            return self._refused(
                command.source, f"積めるのは一番上（{self.top}）の上だけ（base が {entry.base}）"
            )
        if refused := self._unused(entry):
            return self._refused(command.source, refused)
        return self._answered([TaskStacked(entry)], command.source)

    @handles(RejectRequest)
    def _reject(self, command: RejectRequest) -> list[Event]:
        self._processing(_J.STACK, command.task)
        if not command.reason.strip():
            raise Rejected("上げる理由が空")
        files = command.files or self.current_conflicts
        return [IntegrationFailed(command.task, command.reason, files)]

    @handles(UnstackFrom)
    def _unstack(self, command: UnstackFrom) -> list[Event]:
        job = self._processing(_J.DISCARD)
        if command.discarded != job.discarded or command.entry != job.cut_from:
            raise Rejected("閉じる所と破棄したタスクは、処理中の破棄する仕事のもの")
        if command.entry is None:
            # 破棄したタスクは、前の破棄でもう閉じてあった
            return self._answered([StackCutBack(None)], command.source)
        if command.entry not in self.entries:
            return self._refused(
                command.source, f"{command.entry.task} の {command.entry.branch} は積んでいない"
            )
        removed = self.entries[self.entries.index(command.entry) :]
        return self._answered(
            [StackCutBack(command.entry, tuple(entry.task for entry in removed))], command.source
        )

    # --- 当てる ---

    @applies(GitJobQueued)
    def _queued(self, event: GitJobQueued) -> None:
        job = event.job
        self.queue.append(job)
        self.jobs = job.id
        if job.kind is _J.CUT_OVERVIEW:
            self.overview_branch = job.branch
            self.run_base = job.base
        if job.kind is _J.DISCARD:
            self.cuts_pending += 1
        if job.kind is _J.STACK and job.task is not None:
            self.cut_branches.pop(job.task, None)

    @applies(GitJobTaken)
    def _taken(self, event: GitJobTaken) -> None:
        self.queue = [job for job in self.queue if job.id != event.job.id]
        self._reset_current(event.job)

    @applies(GitJobFinished)
    def _finished(self, event: GitJobFinished) -> None:
        self._reset_current(None)
        job = event.job
        if event.outcome.returns_to_queue:
            # 相手（base・閉じる所）は、次に取り出すときのスタックの形で埋め直す
            self.queue.insert(0, job)
            self.returns[job.id] = self.returns.get(job.id, 0) + 1
        elif event.outcome is GitJobOutcome.STUCK:
            self.parked = job
        else:
            self.returns.pop(job.id, None)

    @applies(GitJobRetried)
    def _retried(self, event: GitJobRetried) -> None:
        self.parked = None
        self.queue.insert(0, event.job)
        self.returns[event.job.id] = 0

    @applies(GitJobDropped)
    def _dropped(self, event: GitJobDropped) -> None:
        self.parked = None
        self.returns.pop(event.job.id, None)

    @applies(IntegrationRetried)
    def _integration_retried(self, event: IntegrationRetried) -> None:
        self.current_failed = False

    def _reset_current(self, job: GitJob | None) -> None:
        self.current = job
        self.current_conflicts = ()
        self.current_withdrawn = False
        self.current_failed = False
        self.current_delivered = False

    @applies(GitJobWithdrawn)
    def _withdrawn(self, event: GitJobWithdrawn) -> None:
        if event.in_progress:
            self.current_withdrawn = True
        else:
            self.queue = [job for job in self.queue if job.id != event.job.id]
            if self.parked is not None and self.parked.id == event.job.id:
                self.parked = None
            self.returns.pop(event.job.id, None)

    @applies(OverviewRecorded)
    def _overview_recorded(self, event: OverviewRecorded) -> None:
        self.overview = event.entry
        self.used_branches.add(event.entry.branch)
        self.used_prs.add(event.entry.pr)
        self.current_delivered = True

    @applies(RebaseConflicted)
    def _conflicted(self, event: RebaseConflicted) -> None:
        self.current_conflicts = event.files

    @applies(TaskStacked)
    def _stacked(self, event: TaskStacked) -> None:
        self.entries.append(event.entry)
        self.used_branches.add(event.entry.branch)
        self.used_prs.add(event.entry.pr)
        self.current_delivered = True

    @applies(IntegrationFailed)
    def _failed(self, event: IntegrationFailed) -> None:
        # 処理中の仕事は、git 管理タスクのフローが終わる・捨てられるまで処理中のまま
        self.current_failed = True

    @applies(StackCutBack)
    def _cut_back(self, event: StackCutBack) -> None:
        if event.from_entry is not None:
            index = self.entries.index(event.from_entry)
            for entry in self.entries[index:]:
                self.cut_branches[entry.task] = entry.branch
            self.entries = self.entries[:index]
        self.cuts_pending = max(0, self.cuts_pending - 1)
        self.current_delivered = True

    @applies(StackingPaused)
    def _paused(self, event: StackingPaused) -> None:
        self.stacking_paused = True

    @applies(StackingResumed)
    def _resumed(self, event: StackingResumed) -> None:
        self.stacking_paused = False

    @applies(ResultReceived)
    def _received(self, event: ResultReceived) -> None:
        pass  # 状態は変えない。結果を返したステージの Task に知らせるための記録

    @applies(ResultRefused)
    def _refused_applied(self, event: ResultRefused) -> None:
        pass
