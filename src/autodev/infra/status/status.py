"""`autodev status --json` の組み立てと、走っているステージの進み具合のファイル。

状態を外から読むのは `status --json` だけで、HUD と `/autodev` はこれを呼ぶ。
`events` を読むだけの接続で読み、自分で再生する。driver のメインループには関わらない。

外向けの形は、どの集約にも依らない骨組み（形の版・ラン名・最後の seq と時刻・拒んだコマンド）に、
集約ごとの中身を `sections`（既定は `status_sections.SECTIONS`）で差し込む。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from ...adapters.process import children
from ...domain.streams import aggregate_for
from ...domain.value_objects.execution_id import ExecutionId
from ...domain.value_objects.run_name import RunName
from ..files import write_atomic
from ..lock import held_elsewhere
from ..paths import RunPaths, state_root
from ..store.eventstore import AggregateFactory, EventReader, decoded, replay
from ..store.rejections import read_rejections
from .status_sections import SECTIONS, DriverFacts, Replayed, Section

log = logging.getLogger(__name__)

#: 外向けの形の版。欄の意味を変えた・欄を消したら上げる（足すだけなら上げない）
FORMAT = 2

#: 骨組みの欄。差し込む欄の名前とぶつけない
_FRAME_KEYS = frozenset({"format", "name", "last_seq", "updated_at", "rejections"})


def build_status(
    paths: RunPaths,
    factory: AggregateFactory = aggregate_for,
    sections: Mapping[str, Section] = SECTIONS,
) -> dict[str, Any]:
    """ランが無ければ（`events.db` が無ければ）FileNotFoundError。"""
    if clash := sorted(_FRAME_KEYS & set(sections)):
        raise ValueError(f"骨組みの欄と同じ名前の欄は差し込めない: {clash}")
    with EventReader.open(paths.events_db) as reader:
        history = decoded(reader.read_all())
    view = Replayed(
        replay(history, factory), history, read_progress(paths), factory, driver_facts(paths)
    )
    last = history[-1][0] if history else None
    status: dict[str, Any] = {
        "format": FORMAT,
        "name": paths.name.value,
        "last_seq": last.seq if last else 0,
        "updated_at": last.at if last else None,
        "rejections": read_rejections(paths.rejections),
    }
    for key, section in sections.items():
        status[key] = section(view)
    return status


def driver_facts(paths: RunPaths) -> DriverFacts:
    """driver が走っていれば、生きている子はその driver の子なので、前の driver の子として数えない
    （`autodev run` も錠を取ってから子の控えを見る）。"""
    directory = str(paths.root.absolute())
    if held_elsewhere(paths.driver_lock):
        return DriverFacts(directory, running=True)
    live = tuple(child.pid for child in children.alive(paths.children))
    # 控えを見ている間に driver が起動していたら、見つけた子はその driver の子かもしれない
    if live and held_elsewhere(paths.driver_lock):
        return DriverFacts(directory, running=True)
    return DriverFacts(directory, running=False, live_children=live)


def run_status(paths: RunPaths) -> dict[str, Any]:
    """1 つのランの `status --json`。ランが無ければ FileNotFoundError。"""
    return build_status(paths)


def all_statuses(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """状態の置き場にある全ランの `status --json`（ラン名の順）。HUD が一覧に使う。

    events.db の無い所はランではないので載せない。読めないラン（ラン名の規則に合わない・events.db が
    壊れた・この版が読めないイベント・欄を作る所の不具合）は、消さずに `{"name", "error"}` で残す。
    消すと、欄を作る所の不具合で全ランが読めないときに、HUD が「ランが無い」と見せる。
    """
    root = state_root(env)
    if not root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for folder in sorted(root.iterdir(), key=lambda path: path.name):
        if not (folder / "events.db").is_file():
            continue
        try:
            found.append(run_status(RunPaths(RunName(folder.name), folder)))
        except Exception as error:
            # 1 本が読めないだけで、ほかのランの一覧まで落とさない
            log.warning("ラン %s を読めない", folder.name, exc_info=True)
            found.append({"name": folder.name, "error": f"{type(error).__name__}: {error}"})
    return found


# --- 進み具合 ---
#
# 正本ではない使い捨てのファイル。ステージ 1 回で数百の stream-json のイベントが流れるので、
# イベントにすると events が膨らむ。消えても状態は変わらない。


def write_progress(paths: RunPaths, execution: ExecutionId, progress: Mapping[str, Any]) -> None:
    """実行器のスレッドが数秒ごとに呼ぶ。一時ファイルから置き換えるので、読む側は書きかけを見ない。"""
    write_atomic(paths.progress_of(execution), json.dumps(progress, ensure_ascii=False))


def remove_progress(paths: RunPaths, execution: ExecutionId) -> None:
    paths.progress_of(execution).unlink(missing_ok=True)


def prune_progress(paths: RunPaths, keep: Iterable[ExecutionId]) -> None:
    """`keep` に無い実行の進み具合を消す。driver の起動時に、走っていない実行の分を片付ける。

    前の driver が落ちると、実行器が消す前のファイルが残り、`status --json` が終わった実行を
    走っているかのように出す。
    """
    if not paths.progress.is_dir():
        return
    names = {str(execution) for execution in keep}
    for path in paths.progress.glob("*.json"):
        if path.stem not in names:
            path.unlink(missing_ok=True)


def read_progress(paths: RunPaths) -> dict[str, Any]:
    """`ExecutionId` の文字列 → 中身。読めないファイルは飛ばす（使い捨てなので、無くても状態は変わらない）。"""
    found: dict[str, Any] = {}
    if not paths.progress.is_dir():
        return found
    for path in sorted(paths.progress.glob("*.json")):
        try:
            found[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 一覧を取った後に実行器が消した、など
            continue
    return found
