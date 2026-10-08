"""クラスごとのモデルと effort の設定。利用者全体で 1 つ。

置き場は `$XDG_CONFIG_HOME/autodev/models.json`（既定 `~/.config`）。

```json
{"lead": {"model": "opus", "effort": "high"}, "implement": {"effort": "xhigh"}}
```

- どのクラス・欄も省ける。書いたクラスの書いた欄だけが `ModelClasses.default()` を上書きする
- 読めない・形が違う・知らないクラスや欄がある・値が不正なときは、黙って既定に戻さずに
  `ModelConfigError` にする（`repo_config.RepoConfigError` と同じ方針）
- 新しいランを始めるときに 1 回だけ読み、ランに記録する。走っているランには効かない
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..domain.value_objects.base import InvalidValue
from ..domain.value_objects.model_class import (
    AgentKind,
    Effort,
    ModelClass,
    ModelClasses,
    ModelName,
)

FIELDS = ("model", "effort", "agent")


class ModelConfigError(ValueError):
    """設定のファイルが崩れている。文面に、ファイルのパスと直し方を入れる。"""


class _Combination(ModelConfigError):
    """欄ごとには正しいが、model・effort・agent の組み合わせが使えない。"""

    def __init__(self, reason: str, path: Path, cls: ModelClass) -> None:
        super().__init__(_broken(path, f"{cls.value} の組み合わせが使えない: {reason}"))
        self.reason = reason


def models_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    # XDG の仕様では、相対パスの XDG_CONFIG_HOME は無効として既定に戻す
    xdg = env.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if os.path.isabs(xdg) else Path.home() / ".config"
    return base / "autodev" / "models.json"


def load_model_classes(env: Mapping[str, str] | None = None) -> ModelClasses:
    path = models_path(env)
    return _apply(path, _read(path) or {})


def set_model_class(
    cls: ModelClass,
    *,
    model: ModelName | None = None,
    effort: Effort | None = None,
    agent: AgentKind | None = None,
    env: Mapping[str, str] | None = None,
) -> ModelClasses:
    path = models_path(env)
    written = _read(path) or {}
    # 今のファイルが崩れていたら、書く前に止める
    _apply(path, written)
    entry = dict(written.get(cls.value, {}))
    current = entry.get("agent", AgentKind.CLAUDE.value)
    if (
        agent is not None
        and model is None
        and agent.value != current
        and (agent is AgentKind.COPILOT or current != AgentKind.CLAUDE.value)
    ):
        # codex・copilot のモデル名（gpt-5.5 など）が別のエージェントにそのまま渡るのを防ぐ。
        # copilot へは、claude のモデル名（opus など）も引き継げない
        raise ModelConfigError(
            f"{cls.value} の agent を {current} から {agent.value} に替えるには、"
            f"{agent.value} のモデル名を --model で一緒に渡す"
        )
    if model is not None:
        entry["model"] = model.value
    if effort is not None:
        entry["effort"] = effort.value
    if agent is not None:
        entry["agent"] = agent.value
    written[cls.value] = entry
    try:
        merged = _apply(path, written)
    except _Combination as error:
        # ファイルは壊れていない。頼んだ値の組み合わせだけが通らない
        raise ModelConfigError(
            f"{cls.value} の組み合わせが使えない: {error.reason}。"
            "agent を変えるときは、そのエージェントで使える --model と --effort も一緒に渡す"
        ) from error
    _write(path, json.dumps(written, ensure_ascii=False, indent=2) + "\n")
    return merged


def _write(path: Path, text: str) -> None:
    """一時ファイルに書いてから差し替える。途中で落ちても、元の中身か新しい中身のどちらかが残る。"""
    # シンボリックリンク（dotfiles に置いた models.json など）は、リンクを残してリンク先を差し替える
    target = path.resolve()
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ModelConfigError(
            f"モデルの設定 {path} に書けない（{error}）。"
            f"{target.parent} に書き込めるようにしてから、もう一度流す"
        ) from error


def _read(path: Path) -> dict[str, Any] | None:
    """書いてある object。ファイルが無ければ None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ModelConfigError(_broken(path, f"読めない（{error}）")) from error
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as error:
        raise ModelConfigError(_broken(path, f"JSON として読めない（{error}）")) from error
    if not isinstance(loaded, dict):
        raise ModelConfigError(_broken(path, "JSON の object でない"))
    return loaded


def _apply(path: Path, written: Mapping[str, Any]) -> ModelClasses:
    names = {cls.value for cls in ModelClass}
    if unknown := sorted(set(written) - names):
        raise ModelConfigError(_broken(path, f"知らないクラスがある: {', '.join(unknown)}"))
    merged = ModelClasses.default()
    for cls in ModelClass:
        if cls.value not in written:
            continue
        entry = written[cls.value]
        if not isinstance(entry, dict):
            raise ModelConfigError(_broken(path, f"{cls.value} が JSON の object でない"))
        if unknown := sorted(set(entry) - set(FIELDS)):
            raise ModelConfigError(
                _broken(path, f"{cls.value} に知らない欄がある: {', '.join(unknown)}")
            )
        agent = _agent(path, cls, entry)
        if agent in (AgentKind.CODEX, AgentKind.COPILOT) and "model" not in entry:
            raise _Combination(
                f"agent {agent.value} には model が要る（{agent.value} のモデル名を書く）",
                path,
                cls,
            )
        try:
            merged = merged.with_choice(
                cls, model=_model(path, cls, entry), effort=_effort(path, cls, entry), agent=agent
            )
        except InvalidValue as error:
            raise _Combination(str(error), path, cls) from error
    return merged


def _agent(path: Path, cls: ModelClass, entry: Mapping[str, Any]) -> AgentKind | None:
    if "agent" not in entry:
        return None
    try:
        return AgentKind(entry["agent"])
    except ValueError as error:
        raise ModelConfigError(
            _broken(path, f"{cls.value}.agent が {entry['agent']!r} で、使える値でない")
        ) from error


def _model(path: Path, cls: ModelClass, entry: Mapping[str, Any]) -> ModelName | None:
    if "model" not in entry:
        return None
    value = entry["model"]
    if not isinstance(value, str):
        raise ModelConfigError(_broken(path, f"{cls.value}.model が文字列でない"))
    try:
        return ModelName(value)
    except InvalidValue as error:
        raise ModelConfigError(_broken(path, f"{cls.value}.model が使えない（{error}）")) from error


def _effort(path: Path, cls: ModelClass, entry: Mapping[str, Any]) -> Effort | None:
    if "effort" not in entry:
        return None
    try:
        return Effort(entry["effort"])
    except ValueError as error:
        raise ModelConfigError(
            _broken(path, f"{cls.value}.effort が {entry['effort']!r} で、使える値でない")
        ) from error


def _broken(path: Path, reason: str) -> str:
    return (
        f"モデルの設定 {path} が {reason}。"
        f"クラスは {', '.join(cls.value for cls in ModelClass)}、欄は model（空でない文字列。"
        f"agent が codex・copilot のときは必須。copilot は auto 不可）・effort（{', '.join(effort.value for effort in Effort)}。"
        f"agent が受けるものだけ）・agent（{', '.join(agent.value for agent in AgentKind)}）だけにして直すか、"
        "ファイルを消して既定で走らせる"
    )
