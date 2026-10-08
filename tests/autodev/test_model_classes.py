"""モデルのクラス: ステージと統括の claude の argv に付く `--model`・`--effort` と、ランへの記録。

ステージは本物の実行器（`app/stages/executor.py`）を偽の AgentRuntime で走らせ、組んだ AgentCall を
本物の argv の組み方（`argv_for`）に通して見る。統括は組み立ての根（`app/driving/driver.py`）から
走らせ、偽の AgentRuntime が受けた AgentCall を同じく argv にする。claude は起動しない。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from autodev_drive import NAME, FakeExecutor, Rig, View, is_task_supervisor
from executor_fakes import Env, make_env, outcome
from test_seams import main_script

from autodev.adapters.agent.types import AgentCall
from autodev.adapters.claude.agent_runtime import argv_for
from autodev.app.driving.driver import Driver, DriverParts, StartRequest
from autodev.app.driving.mainloop import Delivery
from autodev.app.stages.executor import SHAPE_CORRECTIONS, from_parts
from autodev.app.stages.stage_context import RunSetting, run_setting
from autodev.domain import codec
from autodev.domain.aggregates.run import Run
from autodev.domain.aggregates.task import Task
from autodev.domain.commands.run import StartRun
from autodev.domain.events.run import RunStarted
from autodev.domain.events.task import FlowAccepted, StageRequested, StageStarted, TaskOpened
from autodev.domain.flow.flow import Flow, FlowStep
from autodev.domain.stages.catalog import STAGE_SPECS
from autodev.domain.stages.kinds import StageMode
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.commit_sha import CommitSha
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.instruction import Instruction
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.model_class import (
    AgentKind,
    Effort,
    ModelChoice,
    ModelClass,
    ModelClasses,
    ModelName,
)
from autodev.domain.value_objects.parallel_limit import ParallelLimit
from autodev.domain.value_objects.repository import Repository
from autodev.domain.value_objects.run_name import RunName
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_kind import TaskKind
from autodev.infra.paths import RunPaths

S = StageKind
HEAD = CommitSha("a" * 40)

#: 設定を何も渡さずに始めたランで、ステージの argv に付く（モデル, effort）
DEFAULT_STAGE_FLAGS = {
    S.PLAN: ("opus", "high"),
    S.REPLAN: ("opus", "high"),
    S.REVISE: ("opus", "high"),
    S.DESIGN_JUDGE: ("opus", "high"),
    S.JUDGE: ("opus", "high"),
    S.DESIGN_REVIEW: ("opus", "medium"),
    S.REVIEW: ("opus", "medium"),
    S.ADVERSARIAL_REVIEW: ("opus", "medium"),
    S.TEST_GEN: ("sonnet", "medium"),
    S.IMPL: ("sonnet", "medium"),
    S.FIX: ("sonnet", "medium"),
    S.TEST_FIX: ("sonnet", "medium"),
    S.EXPECT: ("sonnet", "medium"),
    S.RESOLVE_CONFLICT: ("sonnet", "medium"),
    S.WRITE_PR_BODY: ("sonnet", "medium"),
    S.WRITE_OVERVIEW: ("sonnet", "medium"),
}


def implement_xhigh() -> ModelClasses:
    """implement だけを claude-opus-5-5 / xhigh に替えた値。"""
    return ModelClasses.default().with_choice(
        ModelClass.IMPLEMENT, model=ModelName("claude-opus-5-5"), effort=Effort.XHIGH
    )


def flags(argv: list[str]) -> tuple[str | None, str | None]:
    """argv の `--model` と `--effort` の値。"""

    def value(name: str) -> str | None:
        return argv[argv.index(name) + 1] if name in argv else None

    return value("--model"), value("--effort")


def _task_of(kind: StageKind) -> tuple[TaskId, TaskKind]:
    task_kind = sorted(STAGE_SPECS[kind].task_kinds, key=lambda k: k.value)[0]
    if task_kind is TaskKind.PLANNING:
        return TaskId.planning(), task_kind
    if task_kind is TaskKind.GIT:
        return TaskId.git(), task_kind
    return TaskId("task1"), task_kind


def stage_argv(env: Env, kind: StageKind) -> list[str]:
    """`kind` の実行が始まったタスクを置き、実行器に走らせて、claude に渡す argv を返す。"""
    return argv_for(stage_call(env, kind))


def stage_call(env: Env, kind: StageKind) -> AgentCall:
    """`kind` の実行が始まったタスクを置き、実行器に走らせて、エージェントに渡す AgentCall を返す。"""
    spec = STAGE_SPECS[kind]
    task, task_kind = _task_of(kind)
    execution = ExecutionId(task, kind, 1 if spec.parent is not None else 0, 1)
    stream = StreamId.task(task)
    aggregate = Task(stream)
    events = [
        TaskOpened(task_kind),
        FlowAccepted(Flow((FlowStep(spec.parent or kind),), 1)),
        StageRequested(execution, 0),
        StageStarted(execution, HEAD, env.sessions[0]),
    ]
    for number, event in enumerate(events):
        aggregate.apply(event, CommandId(f"seed{number}"))
    env.world.aggregates[stream] = aggregate
    before = len(env.runtime.calls)
    # 結果を返さないので、実行器は形の違いを伝えて返し直させる。最初の起動の argv だけを見る
    for _ in range(1 + SHAPE_CORRECTIONS):
        env.runtime.behaviors.append(lambda call, process: outcome(call, None))
    env.run(execution)
    return env.runtime.calls[before]


def drive(
    rig: Rig, *, start: bool = False, models: ModelClasses | None = None
) -> tuple[int, DriverParts]:
    """`Rig.drive` と同じく driver を組み直して走らせる。始めるときは `models` を StartRun に載せる。"""
    holder: list[Driver] = []
    kept: list[DriverParts] = []

    def make(parts: DriverParts) -> FakeExecutor:
        kept.append(parts)
        executor = FakeExecutor(parts, rig.script, lambda: View(holder[0]), rig.counters)
        rig.executors.append(executor)
        return executor

    driver = Driver(
        rig.paths,
        executor=make,
        runtime=rig.runtime,
        status_command="autodev status --json",
        poll_interval=0.005,
    )
    holder.append(driver)
    rig.driver = driver
    request = None
    if start:
        request = StartRequest(
            StartRun(
                command_id=CommandId(f"start/{NAME}"),
                issuer=Issuer.cli(),
                name=NAME,
                instruction=Instruction("キャッシュを足す"),
                repository=Repository(str(rig.root / "repo")),
                base=BranchName("main"),
                models=ModelClasses.default() if models is None else models,
            )
        )
    return int(driver.drive(request)), kept[0]


def recorded_setting(parts: DriverParts) -> RunSetting:
    """組み立ての根が実行器に渡す、ランの間変わらない値（RunStarted から組む）。"""
    return from_parts(parts, runtime=None).setting  # ty: ignore[invalid-argument-type]


def supervisor_flags(rig: Rig) -> tuple[set, set]:
    """（ラン統括の argv の値の集まり, タスク統括の argv の値の集まり）。"""
    run, task = set(), set()
    for call, _ in rig.runtime.calls:
        (task if is_task_supervisor(call) else run).add(flags(argv_for(call)))
    return run, task


# --- 設定を何も渡さずに始めたラン ---


def test_LLMのステージは表のすべてに既定のモデルとeffortが決まっている():
    llm = {kind for kind, spec in STAGE_SPECS.items() if spec.mode is StageMode.LLM}
    assert llm == set(DEFAULT_STAGE_FLAGS)


@pytest.mark.parametrize("kind", list(DEFAULT_STAGE_FLAGS), ids=lambda kind: kind.value)
def test_既定のランではステージのクラスに当たるモデルとeffortをargvに付ける(
    kind: StageKind, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env = make_env(tmp_path, monkeypatch)
    assert flags(stage_argv(env, kind)) == DEFAULT_STAGE_FLAGS[kind]


def test_StartRunのmodelsを渡さずに始めたランは既定のモデルとeffortを記録して実行器に渡す(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = Rig(tmp_path / "runs", main_script)
    code, parts = drive(rig, start=True)
    assert code == 0
    setting = recorded_setting(parts)
    assert setting.models == ModelClasses.default()
    env = make_env(tmp_path, monkeypatch, models=setting.models)
    assert flags(stage_argv(env, S.PLAN)) == ("opus", "high")
    assert flags(stage_argv(env, S.IMPL)) == ("sonnet", "medium")


# --- StartRun.models で替えたラン ---


def test_implementを替えて始めたランではImplは替えた値でPlanは既定のまま(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = Rig(tmp_path / "runs", main_script)
    code, parts = drive(rig, start=True, models=implement_xhigh())
    assert code == 0
    env = make_env(tmp_path, monkeypatch, models=recorded_setting(parts).models)
    assert flags(stage_argv(env, S.IMPL)) == ("claude-opus-5-5", "xhigh")
    assert flags(stage_argv(env, S.TEST_GEN)) == ("claude-opus-5-5", "xhigh")
    assert flags(stage_argv(env, S.PLAN)) == ("opus", "high")
    assert flags(stage_argv(env, S.REVIEW)) == ("opus", "medium")
    assert flags(stage_argv(env, S.WRITE_PR_BODY)) == ("sonnet", "medium")


def test_implementを替えて始めたランをdriverを組み直して呼び直してもImplは記録した値のまま(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = Rig(tmp_path / "runs", main_script)
    assert drive(rig, start=True, models=implement_xhigh())[0] == 0
    # 同じ events.db を再生して組み直す。StartRun は出さない
    code, parts = drive(rig)
    assert code == 0
    env = make_env(tmp_path, monkeypatch, models=recorded_setting(parts).models)
    assert flags(stage_argv(env, S.IMPL)) == ("claude-opus-5-5", "xhigh")
    assert flags(stage_argv(env, S.PLAN)) == ("opus", "high")


def test_leadを替えて始めたランでは統括も替えた値で起こしreviewを替えても統括は変わらない(
    tmp_path: Path,
):
    models = (
        ModelClasses.default()
        .with_choice(ModelClass.LEAD, model=ModelName("claude-opus-5-5"), effort=Effort.MAX)
        .with_choice(ModelClass.REVIEW, effort=Effort.LOW)
    )
    rig = Rig(tmp_path, main_script)
    code, _ = drive(rig, start=True, models=models)
    assert code == 0
    run, task = supervisor_flags(rig)
    assert run == {("claude-opus-5-5", "max")}
    assert task == {("claude-opus-5-5", "max")}


def test_reviewをcodexにしたランはDesignReviewにcodexをPlanにclaudeをランディレクトリ付きで渡す(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    models = ModelClasses.default().with_choice(
        ModelClass.REVIEW, model=ModelName("gpt-5.5"), agent=AgentKind.CODEX
    )
    env = make_env(tmp_path, monkeypatch, models=models)
    review = stage_call(env, S.DESIGN_REVIEW)
    plan = stage_call(env, S.PLAN)
    assert review.agent is AgentKind.CODEX
    assert plan.agent is AgentKind.CLAUDE
    assert review.run_dir == str(env.paths.root)
    assert plan.run_dir == str(env.paths.root)


def test_leadをcodexにしたランは統括をcodexでランディレクトリ付きで起こす(tmp_path: Path):
    models = ModelClasses.default().with_choice(
        ModelClass.LEAD, model=ModelName("gpt-5.5"), agent=AgentKind.CODEX
    )
    rig = Rig(tmp_path, main_script)
    code, _ = drive(rig, start=True, models=models)
    assert code == 0
    calls = [call for call, _ in rig.runtime.calls]
    assert calls
    assert {call.agent for call in calls} == {AgentKind.CODEX}
    assert {call.run_dir for call in calls} == {str(rig.paths.root)}


# --- この変更より前に始めたラン ---


def old_run_started() -> RunStarted:
    """models の欄を持たない、この変更より前の形の JSON から読んだ RunStarted。"""
    started = RunStarted(
        RunName("r"),
        Instruction("足す"),
        Repository("/repo"),
        BranchName("main"),
        ParallelLimit(ParallelLimit.DEFAULT),
    )
    data = codec.to_json(started)
    assert isinstance(data, dict)
    data.pop("models", None)
    return codec.from_json(RunStarted, data)


def test_modelsの欄が無いRunStartedを読めてそのランのImplは既定のモデルとeffortで動く(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    old = old_run_started()
    assert old.models is None
    history = [Delivery(1, EventId("run#1"), old, CommandId("c1"), "t")]
    setting = run_setting(RunPaths(RunName("r"), tmp_path / "old"), history)
    assert setting.models == ModelClasses.default()
    env = make_env(tmp_path, monkeypatch, models=setting.models)
    assert flags(stage_argv(env, S.IMPL)) == ("sonnet", "medium")


def test_modelsの欄が無いRunStartedを再生したRunは既定の値を持つ():
    run = Run(StreamId.run())
    assert run.models == ModelClasses.default()
    run.apply(old_run_started(), CommandId("c1"))
    assert run.models == ModelClasses.default()


def models_json(agent: str | None) -> dict:
    """RunStarted の JSON のうち、models の各クラスから agent の欄を除いた（または入れた）形。"""
    started = dataclasses.replace(old_run_started(), models=ModelClasses.default())
    data = codec.to_json(started)
    assert isinstance(data, dict)
    for choice in data["models"].values():
        choice.pop("agent", None)
        if agent is not None:
            choice["agent"] = agent
    return data


def test_agentの欄を持たないmodelsのJSONで書かれたRunStartedは全クラスclaudeとして読む():
    started = codec.from_json(RunStarted, models_json(None))
    assert started.models == ModelClasses.default()
    assert started.models is not None
    assert {started.models.of(cls).agent for cls in ModelClass} == {AgentKind.CLAUDE}


def test_codexを記録したRunStartedはJSONを往復してもcodexのまま():
    models = ModelClasses.default().with_choice(
        ModelClass.REVIEW, model=ModelName("gpt-5.5"), effort=Effort.MINIMAL, agent=AgentKind.CODEX
    )
    started = dataclasses.replace(old_run_started(), models=models)
    data = codec.to_json(started)
    assert isinstance(data, dict)
    assert data["models"]["review"]["agent"] == "codex"
    back = codec.from_json(RunStarted, data)
    assert back.models is not None
    assert back.models.of(ModelClass.REVIEW) == ModelChoice(
        ModelName("gpt-5.5"), Effort.MINIMAL, AgentKind.CODEX
    )
    assert back.models.of(ModelClass.LEAD).agent is AgentKind.CLAUDE


def test_Runは記録されたcodexを再生してmodelsに持つ():
    models = ModelClasses.default().with_choice(
        ModelClass.REVIEW, model=ModelName("gpt-5.5"), agent=AgentKind.CODEX
    )
    run = Run(StreamId.run())
    run.apply(dataclasses.replace(old_run_started(), models=models), CommandId("c1"))
    assert run.models.of(ModelClass.REVIEW).agent is AgentKind.CODEX


# --- StageSpec の検査 ---


def test_LLMのステージなのにクラスを持たないStageSpecは作れない():
    with pytest.raises(ValueError):
        dataclasses.replace(STAGE_SPECS[S.IMPL], model_class=None)


@pytest.mark.parametrize("kind", [S.GATE, S.REVIEW_LOOP], ids=lambda kind: kind.value)
def test_決定的なステージと合成ステージはクラスを持つStageSpecを作れない(kind: StageKind):
    assert STAGE_SPECS[kind].mode is not StageMode.LLM
    with pytest.raises(ValueError):
        dataclasses.replace(STAGE_SPECS[kind], model_class=ModelClass.IMPLEMENT)
