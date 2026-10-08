"""Stack 集約が出すイベント（ストリーム `stack`）。"""

from __future__ import annotations

from dataclasses import dataclass

from ..value_objects.git_job import GitJob
from ..value_objects.git_job_outcome import GitJobOutcome
from ..value_objects.stack_entry import StackEntry
from ..value_objects.task_id import TaskId
from .base import Event


@dataclass(frozen=True)
class GitJobQueued(Event):
    """git 管理タスクの仕事を列に入れた。ポリシーが TakeNextGitJob を出す。"""

    job: GitJob


@dataclass(frozen=True)
class GitJobTaken(Event):
    """列から仕事を取り出した（base と閉じる所は、取り出したときに埋めてある）。

    git 管理タスクの統括（プログラム）が、この仕事の決まった並び（`flow.git_job_flow`）を組む。
    積む仕事なら、ポリシーが相手のタスクを stacking にする。
    """

    job: GitJob


@dataclass(frozen=True)
class GitJobFinished(Event):
    """処理していた仕事を終えた（git 管理タスクのフローが終わった・捨てられた）。次を取り出せる。

    `outcome.returns_to_queue` なら、仕事を列の先頭へ戻した（同じ番号のまま、次に取り出す）。
    """

    job: GitJob
    outcome: GitJobOutcome = GitJobOutcome.DONE


@dataclass(frozen=True)
class GitJobWithdrawn(Event):
    """タスクを止めたので、そのタスクを相手にする仕事を列から外した。

    `in_progress` なら処理中の仕事で、ポリシーが git 管理タスクのフローを捨てさせる（AbandonFlow）。
    その後に届く、この仕事への AppendEntry などは受けない（ResultRefused）。
    """

    job: GitJob
    in_progress: bool


@dataclass(frozen=True)
class RebaseConflicted(Event):
    task: TaskId
    files: tuple[str, ...]


@dataclass(frozen=True)
class TaskStacked(Event):
    entry: StackEntry


@dataclass(frozen=True)
class IntegrationFailed(Event):
    task: TaskId
    reason: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class StackCutBack(Event):
    """破棄したタスクから上を閉じた。`removed` は閉じた中に入っていたタスク（下から）。

    積む列に戻すタスクは Run が決めて持っている（TasksDiscarded.requeue）。ポリシーは ReturnToQueue を
    出すだけで、一覧を写さない。閉じるものが無かった（前の破棄で閉じてあった）なら `from_entry` は None。
    """

    from_entry: StackEntry | None
    removed: tuple[TaskId, ...] = ()


@dataclass(frozen=True)
class OverviewRecorded(Event):
    """概要 PR を作った。スタックの一番下で、その base はランの base。"""

    entry: StackEntry


@dataclass(frozen=True)
class GitJobRetried(Event):
    """止めた仕事を、列の先頭へ戻した（ラン統括が続けると答えた）。戻した回数は数え直す。"""

    job: GitJob


@dataclass(frozen=True)
class GitJobDropped(Event):
    """止めた仕事をやめた（ラン統括が答え以外の判断で閉じた）。後ろの仕事を取り出せる。"""

    job: GitJob


@dataclass(frozen=True)
class IntegrationRetried(Event):
    """統合の失敗に回答が届き、同じ仕事で統合をやり直す。失敗の印を下ろした。"""

    task: TaskId


@dataclass(frozen=True)
class StackingPaused(Event):
    """再計画が始まったので、積む仕事（積む・積み直す）を取り出すのを止めた。"""


@dataclass(frozen=True)
class StackingResumed(Event):
    """再計画を反映したので、積む仕事をまた取り出す。ポリシーが TakeNextGitJob を出す。

    取り出しを TasksPlanned ではなくこのイベントで頼むのは、範囲が変わった積む仕事を列から外す
    （TasksPlanned の withdraw → WithdrawRequest）のが、取り出すより先になるようにするためである。
    メインループは seq の順に配るので、TasksPlanned を受けた受け手がみな処理を終えてから、この
    イベントが配られる。
    """
