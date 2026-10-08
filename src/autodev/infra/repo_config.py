"""リポジトリごとの設定。人が書き、driver は読むだけ。ランをまたいで使い回す。

置き場は `$XDG_CONFIG_HOME/autodev/repos/<スラッグ>.json`（既定 `~/.config`）。スラッグは対象
リポジトリの絶対パスの両端の `/` を落とし、`/` を `__`、`:` を `_` にしたもの。

```json
{
  "quickChecks": ["uv run ruff check ."],
  "regressionTests": ["uv run pytest -q"],
  "testGlobs": ["**/test_*.py", "**/tests/**"],
  "protected": [".github/workflows/**", "uv.lock"],
  "untested": ["docs/**", "**/*.md"]
}
```

- どの欄も省ける。省いた欄は既定（`testGlobs` は `DEFAULT_TEST_GLOBS`、ほかは空）。書いた
  `testGlobs` は既定に足さず、置き換える
- ファイルが無いのは崩れではない（検証コマンドが空でも走れる）。読めない・形が違う・
  知らない欄があるときは、黙って既定に戻さずに `RepoConfigError` にする。欄の名前を打ち間違えた
  設定が既定のまま走ると、守ったつもりの変更禁止パスが効かない
- ランの作業に合わせて決めた値はここに書かない（計画が決めたものはランに閉じる）
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.value_objects.base import InvalidValue
from ..domain.value_objects.glob_pattern import DEFAULT_TEST_GLOBS, GlobPattern
from ..domain.value_objects.repository import Repository
from ..domain.value_objects.verify_command import VerifyCommand

#: JSON の欄 → `RepoConfig` の欄
FIELDS = {
    "quickChecks": "quick_checks",
    "regressionTests": "regression_tests",
    "testGlobs": "test_globs",
    "protected": "protected_globs",
    "untested": "untested_globs",
}


class RepoConfigError(ValueError):
    """設定のファイルが崩れている。文面に、ファイルのパスと直し方を入れる。"""


@dataclass(frozen=True)
class RepoConfig:
    #: 軽い検査（lint・型検査など速いもの。ブリーフに載せる。ランの値は計画が決める）
    quick_checks: tuple[VerifyCommand, ...] = ()
    #: 回帰テスト（テスト全体など遅いもの。ブリーフに載せる）
    regression_tests: tuple[VerifyCommand, ...] = ()
    test_globs: tuple[GlobPattern, ...] = DEFAULT_TEST_GLOBS
    #: 変更禁止のパス
    protected_globs: tuple[GlobPattern, ...] = ()
    #: テストが要らないパス
    untested_globs: tuple[GlobPattern, ...] = ()
    #: 読んだファイル。無くて既定にしたなら None
    source: Path | None = None

    def executor_arguments(self) -> dict[str, Any]:
        """`from_parts` に渡す欄（`RunSetting` の欄の名前）。"""
        return {
            "quick_checks": self.quick_checks,
            "regression_tests": self.regression_tests,
            "test_globs": self.test_globs,
            "protected_globs": self.protected_globs,
            "untested_globs": self.untested_globs,
        }


def config_root(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    # XDG の仕様では、相対パスの XDG_CONFIG_HOME は無効として既定に戻す
    xdg = env.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if os.path.isabs(xdg) else Path.home() / ".config"
    return base / "autodev" / "repos"


def slug(repository: Repository) -> str:
    return repository.value.strip("/").replace("/", "__").replace(":", "_")


def config_path(repository: Repository, env: Mapping[str, str] | None = None) -> Path:
    return config_root(env) / f"{slug(repository)}.json"


def load_repo_config(repository: Repository, env: Mapping[str, str] | None = None) -> RepoConfig:
    path = config_path(repository, env)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return RepoConfig()
    except OSError as error:
        raise RepoConfigError(_broken(path, f"読めない（{error}）")) from error
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as error:
        raise RepoConfigError(_broken(path, f"JSON として読めない（{error}）")) from error
    if not isinstance(loaded, dict):
        raise RepoConfigError(_broken(path, "JSON の object でない"))
    if "verify" in loaded:
        raise RepoConfigError(
            f"リポジトリの設定 {path} に古い鍵 `verify` がある。"
            "`verify` は `regressionTests` に改名した。速いものは `quickChecks` に分ける"
        )
    if unknown := sorted(set(loaded) - set(FIELDS)):
        raise RepoConfigError(_broken(path, f"知らない欄がある: {', '.join(unknown)}"))
    fields: dict[str, Any] = {}
    for key, name in FIELDS.items():
        if key not in loaded:
            continue
        kind = VerifyCommand if key in ("quickChecks", "regressionTests") else GlobPattern
        fields[name] = _strings(path, key, loaded[key], kind)
    return RepoConfig(**fields, source=path)


def _strings(path: Path, key: str, value: Any, kind: type[Any]) -> tuple[Any, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RepoConfigError(_broken(path, f"{key} が文字列の配列でない"))
    try:
        return tuple(kind(item) for item in value)
    except InvalidValue as error:
        raise RepoConfigError(_broken(path, f"{key} に使えない値がある（{error}）")) from error


def _broken(path: Path, reason: str) -> str:
    return (
        f"リポジトリの設定 {path} が {reason}。"
        f"欄は {', '.join(FIELDS)}（どれも省ける・文字列の配列）だけにして直すか、"
        "ファイルを消して既定で走らせる"
    )
