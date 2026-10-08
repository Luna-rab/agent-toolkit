"""統括の判断の JSON を、コマンド 1 つに置き換える表。

判断の JSON は `decision` と、`decision` を camelCase にした中身の欄（`insert-task` なら `insertTask`）
だけを持つ（`schemas/supervisor-*.json`）。どの `decision` でどの欄が要るかはスキーマに書けない
（根に結合子を置かない）ので、ここで確かめ、合わなければ理由を返して同じセッションに差し戻す。
判断の中身がドメインの規則に合うか（循環・経路・上限など）は、置き換えたコマンドを集約が確かめる。

中身の欄の名前は、コマンドの欄の名前を camelCase にしたもの。`command_id`・`issuer`・`task` は
driver が埋める（統括は名乗れない）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ...domain import codec
from ...domain.commands.base import Command
from ...domain.commands.questions import PostQuestion
from ...domain.commands.run import (
    AnswerEscalation,
    ApplyReplan,
    EscalateToRun,
    FinishRun,
    InsertTask,
    RequestReplan,
    StopTasks,
)
from ...domain.commands.task import AcceptFlow
from ...domain.value_objects.base import InvalidValue
from ...domain.value_objects.command_id import CommandId
from ...domain.value_objects.issuer import Issuer
from ...domain.value_objects.task_id import TaskId

#: タスク統括（実装タスク）の判断
TASK_DECISIONS: Mapping[str, type[Command]] = {"run-flow": AcceptFlow, "escalate": EscalateToRun}
#: ラン統括の判断
RUN_DECISIONS: Mapping[str, type[Command]] = {
    "replan": RequestReplan,
    "insert-task": InsertTask,
    "stop-tasks": StopTasks,
    "apply-plan": ApplyReplan,
    "answer": AnswerEscalation,
    "ask-user": PostQuestion,
    "finish": FinishRun,
}


class DecisionError(ValueError):
    """判断の JSON をコマンドに置き換えられない。理由はそのまま統括に返す。"""


def payload_key(decision: str) -> str:
    """`decision` の中身の欄の名前（`insert-task` → `insertTask`）。"""
    head, *rest = decision.split("-")
    return head + "".join(part.capitalize() for part in rest)


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _snake_keys(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {_snake(str(key)): _snake_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_snake_keys(item) for item in value]
    return value


def to_command(
    decisions: Mapping[str, type[Command]],
    raw: Mapping[str, Any] | None,
    *,
    command_id: CommandId,
    issuer: Issuer,
    task: TaskId | None = None,
) -> Command:
    """判断の JSON を、その `decision` のコマンドにする。`task` はタスク統括が統括するタスク。"""
    if raw is None:
        raise DecisionError("判断が返っていない（結果として 1 つ返す）")
    decision = raw.get("decision")
    if not isinstance(decision, str) or decision not in decisions:
        names = "・".join(decisions)
        raise DecisionError(f"decision は {names} のどれか（{decision!r}）")
    key = payload_key(decision)
    payload = raw.get(key)
    if not isinstance(payload, Mapping):
        raise DecisionError(f"decision が {decision} なのに、中身の欄 {key} が無い")
    if others := sorted(k for k in raw if k not in ("decision", key) and raw[k] is not None):
        raise DecisionError(
            f"decision が {decision} なのに、ほかの中身の欄を書いた: {', '.join(others)}"
        )
    cls = decisions[decision]
    fields = _snake_keys(payload)
    if claimed := sorted({"command_id", "issuer", "task"} & set(fields)):
        raise DecisionError(f"{key} に driver が埋める欄を書いた: {', '.join(claimed)}")
    fields |= {"command_id": command_id.value, "issuer": codec.to_json(issuer)}
    if task is not None:
        fields["task"] = task.value
    try:
        return codec.from_json(cls, fields)
    except (codec.DecodeError, InvalidValue, TypeError) as error:
        raise DecisionError(f"{key} の中身が読めない: {error}") from error
