"""クラスごとのモデルと effort の設定（`infra/model_config.py`）と `autodev config show` / `set`。

設定ファイルの置き場は XDG_CONFIG_HOME を tmp_path に向けて決め、利用者の ~/.config を読み書きしない。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev import cli
from autodev.domain.value_objects.base import InvalidValue
from autodev.domain.value_objects.model_class import (
    AgentKind,
    Effort,
    ModelChoice,
    ModelClass,
    ModelClasses,
    ModelName,
)
from autodev.infra.model_config import (
    ModelConfigError,
    load_model_classes,
    models_path,
    set_model_class,
)

DEFAULT_JSON = {
    "lead": {"model": "opus", "effort": "high", "agent": "claude"},
    "review": {"model": "opus", "effort": "medium", "agent": "claude"},
    "implement": {"model": "sonnet", "effort": "medium", "agent": "claude"},
    "write": {"model": "sonnet", "effort": "medium", "agent": "claude"},
}


def env(tmp_path: Path) -> dict[str, str]:
    return {"XDG_CONFIG_HOME": str(tmp_path)}


def file_of(tmp_path: Path) -> Path:
    return tmp_path / "autodev" / "models.json"


def write(tmp_path: Path, body: object) -> Path:
    path = file_of(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return path


# --- load_model_classes ---


def test_ファイルが無ければ4つのクラスは既定のモデルとeffort(tmp_path: Path):
    loaded = load_model_classes(env(tmp_path))
    assert loaded.of(ModelClass.LEAD) == ModelChoice(ModelName("opus"), Effort.HIGH)
    assert loaded.of(ModelClass.REVIEW) == ModelChoice(ModelName("opus"), Effort.MEDIUM)
    assert loaded.of(ModelClass.IMPLEMENT) == ModelChoice(ModelName("sonnet"), Effort.MEDIUM)
    assert loaded.of(ModelClass.WRITE) == ModelChoice(ModelName("sonnet"), Effort.MEDIUM)
    assert not file_of(tmp_path).exists()


def test_書いたクラスの書いた欄だけが既定を上書きする(tmp_path: Path):
    write(tmp_path, {"implement": {"effort": "high"}})
    loaded = load_model_classes(env(tmp_path))
    assert loaded.of(ModelClass.IMPLEMENT) == ModelChoice(ModelName("sonnet"), Effort.HIGH)
    default = ModelClasses.default()
    for cls in (ModelClass.LEAD, ModelClass.REVIEW, ModelClass.WRITE):
        assert loaded.of(cls) == default.of(cls)


def test_modelとeffortの両方を書いたクラスは両方替わり別のクラスと組み合わせられる(tmp_path: Path):
    write(
        tmp_path,
        {"lead": {"model": "claude-opus-5-5", "effort": "max"}, "write": {"model": "haiku"}},
    )
    loaded = load_model_classes(env(tmp_path))
    assert loaded == (
        ModelClasses.default()
        .with_choice(ModelClass.LEAD, model=ModelName("claude-opus-5-5"), effort=Effort.MAX)
        .with_choice(ModelClass.WRITE, model=ModelName("haiku"))
    )


def test_置き場はXDG_CONFIG_HOMEの下で空か相対パスなら既定の_config():
    assert models_path({"XDG_CONFIG_HOME": "/tmp/x"}) == Path("/tmp/x/autodev/models.json")
    home = Path.home() / ".config" / "autodev" / "models.json"
    assert models_path({"XDG_CONFIG_HOME": ""}) == home
    assert models_path({}) == home
    assert models_path({"XDG_CONFIG_HOME": "rel/dir"}) == home


def test_XDG_CONFIG_HOMEの下のファイルを読む(tmp_path: Path):
    write(tmp_path, {"review": {"model": "haiku"}})
    assert models_path(env(tmp_path)) == file_of(tmp_path)
    loaded = load_model_classes(env(tmp_path))
    assert loaded.of(ModelClass.REVIEW) == ModelChoice(ModelName("haiku"), Effort.MEDIUM)


@pytest.mark.parametrize(
    "body",
    [
        "{",
        [],
        {"ultra": {}},
        {"lead": {"temperature": 1}},
        {"lead": {"effort": "huge"}},
        {"lead": {"model": ""}},
        {"lead": {"model": 1}},
        {"lead": "opus"},
        {"lead": {"agent": 1}},
    ],
    ids=[
        "壊れたJSON",
        "最上位が配列",
        "知らないクラス",
        "知らない欄",
        "不正なeffort",
        "空のmodel",
        "文字列でないmodel",
        "クラスの値がobjectでない",
        "文字列でないagent",
    ],
)
def test_崩れた設定は既定に戻さずパスを添えたModelConfigErrorにする(tmp_path: Path, body: object):
    path = write(tmp_path, body)
    with pytest.raises(ModelConfigError) as caught:
        load_model_classes(env(tmp_path))
    assert str(path) in str(caught.value)


def test_読めない設定はパスを添えたModelConfigErrorにする(tmp_path: Path):
    # ディレクトリはファイルとして読めない
    path = file_of(tmp_path)
    path.mkdir(parents=True)
    with pytest.raises(ModelConfigError) as caught:
        load_model_classes(env(tmp_path))
    assert str(path) in str(caught.value)


# --- config show / set ---


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


def run_cli(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    capsys.readouterr()
    try:
        code = cli.main(list(args))
    except SystemExit as exit:
        # argparse の引数の誤り（`_Parser` が 1 で抜ける）
        code = exit.code
    out, err = capsys.readouterr()
    return int(code or 0), out, err


def test_config_showはファイルが無ければ既定の4クラスとパスとexistsのfalseを出す(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    code, out, err = run_cli(capsys, "config", "show")
    assert code == 0, err
    assert json.loads(out) == {
        "path": str(file_of(config_home)),
        "exists": False,
        "classes": DEFAULT_JSON,
    }
    assert not file_of(config_home).exists()


def test_config_showは上書きしたクラスを上書き後の値で出す(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    write(config_home, {"implement": {"model": "claude-opus-5-5", "effort": "xhigh"}})
    code, out, err = run_cli(capsys, "config", "show")
    assert code == 0, err
    shown = json.loads(out)
    assert shown["exists"] is True
    assert shown["path"] == str(file_of(config_home))
    assert shown["classes"] == {
        **DEFAULT_JSON,
        "implement": {"model": "claude-opus-5-5", "effort": "xhigh", "agent": "claude"},
    }


def test_config_showは崩れたファイルなら1で止まる(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    path = write(config_home, "{")
    code, out, err = run_cli(capsys, "config", "show")
    assert code == 1
    assert out == ""
    assert str(path) in err


def test_config_setはファイルを作って書き続けて別の欄を足しても前に書いた欄を消さない(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    path = file_of(config_home)
    code, out, err = run_cli(capsys, "config", "set", "--class", "write", "--model", "haiku")
    assert code == 0, err
    assert json.loads(path.read_text("utf-8")) == {"write": {"model": "haiku"}}
    shown = json.loads(out)
    assert shown["exists"] is True
    assert shown["classes"]["write"] == {"model": "haiku", "effort": "medium", "agent": "claude"}

    code, out, err = run_cli(capsys, "config", "set", "--class", "write", "--effort", "low")
    assert code == 0, err
    assert json.loads(path.read_text("utf-8")) == {"write": {"model": "haiku", "effort": "low"}}
    assert json.loads(out)["classes"]["write"] == {
        "model": "haiku",
        "effort": "low",
        "agent": "claude",
    }

    # ほかのクラスに書いた欄も残る
    code, _, err = run_cli(capsys, "config", "set", "--class", "lead", "--effort", "max")
    assert code == 0, err
    assert json.loads(path.read_text("utf-8")) == {
        "write": {"model": "haiku", "effort": "low"},
        "lead": {"effort": "max"},
    }
    code, out, _ = run_cli(capsys, "config", "show")
    assert json.loads(out)["classes"] == {
        **DEFAULT_JSON,
        "lead": {"model": "opus", "effort": "max", "agent": "claude"},
        "write": {"model": "haiku", "effort": "low", "agent": "claude"},
    }


def test_config_setは人が書いた別のクラスの欄を残す(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    path = write(config_home, {"implement": {"model": "claude-opus-5-5", "effort": "xhigh"}})
    code, _, err = run_cli(capsys, "config", "set", "--class", "review", "--model", "haiku")
    assert code == 0, err
    assert json.loads(path.read_text("utf-8")) == {
        "implement": {"model": "claude-opus-5-5", "effort": "xhigh"},
        "review": {"model": "haiku"},
    }


BAD_SETS = [
    ("config", "set", "--class", "write"),
    ("config", "set", "--class", "ultra", "--model", "haiku"),
    ("config", "set", "--class", "write", "--effort", "huge"),
    ("config", "set", "--class", "write", "--model", ""),
    ("config", "set", "--class", "write", "--model", "   "),
    ("config", "set", "--class", "write", "--agent", "gemini"),
    ("config", "set", "--class", "review", "--agent", "codex"),
    (
        "config",
        "set",
        "--class",
        "review",
        "--agent",
        "codex",
        "--model",
        "gpt-5.5",
        "--effort",
        "max",
    ),
    ("config", "set", "--class", "review", "--agent", "claude", "--effort", "minimal"),
]
BAD_SET_IDS = [
    "欄が無い",
    "知らないクラス",
    "不正なeffort",
    "空のmodel",
    "空白のmodel",
    "知らないagent",
    "codexでmodelが無い",
    "codexにmax",
    "claudeにminimal",
]


@pytest.mark.parametrize("args", BAD_SETS, ids=BAD_SET_IDS)
def test_config_setは不正な引数ならファイルが無いときは作らずに1で止まる(
    config_home: Path, capsys: pytest.CaptureFixture[str], args: tuple[str, ...]
):
    code, _, _ = run_cli(capsys, *args)
    assert code == 1
    assert not file_of(config_home).exists()
    assert not (config_home / "autodev").exists()


@pytest.mark.parametrize("args", BAD_SETS, ids=BAD_SET_IDS)
def test_config_setは不正な引数ならあるファイルの中身を変えずに1で止まる(
    config_home: Path, capsys: pytest.CaptureFixture[str], args: tuple[str, ...]
):
    path = write(config_home, {"write": {"model": "sonnet", "effort": "high"}})
    before = path.read_bytes()
    code, _, _ = run_cli(capsys, *args)
    assert code == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "body",
    ["{", [], {"ultra": {}}, {"lead": {"effort": "huge"}}, {"lead": {"agent": "gemini"}}],
    ids=["壊れたJSON", "配列", "知らないクラス", "不正なeffort", "知らないagent"],
)
def test_config_setは今のファイルが崩れていたら書かずにパスを出して1で止まる(
    config_home: Path, capsys: pytest.CaptureFixture[str], body: object
):
    path = write(config_home, body)
    before = path.read_bytes()
    code, _, err = run_cli(capsys, "config", "set", "--class", "write", "--model", "haiku")
    assert code == 1
    assert path.read_bytes() == before
    assert str(path) in err


# --- agent ---

CODEX_REVIEW = {"review": {"agent": "codex", "model": "gpt-5.5", "effort": "high"}}


def test_reviewにcodexを書くとreviewだけがcodexでほかのクラスはclaude(tmp_path: Path):
    write(tmp_path, CODEX_REVIEW)
    loaded = load_model_classes(env(tmp_path))
    assert loaded.of(ModelClass.REVIEW) == ModelChoice(
        ModelName("gpt-5.5"), Effort.HIGH, AgentKind.CODEX
    )
    assert loaded.of(ModelClass.REVIEW).agent is AgentKind.CODEX
    for cls in (ModelClass.LEAD, ModelClass.IMPLEMENT, ModelClass.WRITE):
        assert loaded.of(cls).agent is AgentKind.CLAUDE
        assert loaded.of(cls) == ModelClasses.default().of(cls)


def test_agentを書かない設定は全クラスがclaude(tmp_path: Path):
    write(tmp_path, {"lead": {"model": "opus"}})
    loaded = load_model_classes(env(tmp_path))
    assert {loaded.of(cls).agent for cls in ModelClass} == {AgentKind.CLAUDE}


def test_ファイルが無ければ全クラスがclaude(tmp_path: Path):
    loaded = load_model_classes(env(tmp_path))
    assert {loaded.of(cls).agent for cls in ModelClass} == {AgentKind.CLAUDE}


def test_codexはminimalを受けclaudeは受けない(tmp_path: Path):
    write(tmp_path, {"write": {"agent": "codex", "model": "gpt-5.5", "effort": "minimal"}})
    assert load_model_classes(env(tmp_path)).of(ModelClass.WRITE).effort is Effort.MINIMAL


@pytest.mark.parametrize(
    "body",
    [
        {"review": {"agent": "codex", "model": "gpt-5.5", "effort": "max"}},
        {"review": {"agent": "claude", "effort": "minimal"}},
        {"review": {"effort": "minimal"}},
        {"review": {"agent": "gemini"}},
        {"review": {"agent": "codex", "effort": "high"}},
        {"review": {"agent": "codex"}},
    ],
    ids=[
        "codexにmax",
        "claudeにminimal",
        "agentなしにminimal",
        "知らないagent",
        "codexでmodelなし",
        "codexでmodelもeffortもなし",
    ],
)
def test_agentとeffortとmodelの組み合わせが合わない設定はパスを添えたModelConfigErrorにする(
    tmp_path: Path, body: object
):
    path = write(tmp_path, body)
    with pytest.raises(ModelConfigError) as caught:
        load_model_classes(env(tmp_path))
    assert str(path) in str(caught.value)


def test_agentの規則はクラスごとで別のクラスのmodelでは満たされない(tmp_path: Path):
    write(tmp_path, {"lead": {"model": "opus"}, "review": {"agent": "codex", "effort": "high"}})
    with pytest.raises(ModelConfigError):
        load_model_classes(env(tmp_path))


def test_ModelChoiceはagentが受けないeffortなら作れない():
    with pytest.raises(InvalidValue):
        ModelChoice(ModelName("gpt-5.5"), Effort.MAX, AgentKind.CODEX)
    with pytest.raises(InvalidValue):
        ModelChoice(ModelName("opus"), Effort.MINIMAL, AgentKind.CLAUDE)
    with pytest.raises(InvalidValue):
        ModelChoice(ModelName("opus"), Effort.MINIMAL)


def test_AgentKindが受けるeffort():
    assert AgentKind.CLAUDE.efforts == {
        Effort.LOW,
        Effort.MEDIUM,
        Effort.HIGH,
        Effort.XHIGH,
        Effort.MAX,
    }
    assert AgentKind.CODEX.efforts == {
        Effort.MINIMAL,
        Effort.LOW,
        Effort.MEDIUM,
        Effort.HIGH,
        Effort.XHIGH,
    }


def test_with_choiceはagentを替えられ替えない欄は残す():
    chosen = ModelClasses.default().with_choice(
        ModelClass.REVIEW, model=ModelName("gpt-5.5"), agent=AgentKind.CODEX
    )
    assert chosen.of(ModelClass.REVIEW) == ModelChoice(
        ModelName("gpt-5.5"), Effort.MEDIUM, AgentKind.CODEX
    )
    assert chosen.of(ModelClass.LEAD) == ModelClasses.default().of(ModelClass.LEAD)


# --- config set --agent ---


def test_config_setのagentはcodexとmodelを書きJSONにもagentを出す(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    code, out, err = run_cli(
        capsys, "config", "set", "--class", "review", "--agent", "codex", "--model", "gpt-5.5"
    )
    assert code == 0, err
    assert json.loads(file_of(config_home).read_text("utf-8")) == {
        "review": {"agent": "codex", "model": "gpt-5.5"}
    }
    assert json.loads(out)["classes"]["review"]["agent"] == "codex"
    assert json.loads(out)["classes"]["review"]["model"] == "gpt-5.5"
    assert json.loads(out)["classes"]["lead"]["agent"] == "claude"


def test_config_setはagentだけでも書けmodelが既にあるクラスをcodexにできる(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    write(config_home, {"review": {"model": "gpt-5.5"}})
    code, out, err = run_cli(capsys, "config", "set", "--class", "review", "--agent", "codex")
    assert code == 0, err
    assert json.loads(file_of(config_home).read_text("utf-8")) == {
        "review": {"model": "gpt-5.5", "agent": "codex"}
    }
    assert json.loads(out)["classes"]["review"]["agent"] == "codex"


def test_config_setのeffortにminimalはcodexなら通る(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    code, _, err = run_cli(
        capsys,
        "config",
        "set",
        "--class",
        "review",
        "--agent",
        "codex",
        "--model",
        "gpt-5.5",
        "--effort",
        "minimal",
    )
    assert code == 0, err
    code, out, _ = run_cli(capsys, "config", "show")
    assert json.loads(out)["classes"]["review"] == {
        "model": "gpt-5.5",
        "effort": "minimal",
        "agent": "codex",
    }


def test_config_setはclaudeに戻すとminimalが残るなら書かずに1で止まる(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    path = write(
        config_home, {"review": {"agent": "codex", "model": "gpt-5.5", "effort": "minimal"}}
    )
    before = path.read_bytes()
    code, _, _ = run_cli(capsys, "config", "set", "--class", "review", "--agent", "claude")
    assert code == 1
    assert path.read_bytes() == before


def test_config_setはcodexのreviewを_modelなしでclaudeに戻すなら書かずに1で止まる(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    path = write(config_home, {"review": {"agent": "codex", "model": "gpt-5.5"}})
    before = path.read_bytes()
    code, _, err = run_cli(capsys, "config", "set", "--class", "review", "--agent", "claude")
    assert code == 1
    assert "--model" in err
    assert path.read_bytes() == before


def test_set_model_classはcodexのreviewを_modelなしでclaudeに戻すとModelConfigErrorで中身を変えない(
    config_home: Path,
):
    path = write(config_home, {"review": {"agent": "codex", "model": "gpt-5.5"}})
    before = path.read_bytes()
    with pytest.raises(ModelConfigError):
        set_model_class(ModelClass.REVIEW, agent=AgentKind.CLAUDE, env=env(config_home))
    assert path.read_bytes() == before


def test_config_showはagentを書いたクラスをcodexで出す(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    write(config_home, CODEX_REVIEW)
    code, out, err = run_cli(capsys, "config", "show")
    assert code == 0, err
    assert json.loads(out)["classes"] == {
        **DEFAULT_JSON,
        "review": {"model": "gpt-5.5", "effort": "high", "agent": "codex"},
    }


# --- agent copilot ---

COPILOT_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def test_copilotはnoneを受けclaudeとcodexは受けない():
    ModelChoice(ModelName("gpt-5.5"), Effort.NONE, AgentKind.COPILOT)
    with pytest.raises(InvalidValue):
        ModelChoice(ModelName("opus"), Effort.NONE, AgentKind.CLAUDE)
    with pytest.raises(InvalidValue):
        ModelChoice(ModelName("gpt-5.5"), Effort.NONE, AgentKind.CODEX)


def test_copilotはmaxとminimalの両方を受ける():
    ModelChoice(ModelName("claude-sonnet-4.5"), Effort.MAX, AgentKind.COPILOT)
    ModelChoice(ModelName("claude-sonnet-4.5"), Effort.MINIMAL, AgentKind.COPILOT)


def test_AgentKindのcopilotが受けるeffortはnoneからmaxの7つ():
    assert AgentKind.COPILOT.efforts == {
        Effort.NONE,
        Effort.MINIMAL,
        Effort.LOW,
        Effort.MEDIUM,
        Effort.HIGH,
        Effort.XHIGH,
        Effort.MAX,
    }
    assert Effort.NONE not in AgentKind.CLAUDE.efforts
    assert Effort.NONE not in AgentKind.CODEX.efforts


@pytest.mark.parametrize("name", ["auto", "Auto"])
def test_copilotのModelChoiceはautoを大文字小文字を問わず拒む(name: str):
    with pytest.raises(InvalidValue):
        ModelChoice(ModelName(name), Effort.HIGH, AgentKind.COPILOT)


@pytest.mark.parametrize("name", ["auto", "Auto"])
def test_ModelNameのautoは作れclaudeのModelChoiceも今のまま通る(name: str):
    ModelName(name)
    ModelChoice(ModelName(name), Effort.HIGH)


def test_modelsjsonのcopilotはlead_だけを替えほかは既定のclaude(tmp_path: Path):
    write(
        tmp_path,
        {"lead": {"agent": "copilot", "model": "gpt-5.5", "effort": "high"}},
    )
    loaded = load_model_classes(env(tmp_path))
    assert loaded.of(ModelClass.LEAD) == ModelChoice(
        ModelName("gpt-5.5"), Effort.HIGH, AgentKind.COPILOT
    )
    defaults = ModelClasses.default()
    for cls in (ModelClass.REVIEW, ModelClass.IMPLEMENT, ModelClass.WRITE):
        assert loaded.of(cls) == defaults.of(cls)
        assert loaded.of(cls).agent is AgentKind.CLAUDE


@pytest.mark.parametrize("cls", [c.value for c in ModelClass])
def test_modelsjsonのcopilotは4クラスのどれにも書ける(tmp_path: Path, cls: str):
    write(tmp_path, {cls: {"agent": "copilot", "model": "gpt-5.5", "effort": "none"}})
    assert load_model_classes(env(tmp_path)).of(ModelClass(cls)) == ModelChoice(
        ModelName("gpt-5.5"), Effort.NONE, AgentKind.COPILOT
    )


@pytest.mark.parametrize(
    "body",
    [
        {"review": {"agent": "copilot", "effort": "high"}},
        {"review": {"agent": "copilot"}},
        {"lead": {"model": "opus"}, "review": {"agent": "copilot", "effort": "high"}},
        {"lead": {"agent": "copilot", "model": "auto"}},
        {"lead": {"agent": "copilot", "model": "Auto", "effort": "high"}},
        {"lead": {"agent": "copilot", "model": "gpt-5.5", "effort": "ultra"}},
        {"lead": {"agent": "claude", "model": "opus", "effort": "none"}},
        {"lead": {"agent": "codex", "model": "gpt-5.5", "effort": "none"}},
    ],
    ids=[
        "modelなし",
        "modelもeffortもなし",
        "別のクラスのmodelでは満たされない",
        "auto",
        "Auto",
        "知らないeffort",
        "claudeにnone",
        "codexにnone",
    ],
)
def test_copilotの設定が合わなければパスを添えたModelConfigErrorにする(
    tmp_path: Path, body: object
):
    path = write(tmp_path, body)
    with pytest.raises(ModelConfigError) as caught:
        load_model_classes(env(tmp_path))
    assert str(path) in str(caught.value)


@pytest.mark.parametrize("cls", list(ModelClass))
def test_set_model_classはどのクラスにもcopilotを書ける(config_home: Path, cls: ModelClass):
    merged = set_model_class(
        cls,
        agent=AgentKind.COPILOT,
        model=ModelName("gpt-5.5"),
        effort=Effort.MEDIUM,
        env=env(config_home),
    )
    assert merged.of(cls) == ModelChoice(ModelName("gpt-5.5"), Effort.MEDIUM, AgentKind.COPILOT)
    assert json.loads(file_of(config_home).read_text("utf-8")) == {
        cls.value: {"agent": "copilot", "model": "gpt-5.5", "effort": "medium"}
    }


def test_set_model_classはmodelなしのcopilotならModelConfigErrorでファイルを作らない(
    config_home: Path,
):
    with pytest.raises(ModelConfigError):
        set_model_class(ModelClass.LEAD, agent=AgentKind.COPILOT, env=env(config_home))
    assert not file_of(config_home).exists()


@pytest.mark.parametrize("agent", [AgentKind.CLAUDE, AgentKind.CODEX])
def test_set_model_classはcopilotのreviewを_modelなしで切り替えるとModelConfigErrorで中身を変えない(
    config_home: Path, agent: AgentKind
):
    path = write(config_home, {"review": {"agent": "copilot", "model": "gpt-5.5"}})
    before = path.read_bytes()
    with pytest.raises(ModelConfigError):
        set_model_class(ModelClass.REVIEW, agent=agent, env=env(config_home))
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", ["auto", "Auto"])
def test_set_model_classはcopilotのautoを拒み何も書かない(config_home: Path, name: str):
    with pytest.raises(ModelConfigError):
        set_model_class(
            ModelClass.LEAD,
            agent=AgentKind.COPILOT,
            model=ModelName(name),
            env=env(config_home),
        )
    assert not file_of(config_home).exists()


def test_set_model_classはcopilotのautoを拒むとき既にあるファイルを変えない(config_home: Path):
    path = write(config_home, {"lead": {"agent": "copilot", "model": "gpt-5.5"}})
    before = path.read_bytes()
    with pytest.raises(ModelConfigError):
        set_model_class(ModelClass.LEAD, model=ModelName("auto"), env=env(config_home))
    assert path.read_bytes() == before


@pytest.mark.parametrize("effort", COPILOT_EFFORTS)
def test_config_setのcopilotは7つのeffortを受けてconfig_showに出す(
    config_home: Path, capsys: pytest.CaptureFixture[str], effort: str
):
    code, _, err = run_cli(
        capsys,
        "config",
        "set",
        "--class",
        "implement",
        "--agent",
        "copilot",
        "--model",
        "gpt-5.5",
        "--effort",
        effort,
    )
    assert code == 0, err
    code, out, err = run_cli(capsys, "config", "show")
    assert code == 0, err
    assert json.loads(out)["classes"]["implement"] == {
        "model": "gpt-5.5",
        "effort": effort,
        "agent": "copilot",
    }


def test_config_setのcopilotにautoを渡すと1で止まりファイルを作らない(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    code, _, err = run_cli(
        capsys, "config", "set", "--class", "lead", "--agent", "copilot", "--model", "auto"
    )
    assert code != 0
    assert err
    assert not file_of(config_home).exists()


def test_config_setのclaudeにnoneを渡すと1で止まりファイルを作らない(
    config_home: Path, capsys: pytest.CaptureFixture[str]
):
    code, _, _ = run_cli(capsys, "config", "set", "--class", "lead", "--effort", "none")
    assert code != 0
    assert not file_of(config_home).exists()


def test_壊れた設定の案内文にcopilotとnoneが出る(tmp_path: Path):
    write(tmp_path, {"lead": {"agent": "copilot", "effort": "high"}})
    with pytest.raises(ModelConfigError) as caught:
        load_model_classes(env(tmp_path))
    assert "copilot" in str(caught.value)
    assert "none" in str(caught.value)
