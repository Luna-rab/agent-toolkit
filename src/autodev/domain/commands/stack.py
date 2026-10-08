"""Stack 集約が受けるコマンド。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..value_objects.branch_name import BranchName
from ..value_objects.execution_id import ExecutionId
from ..value_objects.flow_ending import FlowEnding
from ..value_objects.git_job_kind import GitJobKind
from ..value_objects.issuer_kind import IssuerKind
from ..value_objects.stack_entry import StackEntry
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId
from .base import Command

_K = IssuerKind


@dataclass(frozen=True, kw_only=True)
class StackCommand(Command):
    AGGREGATE: ClassVar[str] = "Stack"

    @property
    def target(self) -> StreamId:
        return StreamId.stack()

    @property
    def supervised_task(self) -> TaskId | None:
        """スタックを変えるのは git 管理タスクだけ。"""
        return TaskId.git()


#: Stack のコマンドを出せる者。git 管理タスクのステージの結果は、ポリシーが StageCompleted から組む
_STACK_ISSUERS = frozenset({_K.POLICY, _K.TASK_SUPERVISOR})


@dataclass(frozen=True, kw_only=True)
class EnqueueStack(StackCommand):
    """フローを終えたタスク（TaskGated）・積む列に戻すタスク（TasksReturnedToQueue）を積む仕事を入れる。"""

    ISSUERS = frozenset({_K.POLICY})
    task: TaskId
    branch: BranchName


@dataclass(frozen=True, kw_only=True)
class EnqueueGitJob(StackCommand):
    """積む以外の git 管理タスクの仕事を列に入れる。相手は、きっかけのイベントから写す。

    概要ブランチを切る仕事（RunStarted）は、概要ブランチとランの base を持つ。Stack はそれを覚え、
    概要 PR を相手にする仕事に埋める。
    """

    ISSUERS = frozenset({_K.POLICY})
    kind: GitJobKind
    task: TaskId | None = None
    branch: BranchName | None = None
    base: BranchName | None = None
    discarded: frozenset[TaskId] = frozenset()
    ready_overview: bool = False


@dataclass(frozen=True, kw_only=True)
class TakeNextGitJob(StackCommand):
    """次の仕事を取り出す。処理中の仕事がある・取り出せる仕事が無いなら、何もしない。"""

    ISSUERS = _STACK_ISSUERS


@dataclass(frozen=True, kw_only=True)
class FinishGitJob(StackCommand):
    """処理していた仕事を終える（git 管理タスクの FlowFinished・FlowAbandoned を受けて）。

    取り下げ・統合の失敗で捨てたのか、それ以外で捨てたのか（列の先頭へ戻す）は、Stack が自分の記録と
    合わせて決める。ポリシーはフローの終わり方しか知らない。
    """

    ISSUERS = frozenset({_K.POLICY})
    #: 終えた仕事の番号（FlowFinished.job.id）。処理中の仕事でなければ何もしない
    job: int
    ending: FlowEnding = FlowEnding.FINISHED


@dataclass(frozen=True, kw_only=True)
class RecordConflict(StackCommand):
    ISSUERS = _STACK_ISSUERS
    task: TaskId
    files: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class AppendEntry(StackCommand):
    """積んだ 1 本（StackLink の StageCompleted の job と result.pr から組む）。"""

    ISSUERS = _STACK_ISSUERS
    entry: StackEntry
    #: 結果を返したステージ（StackLink）の実行。受けたかを ResultReceived・ResultRefused で返す
    source: ExecutionId | None = None


@dataclass(frozen=True, kw_only=True)
class RejectRequest(StackCommand):
    """統合に失敗した（git 管理タスクの StageReported の integration-failed から組む）。衝突した
    ファイルを書かなければ、処理中の仕事に記録した衝突（RecordConflict）を使う。"""

    ISSUERS = _STACK_ISSUERS
    task: TaskId
    reason: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class UnstackFrom(StackCommand):
    """破棄したタスクから上を閉じた（Relink の StageCompleted の job から組む）。"""

    ISSUERS = _STACK_ISSUERS
    #: 閉じた中で一番下（GitJob.cut_from）。閉じるものが無ければ None
    entry: StackEntry | None
    #: 破棄したタスク（GitJob.discarded）
    discarded: frozenset[TaskId]
    source: ExecutionId | None = None


@dataclass(frozen=True, kw_only=True)
class RecordOverview(StackCommand):
    """概要 PR を作った（CreateOverviewPR の StageCompleted の job と result.pr から組む）。
    `entry.task` は git 管理タスク。"""

    ISSUERS = _STACK_ISSUERS
    entry: StackEntry
    source: ExecutionId | None = None


@dataclass(frozen=True, kw_only=True)
class WithdrawRequest(StackCommand):
    """止めたタスクを相手にする仕事を、列から外す（TasksStopped を受けて）。"""

    ISSUERS = frozenset({_K.POLICY})
    task: TaskId


@dataclass(frozen=True, kw_only=True)
class RetryGitJob(StackCommand):
    """止めた仕事（戻した回数が上限に達した）を、ラン統括が続けると答えた。列の先頭へ戻し、数え直す。

    git 管理タスクの、上げ元の実行が無い stage-errors への回答（EscalationResolved）を受けたポリシーが、
    止めた仕事があるかを知らずに出す。無ければ何もしない。
    """

    ISSUERS = frozenset({_K.POLICY})


@dataclass(frozen=True, kw_only=True)
class DropGitJob(StackCommand):
    """止めた仕事を、ラン統括が答え以外の判断（差し込む・止める など）で閉じた。仕事をやめる。

    git 管理タスクの側の EscalationClosed を受けたポリシーが、止めた仕事があるかを知らずに出す。
    """

    ISSUERS = frozenset({_K.POLICY})


@dataclass(frozen=True, kw_only=True)
class RetryIntegration(StackCommand):
    """統合の失敗のエスカレーションに回答が届き、同じ仕事で統合をやり直す（失敗の印を下ろす）。

    git 管理タスクの integration-failed への回答を受けたポリシーが出す。印が無ければ何もしない。
    """

    ISSUERS = frozenset({_K.POLICY})


@dataclass(frozen=True, kw_only=True)
class PauseStacking(StackCommand):
    """再計画が始まった（ReplanRequested）。積む仕事を取り出すのを止める。もう止めていれば何もしない。"""

    ISSUERS = frozenset({_K.POLICY})


@dataclass(frozen=True, kw_only=True)
class ResumeStacking(StackCommand):
    """計画を反映した（TasksPlanned）。積む仕事をまた取り出す。止めていなければ何もしない。"""

    ISSUERS = frozenset({_K.POLICY})
