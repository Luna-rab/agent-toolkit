"""Stack（`domain/aggregates/stack.py`）。スタックの形と git 管理タスクの仕事の列を、コマンドとイベントの列だけで確かめる。"""

from __future__ import annotations

import itertools

import pytest

from autodev.domain.aggregates.base import Rejected
from autodev.domain.aggregates.stack import Stack
from autodev.domain.commands.base import Command
from autodev.domain.commands.stack import (
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
from autodev.domain.events.base import Event
from autodev.domain.events.review_ledger import ResultReceived, ResultRefused
from autodev.domain.events.stack import (
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
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.flow_ending import FlowEnding
from autodev.domain.value_objects.git_job import GitJob
from autodev.domain.value_objects.git_job_kind import GitJobKind
from autodev.domain.value_objects.git_job_outcome import GitJobOutcome
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.limits import MAX_JOB_RETURNS
from autodev.domain.value_objects.pr_number import PrNumber
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.stack_entry import StackEntry
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId

STREAM = StreamId.stack()
RUN = RunName("add-cache")
BASE = BranchName("main")
OVERVIEW_BRANCH = BranchName.overview(RUN)
OVERVIEW = StackEntry(TaskId.git(), OVERVIEW_BRANCH, PrNumber(1), BASE)
GIT = Issuer.task_supervisor(TaskId.git())
POLICY = Issuer.policy("git-jobs", EventId("task/task1#9"))
J = GitJobKind
_ids = itertools.count(1)
_rounds = itertools.count(1)


def cid() -> CommandId:
    return CommandId(f"c{next(_ids)}")


def source(stage: StageKind) -> ExecutionId:
    """git 管理タスクのステージの実行（結果を渡したステージ）。"""
    return ExecutionId(TaskId.git(), stage, 0, next(_rounds))


def branch(number: int, branch_round: int = 0) -> BranchName:
    return BranchName.for_task(RUN, number, branch_round)


def t(number: int) -> TaskId:
    return TaskId.numbered(number)


def drive(stack: Stack, command: Command) -> list[Event]:
    events = stack.handle(command)
    for event in events:
        stack.apply(event, command.command_id)
    return events


def enqueue_job(stack: Stack, kind: GitJobKind, **kwargs) -> list[Event]:
    return drive(stack, EnqueueGitJob(command_id=cid(), issuer=POLICY, kind=kind, **kwargs))


def enqueue(stack: Stack, number: int, branch_round: int = 0) -> list[Event]:
    return drive(
        stack,
        EnqueueStack(
            command_id=cid(), issuer=POLICY, task=t(number), branch=branch(number, branch_round)
        ),
    )


def take(stack: Stack) -> GitJob | None:
    events = drive(stack, TakeNextGitJob(command_id=cid(), issuer=POLICY))
    if not events:
        return None
    (taken,) = events
    assert isinstance(taken, GitJobTaken)
    return taken.job


def finish(stack: Stack) -> list[Event]:
    assert stack.current is not None
    return drive(stack, FinishGitJob(command_id=cid(), issuer=POLICY, job=stack.current.id))


def record_overview(
    stack: Stack, entry: StackEntry = OVERVIEW, by: ExecutionId | None = None
) -> list[Event]:
    by = by or source(StageKind.CREATE_OVERVIEW_PR)
    return drive(stack, RecordOverview(command_id=cid(), issuer=POLICY, entry=entry, source=by))


def opened() -> Stack:
    """概要ブランチを切り、概要 PR を作り終えたスタック。"""
    stack = Stack(STREAM)
    enqueue_job(stack, J.CUT_OVERVIEW, task=TaskId.planning(), branch=OVERVIEW_BRANCH, base=BASE)
    take(stack)
    finish(stack)
    enqueue_job(stack, J.OPEN_OVERVIEW)
    take(stack)
    record_overview(stack)
    finish(stack)
    return stack


def append(stack: Stack, entry: StackEntry, by: ExecutionId | None = None) -> list[Event]:
    by = by or source(StageKind.STACK_LINK)
    return drive(stack, AppendEntry(command_id=cid(), issuer=POLICY, entry=entry, source=by))


def entry_for(job: GitJob, pr: int) -> StackEntry:
    """取り出した積む仕事から、StackLink の結果（PR の番号）で積んだ 1 本を組む（ポリシーの組み方）。"""
    assert job.task is not None and job.branch is not None and job.base is not None
    return StackEntry(job.task, job.branch, PrNumber(pr), job.base)


def stack_one(stack: Stack, number: int, pr: int, branch_round: int = 0) -> StackEntry:
    """列に入れて、取り出して、一番上に積んで、仕事を終える。"""
    enqueue(stack, number, branch_round)
    job = take(stack)
    assert job is not None
    entry = entry_for(job, pr)
    append(stack, entry)
    finish(stack)
    return entry


def discard_job(stack: Stack, *numbers: int) -> list[Event]:
    return enqueue_job(stack, J.DISCARD, discarded=frozenset(t(n) for n in numbers))


def unstack(stack: Stack, job: GitJob, by: ExecutionId | None = None) -> list[Event]:
    by = by or source(StageKind.RELINK)
    return drive(
        stack,
        UnstackFrom(
            command_id=cid(),
            issuer=POLICY,
            entry=job.cut_from,
            discarded=job.discarded,
            source=by,
        ),
    )


def withdraw(stack: Stack, number: int) -> list[Event]:
    return drive(stack, WithdrawRequest(command_id=cid(), issuer=POLICY, task=t(number)))


def refused(events: list[Event]) -> str:
    (event,) = events
    assert isinstance(event, ResultRefused)
    return event.reason


# --- 概要 PR ---


def test_概要ブランチを切る仕事は概要PRが無くても取り出し概要PRの仕事に相手を埋める():
    stack = Stack(STREAM)
    (queued,) = enqueue_job(
        stack, J.CUT_OVERVIEW, task=TaskId.planning(), branch=OVERVIEW_BRANCH, base=BASE
    )
    assert isinstance(queued, GitJobQueued)
    assert take(stack) == queued.job
    finish(stack)
    enqueue_job(stack, J.OPEN_OVERVIEW)
    job = take(stack)
    assert job is not None
    assert (job.branch, job.base) == (OVERVIEW_BRANCH, BASE)


def test_概要PRがスタックの一番下になり受けたことを知らせる():
    stack = Stack(STREAM)
    enqueue_job(stack, J.CUT_OVERVIEW, branch=OVERVIEW_BRANCH, base=BASE)
    take(stack)
    finish(stack)
    enqueue_job(stack, J.OPEN_OVERVIEW)
    take(stack)
    by = source(StageKind.CREATE_OVERVIEW_PR)
    assert record_overview(stack, by=by) == [OverviewRecorded(OVERVIEW), ResultReceived(by)]
    assert (stack.overview, stack.top) == (OVERVIEW, OVERVIEW.branch)


def test_概要PRは1つで持ち主はgit管理タスク():
    stack = opened()
    enqueue_job(stack, J.OPEN_OVERVIEW)
    take(stack)
    assert "概要 PR はすでにある" in refused(record_overview(stack))
    with pytest.raises(Rejected, match="持ち主は git 管理タスク"):
        record_overview(stack, StackEntry(t(1), branch(1), PrNumber(9), BASE))


def test_概要PRの仕事を処理していなければ概要PRを受けない():
    with pytest.raises(Rejected, match="処理中の仕事は open-overview"):
        record_overview(Stack(STREAM))


def test_概要PRが無ければ一番上や概要PRを相手にする仕事を取り出さない():
    stack = Stack(STREAM)
    enqueue_job(stack, J.CUT_OVERVIEW, branch=OVERVIEW_BRANCH, base=BASE)
    take(stack)
    finish(stack)
    enqueue(stack, 1)
    enqueue_job(stack, J.CUT_TASK, task=t(2), branch=branch(2))
    enqueue_job(stack, J.REWRITE_OVERVIEW)
    assert take(stack) is None
    enqueue_job(stack, J.OPEN_OVERVIEW)
    assert take(stack) is not None and stack.current is not None
    assert stack.current.kind is J.OPEN_OVERVIEW


def test_概要ブランチを切る仕事はランに1つで相手を持つ():
    stack = Stack(STREAM)
    with pytest.raises(Rejected, match="概要ブランチとランの base を持つ"):
        enqueue_job(stack, J.CUT_OVERVIEW)
    enqueue_job(stack, J.CUT_OVERVIEW, branch=OVERVIEW_BRANCH, base=BASE)
    with pytest.raises(Rejected, match="ランに 1 つだけ"):
        enqueue_job(stack, J.CUT_OVERVIEW, branch=OVERVIEW_BRANCH, base=BASE)


@pytest.mark.parametrize(
    ("kind", "kwargs", "reason"),
    [
        (J.STACK, {"task": t(1)}, "EnqueueStack で入れる"),
        (J.CUT_TASK, {"task": t(1)}, "タスクとブランチを持つ"),
        (J.DISCARD, {}, "破棄したタスクを持つ"),
    ],
)
def test_仕事は種類ごとに要る相手を持つ(kind: GitJobKind, kwargs: dict, reason: str):
    with pytest.raises(Rejected, match=reason):
        enqueue_job(Stack(STREAM), kind, **kwargs)


def test_stack_topを切り直す仕事は計画タスクのためのもの():
    stack = opened()
    enqueue_job(stack, J.CUT_STACK_TOP)
    job = take(stack)
    assert job is not None
    assert (job.task, job.base) == (TaskId.planning(), OVERVIEW.branch)


# --- 1 本の列で 1 つずつ ---


def test_処理中の仕事は1つで終えるまで次を取り出さない():
    stack = opened()
    enqueue(stack, 1)
    enqueue_job(stack, J.CUT_TASK, task=t(2), branch=branch(2))
    first = take(stack)
    assert first is not None and first.kind is J.STACK
    assert take(stack) is None
    # 積んでも、git 管理タスクのフローが終わるまで処理中のまま（後ろの段 RefreshOverview がある）
    append(stack, entry_for(first, 2))
    assert take(stack) is None
    # 別の仕事の番号で終えようとしても何もしない
    assert drive(stack, FinishGitJob(command_id=cid(), issuer=POLICY, job=first.id + 99)) == []
    assert finish(stack) == [GitJobFinished(first)]
    second = take(stack)
    assert second is not None and second.kind is J.CUT_TASK
    # 一番上に載せる仕事の base は、取り出したときの一番上
    assert second.base == branch(1)


def test_取り出せる仕事が無ければ何も出さない():
    stack = opened()
    assert take(stack) is None
    assert drive(stack, FinishGitJob(command_id=cid(), issuer=POLICY, job=1)) == []


def test_列にあるタスクと積んだタスクと使ったブランチは積む列に入れない():
    stack = opened()
    enqueue(stack, 1)
    with pytest.raises(Rejected, match="すでに列にある"):
        enqueue(stack, 1)
    take(stack)
    # 処理中の仕事も列にあるものに数える
    with pytest.raises(Rejected, match="すでに列にある"):
        enqueue(stack, 1)
    stack2 = opened()
    stack_one(stack2, 1, 2)
    with pytest.raises(Rejected, match="すでに積んである"):
        enqueue(stack2, 1)


@pytest.mark.parametrize("task", [TaskId.planning(), TaskId.git()])
def test_積めるのは実装タスクだけ(task: TaskId):
    with pytest.raises(Rejected, match="実装タスクだけ"):
        drive(
            opened(),
            EnqueueStack(command_id=cid(), issuer=POLICY, task=task, branch=branch(1)),
        )


# --- 積む ---


def test_積んだ1本の中身の問題は拒まずに受けられないと返す():
    stack = opened()
    enqueue(stack, 1)
    job = take(stack)
    assert job is not None
    good = entry_for(job, 2)
    assert "頼まれたブランチは" in refused(
        append(stack, StackEntry(t(1), branch(1, 1), PrNumber(2), good.base))
    )
    assert "積めるのは一番上" in refused(
        append(stack, StackEntry(t(1), good.branch, PrNumber(2), branch(9)))
    )
    assert "一度積んだ PR" in refused(
        append(stack, StackEntry(t(1), good.branch, OVERVIEW.pr, good.base))
    )
    by = source(StageKind.STACK_LINK)
    assert append(stack, good, by) == [TaskStacked(good), ResultReceived(by)]
    assert stack.entries == [good]


def test_処理中でない仕事の結果は取り違えとして拒む():
    stack = opened()
    with pytest.raises(Rejected, match="処理中の仕事は stack"):
        append(stack, StackEntry(t(1), branch(1), PrNumber(2), OVERVIEW.branch))
    with pytest.raises(Rejected, match="処理中の仕事は stack"):
        drive(stack, RecordConflict(command_id=cid(), issuer=POLICY, task=t(1), files=("a",)))
    with pytest.raises(Rejected, match="処理中の仕事は stack"):
        drive(stack, RejectRequest(command_id=cid(), issuer=POLICY, task=t(1), reason="x"))


def test_衝突を記録し統合の失敗には記録した衝突を載せる():
    stack = opened()
    enqueue(stack, 1)
    take(stack)
    conflicted = drive(
        stack, RecordConflict(command_id=cid(), issuer=POLICY, task=t(1), files=("a.py",))
    )
    assert conflicted == [RebaseConflicted(t(1), ("a.py",))]
    failed = drive(
        stack, RejectRequest(command_id=cid(), issuer=POLICY, task=t(1), reason="両方は残せない")
    )
    assert failed == [IntegrationFailed(t(1), "両方は残せない", ("a.py",))]
    # 統合に失敗しても、git 管理タスクのフローが終わる（捨てられる）まで処理中のまま
    assert stack.current is not None
    finish(stack)
    assert stack.current is None


# --- 止めたタスクの仕事 ---


def test_止めたタスクの仕事を列から外し処理中なら以後の結果を受けない():
    stack = opened()
    enqueue_job(stack, J.CUT_TASK, task=t(2), branch=branch(2))
    enqueue(stack, 1)
    queued = list(stack.queue)
    assert withdraw(stack, 2) == [GitJobWithdrawn(queued[0], in_progress=False)]
    job = take(stack)
    assert job is not None and job.task == t(1)
    assert withdraw(stack, 1) == [GitJobWithdrawn(job, in_progress=True)]
    assert "止めたタスク" in refused(append(stack, entry_for(job, 2)))
    assert withdraw(stack, 1) == []  # もう外した
    assert withdraw(stack, 7) == []  # 列に無い


# --- 破棄 ---


def stacked_three() -> tuple[Stack, list[StackEntry]]:
    stack = opened()
    return stack, [stack_one(stack, n, n + 1) for n in (1, 2, 3)]


def test_破棄の仕事は閉じる所を埋め閉じた中を外す():
    stack, entries = stacked_three()
    discard_job(stack, 2)
    job = take(stack)
    assert job is not None and job.kind is J.DISCARD
    assert job.cut_from == entries[1]
    by = source(StageKind.RELINK)
    assert unstack(stack, job, by) == [StackCutBack(entries[1], (t(2), t(3))), ResultReceived(by)]
    assert stack.entries == entries[:1]


def test_破棄した後は閉じ終えるまで一番上に載せる仕事を取り出さず破棄を先に取り出す():
    stack, _ = stacked_three()
    enqueue(stack, 4)
    enqueue_job(stack, J.CUT_TASK, task=t(5), branch=branch(5))
    enqueue_job(stack, J.REWRITE_OVERVIEW)
    discard_job(stack, 1)
    # 載せる仕事は飛ばし、載せない仕事（概要 PR の書き直し）は列の順に取り出す
    first = take(stack)
    assert first is not None and first.kind is J.REWRITE_OVERVIEW
    finish(stack)
    job = take(stack)
    assert job is not None and job.kind is J.DISCARD
    unstack(stack, job)
    finish(stack)
    after = take(stack)
    assert after is not None and after.kind is J.STACK
    assert after.base == OVERVIEW.branch


def abandon(stack: Stack) -> list[Event]:
    """git 管理タスクのフローが捨てられた（FlowAbandoned）。"""
    assert stack.current is not None
    return drive(
        stack,
        FinishGitJob(
            command_id=cid(), issuer=POLICY, job=stack.current.id, ending=FlowEnding.ABANDONED
        ),
    )


def test_取り下げでも統合の失敗でもなくフローを捨てた仕事は列の先頭へ戻す():
    stack, _ = stacked_three()
    discard_job(stack, 2)
    enqueue_job(stack, J.REWRITE_OVERVIEW)
    job = take(stack)
    assert job is not None and job.kind is J.DISCARD
    (finished,) = abandon(stack)
    assert isinstance(finished, GitJobFinished) and finished.outcome is GitJobOutcome.ABANDONED
    # 同じ仕事を、後ろの仕事より先にもう一度取り出す
    again = take(stack)
    assert again is not None and (again.id, again.kind) == (job.id, J.DISCARD)
    # 結果（閉じた所）をもう受けた仕事は、捨てても戻さない（同じ結果を 2 回受けない）
    unstack(stack, again)
    (finished,) = abandon(stack)
    assert isinstance(finished, GitJobFinished) and finished.outcome is GitJobOutcome.DONE
    after = take(stack)
    assert after is not None and after.kind is J.REWRITE_OVERVIEW


def test_取り下げた仕事と統合に失敗した仕事は捨てても戻さない():
    stack = opened()
    enqueue(stack, 1)
    take(stack)
    withdraw(stack, 1)
    (finished,) = abandon(stack)
    assert isinstance(finished, GitJobFinished) and finished.outcome is GitJobOutcome.WITHDRAWN
    enqueue(stack, 2)
    take(stack)
    drive(stack, RejectRequest(command_id=cid(), issuer=POLICY, task=t(2), reason="壊れる"))
    (finished,) = abandon(stack)
    assert isinstance(finished, GitJobFinished)
    assert finished.outcome is GitJobOutcome.INTEGRATION_FAILED
    assert stack.queue == [] and take(stack) is None


def outcome_of(events: list[Event]) -> GitJobOutcome:
    (finished,) = events
    assert isinstance(finished, GitJobFinished)
    return finished.outcome


def test_済ませなくてもランが終わる仕事は捨てたら消す():
    stack = opened()
    enqueue_job(stack, J.REWRITE_OVERVIEW)
    take(stack)
    assert outcome_of(abandon(stack)) is GitJobOutcome.DROPPED
    assert stack.queue == []


def test_戻した回数が上限に達したら止めて後ろの仕事も待たせラン統括の判断で続けるかやめる():
    stack = opened()
    enqueue(stack, 1)
    enqueue_job(stack, J.REWRITE_OVERVIEW)
    for _ in range(MAX_JOB_RETURNS):
        job = take(stack)
        assert job is not None and job.task == t(1)
        assert outcome_of(abandon(stack)) is GitJobOutcome.ABANDONED
    take(stack)
    assert outcome_of(abandon(stack)) is GitJobOutcome.STUCK
    # 止めている間は、後ろの仕事も取り出さない
    assert take(stack) is None
    # 続けるなら、列の先頭へ戻して数え直す
    (retried,) = drive(stack, RetryGitJob(command_id=cid(), issuer=POLICY))
    assert isinstance(retried, GitJobRetried)
    for _ in range(MAX_JOB_RETURNS):
        take(stack)
        assert outcome_of(abandon(stack)) is GitJobOutcome.ABANDONED
    take(stack)
    assert outcome_of(abandon(stack)) is GitJobOutcome.STUCK
    # やめるなら、後ろの仕事へ進む
    (dropped,) = drive(stack, DropGitJob(command_id=cid(), issuer=POLICY))
    assert isinstance(dropped, GitJobDropped)
    after = take(stack)
    assert after is not None and after.kind is J.REWRITE_OVERVIEW
    # 止めた仕事が無ければ、どちらも何もしない
    assert drive(stack, RetryGitJob(command_id=cid(), issuer=POLICY)) == []
    assert drive(stack, DropGitJob(command_id=cid(), issuer=POLICY)) == []


def test_やめるとランが終わらない止めた仕事はやめる判断を拒み続けるのを待つ():
    stack, _ = stacked_three()
    discard_job(stack, 2)
    for _ in range(MAX_JOB_RETURNS):
        take(stack)
        assert outcome_of(abandon(stack)) is GitJobOutcome.ABANDONED
    take(stack)
    assert outcome_of(abandon(stack)) is GitJobOutcome.STUCK
    with pytest.raises(Rejected, match="やめるとランが終わらない"):
        drive(stack, DropGitJob(command_id=cid(), issuer=POLICY))
    assert stack.parked is not None and stack.parked.kind is J.DISCARD
    (retried,) = drive(stack, RetryGitJob(command_id=cid(), issuer=POLICY))
    assert isinstance(retried, GitJobRetried)


@pytest.mark.parametrize("kind", list(GitJobKind))
def test_やめてよい仕事はタスクを切る積む仕事と概要PRの書き直しだけ(kind: GitJobKind):
    droppable = {J.CUT_TASK, J.CUT_STACK_TOP, J.STACK, J.REWRITE_OVERVIEW}
    assert kind.can_drop is (kind in droppable)


def test_止めたタスクの止めた仕事は列から外す():
    stack = opened()
    enqueue(stack, 1)
    for _ in range(MAX_JOB_RETURNS + 1):
        take(stack)
        abandon(stack)
    parked = stack.parked
    assert parked is not None
    assert withdraw(stack, 1) == [GitJobWithdrawn(parked, in_progress=False)]
    assert stack.parked is None


def test_統合の失敗の印は答えてやり直したら下ろし捨てた仕事は戻す():
    stack = opened()
    enqueue(stack, 1)
    take(stack)
    drive(stack, RejectRequest(command_id=cid(), issuer=POLICY, task=t(1), reason="壊れる"))
    assert drive(stack, RetryIntegration(command_id=cid(), issuer=POLICY)) == [
        IntegrationRetried(t(1))
    ]
    assert drive(stack, RetryIntegration(command_id=cid(), issuer=POLICY)) == []
    assert outcome_of(abandon(stack)) is GitJobOutcome.ABANDONED


def pause(stack: Stack) -> list[Event]:
    return drive(stack, PauseStacking(command_id=cid(), issuer=POLICY))


def resume(stack: Stack) -> list[Event]:
    return drive(stack, ResumeStacking(command_id=cid(), issuer=POLICY))


def test_再計画が進んでいる間は積む仕事だけを取り出さず反映したらまた取り出す():
    stack = opened()
    assert pause(stack) == [StackingPaused()]
    # 確定した提案を退けて頼み直した再計画は、もう止めてある
    assert pause(stack) == []
    enqueue(stack, 1)
    enqueue_job(stack, J.CUT_STACK_TOP)
    # stack-top を切り直す仕事は再計画そのものに要るので、積む仕事を飛ばして取り出す
    job = take(stack)
    assert job is not None and job.kind is J.CUT_STACK_TOP
    finish(stack)
    assert take(stack) is None
    assert resume(stack) == [StackingResumed()]
    job = take(stack)
    assert job is not None and job.kind is J.STACK and job.task == t(1)
    # 初めての計画の反映では、止めていない
    assert resume(stack) == []


def test_積み直すタスクは前に積んだブランチを持ち新しい名前とPRで積む():
    stack, entries = stacked_three()
    discard_job(stack, 2)
    job = take(stack)
    assert job is not None
    unstack(stack, job)
    finish(stack)
    with pytest.raises(Rejected, match="一度積んだブランチ"):
        enqueue(stack, 3)
    enqueue(stack, 3, branch_round=1)
    restack = take(stack)
    assert restack is not None
    assert (restack.branch, restack.previous, restack.base) == (
        branch(3, 1),
        entries[2].branch,
        entries[0].branch,
    )
    assert "一度積んだ PR" in refused(append(stack, entry_for(restack, entries[2].pr.value)))
    assert append(stack, entry_for(restack, 9))[0] == TaskStacked(entry_for(restack, 9))


def test_前の破棄で閉じてあれば閉じるものが無い():
    stack, _ = stacked_three()
    discard_job(stack, 2)
    discard_job(stack, 3)
    first = take(stack)
    assert first is not None
    unstack(stack, first)
    finish(stack)
    second = take(stack)
    assert second is not None and second.cut_from is None
    by = source(StageKind.RELINK)
    assert unstack(stack, second, by) == [StackCutBack(None), ResultReceived(by)]
    assert stack.cuts_pending == 0


def test_閉じる所は処理中の破棄する仕事のもの():
    stack, entries = stacked_three()
    discard_job(stack, 2)
    job = take(stack)
    assert job is not None
    with pytest.raises(Rejected, match="処理中の破棄する仕事のもの"):
        drive(
            stack,
            UnstackFrom(
                command_id=cid(),
                issuer=POLICY,
                entry=entries[0],
                discarded=job.discarded,
                source=source(StageKind.RELINK),
            ),
        )


# --- 再生 ---


def test_イベントの列を再生すると同じ状態になる():
    live = Stack(STREAM)
    history: list[tuple[Event, CommandId]] = []

    def step(command: Command) -> None:
        history.extend((event, command.command_id) for event in drive(live, command))

    def job(kind: GitJobKind, **kwargs) -> None:
        step(EnqueueGitJob(command_id=cid(), issuer=POLICY, kind=kind, **kwargs))

    def take_and(*after: Command) -> None:
        step(TakeNextGitJob(command_id=cid(), issuer=POLICY))
        assert live.current is not None
        for command in after:
            step(command)
        step(FinishGitJob(command_id=cid(), issuer=POLICY, job=live.current.id))

    job(J.CUT_OVERVIEW, branch=OVERVIEW_BRANCH, base=BASE)
    take_and()
    job(J.OPEN_OVERVIEW)
    take_and(RecordOverview(command_id=cid(), issuer=POLICY, entry=OVERVIEW))
    for number in (1, 2):
        step(EnqueueStack(command_id=cid(), issuer=POLICY, task=t(number), branch=branch(number)))
        top = live.top
        assert top is not None
        entry = StackEntry(t(number), branch(number), PrNumber(number + 1), top)
        take_and(AppendEntry(command_id=cid(), issuer=POLICY, entry=entry))
    job(J.DISCARD, discarded=frozenset({t(1)}))
    step(TakeNextGitJob(command_id=cid(), issuer=POLICY))
    assert live.current is not None
    step(
        UnstackFrom(
            command_id=cid(),
            issuer=POLICY,
            entry=live.current.cut_from,
            discarded=live.current.discarded,
        )
    )
    replayed = Stack.replay(STREAM, history)
    assert vars(replayed) == vars(live)
    assert replayed.cut_branches == {t(1): branch(1), t(2): branch(2)}
