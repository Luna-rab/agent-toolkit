"""反応: イベントに書かれた副作用を、ポートを呼んで起こす受け手。

反応は判断しない。起こすかどうか・何を起こすかは、イベントが出た時点でドメインが決めている
（統括を起こすのは `domain.supervision.wake_for`、ask の続きは `domain.policies.registry.FOLLOW_UPS`）。
反応は 2 回呼ばれても同じ結果になるように作る（落ちた後に、チェックポイントの後ろから配り直される）。

| 受け手 | 受けるイベント | すること |
| --- | --- | --- |
| `wake-supervisors` | `wake_for` が統括を返すもの | 統括を別のスレッドで起こす（`SupervisorRunner`） |
| `write-questions` | QuestionPosted・QuestionAnswered・QuestionWithdrawn | `questions/<QuestionId>.json` を書く |
| `write-ask-answers` | EscalationResolved（止めた呼び出しがある） | `answers/<tool_use_id>.json` を書き、FOLLOW_UPS の続きを返す |
| `append-design-appendix` | DesignSettled | 確定した設計ファイルの末尾に、must-fix 以外の指摘を書き足す |
| `begin-stage` | StageRequested | 実行器の `begin` |
| `run-stage` | StageStarted（今 running の実行） | 実行器の `run` |
| `interrupt-stage` | StageInterrupted | 実行器の `interrupt` |
| `restart-execution` | ExecutionRestarted | 実行器の `restart` |
| `abort-rebase` | IntegrationFailed | 実行器の `abort_rebase`（統合に失敗したタスクの rebase を取りやめる） |

受け手の名前はチェックポイントの鍵で、反応が返すコマンドの id にも入るので、変えない。FOLLOW_UPS の
続きの id は行の名前（`resume-ask`）で決まるので、受け手の名前に同じ名前を使わない。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from ...domain.aggregates.base import Aggregate
from ...domain.aggregates.questions import Questions
from ...domain.aggregates.task import Task
from ...domain.commands.base import Command
from ...domain.events.design import DesignSettled
from ...domain.events.questions import QuestionAnswered, QuestionPosted, QuestionWithdrawn
from ...domain.events.stack import IntegrationFailed
from ...domain.events.task import (
    EscalationResolved,
    ExecutionRestarted,
    StageInterrupted,
    StageRequested,
    StageStarted,
)
from ...domain.policies.registry import FOLLOW_UPS
from ...domain.supervision import wake_for
from ...domain.value_objects.stream_id import StreamId
from ...infra.paths import RunPaths
from ..stages.executor import StageExecutor
from ..stages.files import append_appendix, write_answer, write_question
from ..stages.prompts.render import Prompts
from ..supervision.supervisors import SupervisorRunner
from .mainloop import Delivery, Inbox, Subscriber

Aggregates = Callable[[], Mapping[StreamId, Aggregate]]

#: ask の続き（回答のファイルを書いた後の ResumeStage）
_RESUME_ASK = FOLLOW_UPS["resume-ask"]


def _rewrite_question(
    paths: RunPaths,
    aggregates: Mapping[StreamId, Aggregate],
    event: QuestionAnswered | QuestionWithdrawn,
) -> None:
    """回答が届いた・取り下げた質問のファイルを、出したときの本文のまま書き直す。"""
    book = aggregates.get(StreamId.questions())
    asked = book.questions.get(event.question) if isinstance(book, Questions) else None
    body = asked.body if asked is not None else ""
    if isinstance(event, QuestionAnswered):
        write_question(paths, event.question, body, event.escalation, event.answer)
    else:
        write_question(paths, event.question, body, event.escalation, withdrawn=event.reason)


def reactions(
    *,
    paths: RunPaths,
    aggregates: Aggregates,
    inbox: Inbox,
    executor: StageExecutor,
    supervisors: SupervisorRunner,
    prompts: Prompts,
) -> list[Subscriber]:
    """登録する反応（この順に、ポリシーの後ろに登録する）。"""

    def wake_supervisors(delivery: Delivery) -> list[Command]:
        wake = wake_for(delivery.event, delivery.event_id)
        if wake is not None:
            prompt = prompts.wake(wake, delivery, aggregates())
            supervisors.wake(wake.supervisor, prompt, delivery.event_id)
        return []

    def write_questions(delivery: Delivery) -> list[Command]:
        event = delivery.event
        if isinstance(event, QuestionPosted):
            write_question(paths, event.question, event.body, event.escalation)
        elif isinstance(event, (QuestionAnswered, QuestionWithdrawn)):
            _rewrite_question(paths, aggregates(), event)
        return []

    def write_ask_answers(delivery: Delivery) -> list[Command]:
        event = delivery.event
        if not isinstance(event, EscalationResolved) or event.tool_use_id is None:
            return []
        write_answer(paths, event.tool_use_id, event.answer)
        return _RESUME_ASK.receive(event, delivery.event_id)

    def append_design_appendix(delivery: Delivery) -> list[Command]:
        event = delivery.event
        if isinstance(event, DesignSettled):
            append_appendix(paths, event.proposal.design, event.appendix)
        return []

    def begin_stage(delivery: Delivery) -> list[Command]:
        event = delivery.event
        if isinstance(event, StageRequested):
            executor.begin(event.execution, inbox.expect(event.execution))
        return []

    def run_stage(delivery: Delivery) -> list[Command]:
        event = delivery.event
        if not isinstance(event, StageStarted):
            return []
        execution = event.execution
        task = aggregates().get(StreamId.task(execution.task))
        # 配り直した古い StageStarted（もう終わった・止めた実行）と、この driver がもう走らせている
        # 実行（続きから再開した StageStarted が 2 つ並んだ）は起こさない（2 回呼ばれても同じ結果）
        if not isinstance(task, Task) or execution not in task.running_executions():
            return []
        if inbox.holds(execution):
            return []
        executor.run(execution, inbox.expect(execution))
        return []

    def interrupt_stage(delivery: Delivery) -> list[Command]:
        event = delivery.event
        if isinstance(event, StageInterrupted):
            executor.interrupt(event.execution)
        return []

    def restart_execution(delivery: Delivery) -> list[Command]:
        event = delivery.event
        if isinstance(event, ExecutionRestarted):
            executor.restart(event.execution, event.start_commit)
        return []

    def abort_rebase(delivery: Delivery) -> list[Command]:
        event = delivery.event
        if isinstance(event, IntegrationFailed):
            executor.abort_rebase(event.task)
        return []

    named: Sequence[tuple[str, Callable[[Delivery], list[Command]]]] = (
        ("wake-supervisors", wake_supervisors),
        ("write-questions", write_questions),
        ("write-ask-answers", write_ask_answers),
        ("append-design-appendix", append_design_appendix),
        ("begin-stage", begin_stage),
        ("run-stage", run_stage),
        ("interrupt-stage", interrupt_stage),
        ("restart-execution", restart_execution),
        ("abort-rebase", abort_rebase),
    )
    return [Subscriber(name, receive) for name, receive in named]
