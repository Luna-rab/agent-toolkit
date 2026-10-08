"""エスカレーションの出所から、1 段上の統括を決める。

エスカレーションは段を飛ばさない。ステージ（とタスクの中のポリシー）が上げたものはそのタスクの
統括が受け、タスクの統括が上げたものはラン統括が受け、ラン統括が上げたもの（質問）は `/autodev`
が受ける。どの段がどの種類を上げてよいかも、ここに置く。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.limits import MAX_SUPERVISOR_FAILURES
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId
from ..value_objects.task_kind import TaskKind

_E = EscalationKind


class SupervisorLevel(Enum):
    """エスカレーションを受ける段。"""

    TASK = "task-supervisor"
    RUN = "run-supervisor"
    #: `/autodev` のエージェント（その先のユーザー）
    USER = "user"


@dataclass(frozen=True)
class Route:
    level: SupervisorLevel
    #: TASK のとき、どのタスクの統括か
    task: TaskId | None = None


#: タスクの中で上がり、そのタスクの統括が受けるもの（タスクの種類ごと）。ステージの結果から Task が
#: 出すものと、タスクの中のポリシー（停滞・設計のジャッジ）が Escalate で出すものの両方
RAISED_IN_TASK: Mapping[TaskKind, frozenset[EscalationKind]] = {
    TaskKind.PLANNING: frozenset(
        {
            _E.ASK,
            _E.DESIGN_AMBIGUOUS,
            _E.DESIGN_REVERTED,
            _E.DESIGN_ROUNDS_EXHAUSTED,
            _E.STAGE_ERRORS,
            _E.RESULT_REFUSED,
        }
    ),
    TaskKind.IMPLEMENTATION: frozenset(
        {
            _E.STALL,
            _E.SUPERVISOR_FINDING,
            _E.DESIGN_GAP,
            _E.TEST_CONFLICT,
            _E.RED_CHECK_FAILED,
            _E.QUICK_CHECK_FAILED,
            _E.UNTESTED_CHANGE,
            _E.GATE_UNFIXABLE,
            _E.STAGE_ERRORS,
            # 統合をやり直す差し込んだタスクの ResolveConflict が「両方は残せない」と返した
            _E.INTEGRATION_FAILED,
            _E.RESULT_REFUSED,
        }
    ),
    TaskKind.GIT: frozenset({_E.INTEGRATION_FAILED, _E.STAGE_ERRORS, _E.RESULT_REFUSED}),
}

#: タスクの統括がラン統括へ上げてよいもの。実装タスクの統括（LLM）は自分で解けないことを
#: needs-replan・needs-human で上げる。計画タスクと git 管理タスクの統括はプログラムで、自分では
#: 解かず、受けたものをそのままの種類で上げる
RELAYED_TO_RUN: Mapping[TaskKind, frozenset[EscalationKind]] = {
    TaskKind.PLANNING: RAISED_IN_TASK[TaskKind.PLANNING],
    TaskKind.IMPLEMENTATION: frozenset({_E.NEEDS_REPLAN, _E.NEEDS_HUMAN}),
    TaskKind.GIT: RAISED_IN_TASK[TaskKind.GIT],
}


def task_of_stream(stream: StreamId) -> TaskId:
    """`task/<TaskId>` のストリームのタスク。"""
    prefix, _, task = stream.value.partition("/")
    if prefix != "task" or not task:
        raise ValueError(f"タスクのストリームではない: {stream}")
    return TaskId(task)


class EscalationRouter:
    @staticmethod
    def route(stream: StreamId, kind: EscalationKind, task: TaskId | None = None) -> Route:
        """`stream` の集約が出した `kind` のエスカレーションを受ける段。

        `task` は上げてきたタスク（Run の EscalationRaised.task）。統括が応じなかった
        （supervisor-failed）なら、応じなかった統括の 1 段上が受ける。タスク統括（task がある）なら
        ラン統括、ラン統括（task が無い）なら `/autodev`。タスク統括が同じ知らせに続けて応じなかった
        数が上限を超えても、受けるのはラン統括のままにする（段を飛ばさない）。そのときラン統括が
        ユーザーに聞いてから答えることは、Run が答えを受けるときに求める
        （`requires_user_answer`）。
        """
        if kind is _E.QUESTION:
            return Route(SupervisorLevel.USER)
        if stream == StreamId.run():
            if kind is _E.SUPERVISOR_FAILED and task is None:
                return Route(SupervisorLevel.USER)
            return Route(SupervisorLevel.RUN)
        return Route(SupervisorLevel.TASK, task_of_stream(stream))

    @staticmethod
    def requires_user_answer(kind: EscalationKind, failures: int) -> bool:
        """ラン統括がこの上げに自分で答えてはいけない（ユーザーの回答を添えて答える）か。

        タスク統括が同じ知らせに続けて応じなかった数（`failures`）が MAX_SUPERVISOR_FAILURES を
        超えたら、ラン統括の答えでは直らないとみなす（毎回「続けて」と答えても輪が止まる）。
        """
        return kind is _E.SUPERVISOR_FAILED and failures > MAX_SUPERVISOR_FAILURES

    @staticmethod
    def relays_as_is(task_kind: TaskKind) -> bool:
        """そのタスクの統括が、受けたエスカレーションを自分では解かずにそのまま上げるか。"""
        return RELAYED_TO_RUN[task_kind] == RAISED_IN_TASK[task_kind]

    @staticmethod
    def why_not_raise(task_kind: TaskKind, kind: EscalationKind) -> str | None:
        """そのタスクの中で `kind` を上げてよくないなら、その理由。"""
        if kind in RAISED_IN_TASK[task_kind]:
            return None
        return f"{task_kind.value} のタスクの中では {kind.value} を上げない"

    @staticmethod
    def why_not_relay(task_kind: TaskKind, kind: EscalationKind) -> str | None:
        """そのタスクの統括が `kind` をラン統括へ上げてよくないなら、その理由。"""
        if kind in RELAYED_TO_RUN[task_kind]:
            return None
        allowed = ", ".join(sorted(k.value for k in RELAYED_TO_RUN[task_kind]))
        return f"{task_kind.value} のタスクの統括がラン統括へ上げられるのは {allowed} だけ（{kind.value}）"
