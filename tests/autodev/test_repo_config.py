"""リポジトリごとの設定（`infra/repo_config.py`）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.domain.value_objects.glob_pattern import DEFAULT_TEST_GLOBS, GlobPattern
from autodev.domain.value_objects.repository import Repository
from autodev.domain.value_objects.verify_command import VerifyCommand
from autodev.infra.repo_config import (
    RepoConfig,
    RepoConfigError,
    config_path,
    load_repo_config,
)

REPO = Repository("/home/me/src/github.com/o/r")


def env(tmp_path: Path) -> dict[str, str]:
    return {"XDG_CONFIG_HOME": str(tmp_path)}


def write(tmp_path: Path, body: object) -> Path:
    path = config_path(REPO, env(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return path


def test_置き場はXDG_CONFIG_HOMEの下でスラッグはパスのスラッシュとコロンを替えたもの(
    tmp_path: Path,
):
    assert config_path(REPO, env(tmp_path)) == (
        tmp_path / "autodev" / "repos" / "home__me__src__github.com__o__r.json"
    )
    assert config_path(Repository("/c:/x"), env(tmp_path)).name == "c___x.json"
    # 相対パスの XDG_CONFIG_HOME は無効として既定に戻す
    assert config_path(REPO, {"XDG_CONFIG_HOME": "rel"}) == (
        Path.home() / ".config" / "autodev" / "repos" / "home__me__src__github.com__o__r.json"
    )


def test_ファイルが無ければ既定で読み読んだファイルは無いと残す(tmp_path: Path):
    assert load_repo_config(REPO, env(tmp_path)) == RepoConfig()
    assert RepoConfig().test_globs == DEFAULT_TEST_GLOBS


def test_6つの欄を読み省いた欄は既定にしtestGlobsは既定を置き換える(tmp_path: Path):
    path = write(
        tmp_path,
        {
            "quickChecks": ["uv run ruff check ."],
            "regressionTests": ["uv run pytest"],
            "testGlobs": ["spec/**"],
        },
    )
    config = load_repo_config(REPO, env(tmp_path))
    assert config == RepoConfig(
        quick_checks=(VerifyCommand("uv run ruff check ."),),
        regression_tests=(VerifyCommand("uv run pytest"),),
        test_globs=(GlobPattern("spec/**"),),
        source=path,
    )
    write(tmp_path, {"protected": ["uv.lock"], "untested": ["docs/**"]})
    assert load_repo_config(REPO, env(tmp_path)).executor_arguments() == {
        "quick_checks": (),
        "regression_tests": (),
        "test_globs": DEFAULT_TEST_GLOBS,
        "protected_globs": (GlobPattern("uv.lock"),),
        "untested_globs": (GlobPattern("docs/**"),),
    }


def test_同じコマンドをquickChecksとregressionTestsの両方に書くと両方に入る(tmp_path: Path):
    write(tmp_path, {"quickChecks": ["uv run ruff"], "regressionTests": ["uv run ruff"]})
    config = load_repo_config(REPO, env(tmp_path))
    assert config.quick_checks == (VerifyCommand("uv run ruff"),)
    assert config.regression_tests == (VerifyCommand("uv run ruff"),)


def test_空のobjectでは軽い検査も回帰テストも空(tmp_path: Path):
    write(tmp_path, {})
    config = load_repo_config(REPO, env(tmp_path))
    assert config.quick_checks == () and config.regression_tests == ()


@pytest.mark.parametrize("key", ["quickChecks", "regressionTests"])
@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("uv run ruff", "が文字列の配列でない"),
        ([1], "が文字列の配列でない"),
        ([""], "に使えない値がある"),
        (["  "], "に使えない値がある"),
    ],
)
def test_検証コマンドの欄が文字列の配列でないか空文字列を含むと拒む(
    tmp_path: Path, key: str, value: object, reason: str
):
    write(tmp_path, {key: value})
    with pytest.raises(RepoConfigError) as error:
        load_repo_config(REPO, env(tmp_path))
    assert f"{key} {reason}" in str(error.value)


def test_古い鍵verifyは直し方つきで拒む(tmp_path: Path):
    path = write(tmp_path, {"verify": ["uv run pytest"]})
    with pytest.raises(RepoConfigError) as error:
        load_repo_config(REPO, env(tmp_path))
    message = str(error.value)
    assert str(path) in message
    assert "`verify` は `regressionTests` に改名した。速いものは `quickChecks` に分ける" in message


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("{", "JSON として読めない"),
        ([], "JSON の object でない"),
        ({"protectedGlobs": []}, "知らない欄がある: protectedGlobs"),
        ({"testGlobs": [1]}, "testGlobs が文字列の配列でない"),
        ({"untested": [" "]}, "untested に使えない値がある"),
    ],
)
def test_崩れた設定は既定に戻さずパスと直し方を添えて拒む(
    tmp_path: Path, body: object, reason: str
):
    path = write(tmp_path, body)
    with pytest.raises(RepoConfigError) as error:
        load_repo_config(REPO, env(tmp_path))
    message = str(error.value)
    assert str(path) in message
    assert reason in message
    assert "quickChecks, regressionTests, testGlobs, protected, untested" in message
