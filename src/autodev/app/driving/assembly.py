"""CLI が使う組み立て: 本物の実行器と AgentRuntime で driver を組む・走り出す前に道具を確かめる・
ランディレクトリの記録を読む。

ここは判断を持たない。記録を読むときは集約を再生してドメインに聞き（`Task.running_executions`・
`Questions.handle`）、答えをそのまま CLI に返す。
"""

from __future__ import annotations

from pathlib import Path

from ...adapters.agent.router import AgentRouter
from ...adapters.claude.agent_runtime import AgentRuntime
from ...adapters.codex.agent_runtime import CodexRuntime
from ...adapters.copilot.agent_runtime import CopilotRuntime
from ...adapters.github import git
from ...adapters.github.forge import Forge
from ...domain.aggregates.base import Rejected
from ...domain.aggregates.questions import Questions
from ...domain.aggregates.run import Run
from ...domain.aggregates.task import Task
from ...domain.commands.questions import AnswerQuestion
from ...domain.events.run import RunStarted
from ...domain.value_objects.execution_id import ExecutionId
from ...domain.value_objects.model_class import AgentKind, ModelClass, ModelClasses
from ...domain.value_objects.stream_id import StreamId
from ...infra.paths import RunPaths
from ...infra.repo_config import RepoConfig
from ...infra.store.eventstore import EventReader
from ...infra.store.requests import RequestBox, UnreadableRequest
from ..stages.executor import from_parts
from .driver import Driver


def build_driver(paths: RunPaths, config: RepoConfig) -> Driver:
    """ステージと統括は同じ AgentRouter で起こす（Claude・Codex・Copilot を登録）。claude と gh は PATH から探す（検査は偽物を先に置く）。"""
    agent = AgentRouter(
        {
            AgentKind.CLAUDE: AgentRuntime(),
            AgentKind.CODEX: CodexRuntime(),
            AgentKind.COPILOT: CopilotRuntime(),
        }
    )
    arguments = config.executor_arguments()
    return Driver(
        paths,
        executor=lambda parts: from_parts(parts, agent, **arguments),
        runtime=agent,
    )


def missing_tools(cwd: Path, models: ModelClasses) -> list[str]:
    """走り出す前に足りない道具。1 つでもあれば走らない（途中で気づくと、worktree と
    概要 PR だけが残る）。claude・codex・copilot は、`models` がそのエージェントを選ぶときだけ確かめる。"""
    missing: list[str] = []
    agents = {models.of(cls).agent for cls in ModelClass}
    if AgentKind.CLAUDE in agents and not AgentRuntime().available():
        missing.append("claude が起動できない（PATH にあるか、`claude --version` が通るか）")
    if AgentKind.CODEX in agents:
        missing += CodexRuntime().missing()
    if AgentKind.COPILOT in agents:
        missing += CopilotRuntime().missing()
    if not git.available():
        missing.append("git が PATH に無い")
    missing += Forge().missing(cwd)
    return missing


# --- ランディレクトリの記録 ---


def run_started(paths: RunPaths) -> RunStarted | None:
    """そのランの RunStarted。events.db が無い・まだ始めていないなら None。"""
    if not paths.events_db.is_file():
        return None
    with EventReader.open(paths.events_db) as reader:
        for stored in reader.read_stream(StreamId.run()):
            event = stored.decode()
            if isinstance(event, RunStarted):
                return event
    return None


def run_aggregate(paths: RunPaths) -> Run:
    """再生した Run。events.db が無ければ、まだ何も起きていない Run。"""
    if not paths.events_db.is_file():
        return Run(StreamId.run())
    with EventReader.open(paths.events_db) as reader:
        history = [(s.decode(), s.command_id) for s in reader.read_stream(StreamId.run())]
    return Run.replay(StreamId.run(), history)


def running_executions(paths: RunPaths) -> list[ExecutionId]:
    """記録の上で running のまま残っている実行。driver が落ちた後は、走っていなくても残る。"""
    if not paths.events_db.is_file():
        return []
    found: list[ExecutionId] = []
    with EventReader.open(paths.events_db) as reader:
        streams = sorted({stored.stream for stored in reader.read_all()}, key=str)
        for stream in streams:
            if stream.is_task:
                history = [(s.decode(), s.command_id) for s in reader.read_stream(stream)]
                found += Task.replay(stream, history).running_executions()
    return found


def answer_refusal(paths: RunPaths, command: AnswerQuestion) -> str | None:
    """要求を足す前に、Questions がその回答を受けるかを確かめる。受けないなら理由（取り下げた質問なら、
    取り下げた理由も入る。S6）。

    driver が要求を拾った所で拒むと `rejected.jsonl` に残るだけで、`/autodev` は気づけない。判断は
    `Questions.handle` のもので、ここは状態を変えずに借りるだけ。

    driver がまだ拾っていない回答（`requests` に残る AnswerQuestion）も、拾われる順に先に当てる。
    当てずに確かめると、同じ質問への 2 つ目の回答が通ったように見え、driver が拾った所で黙って拒む。
    """
    # requests を先に、events を後に読む。driver は回答をイベントにしてから requests の行を消すので、
    # 2 回の読み取りの間に拾われても、回答はどちらかに必ず残る（逆の順だと、両方から消えうる）。
    # 両方に出た回答は、イベントを当てた後なので当てたときに拒まれ、飛ばされる
    with RequestBox.open(paths.events_db) as box:
        pending = box.pending()
    with EventReader.open(paths.events_db) as reader:
        history = [(s.decode(), s.command_id) for s in reader.read_stream(StreamId.questions())]
    questions = Questions.replay(StreamId.questions(), history)
    for request in pending:
        try:
            earlier = request.to_command()
        except UnreadableRequest:
            continue
        if not isinstance(earlier, AnswerQuestion):
            continue
        try:
            events = questions.handle(earlier)
        except Rejected:
            # driver も拒むので、何も当たらない
            continue
        for event in events:
            questions.apply(event, earlier.command_id)
    try:
        questions.handle(command)
    except Rejected as rejected:
        return str(rejected)
    return None
