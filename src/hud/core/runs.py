"""`autodev status --json` の結果を読む。形は autodev の `infra/status/status_sections.py` にある。

読むのは `format` が `FORMAT` の status だけで、欄の形は確かめない。形が崩れていることがあるのは、
実行器が書いたファイルの中身そのままの `progress` だけである。

driver が生きているかは `run.driver_running` だけで決める。`updated_at`（イベントを確定した時刻）も
`progress.updated` も、走っていても古くなるので生存の目安にしない。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

#: `run.phase` の表示名
PHASE_LABEL = {
    "not-started": "始まっていない",
    "planning": "計画中",
    "running": "実行中",
    "panicked": "パニック",
    "finishing": "仕上げ中",
    "finished": "完了",
}
#: statusline に出すのは、終えていないランのうち、最後のイベントからこの秒数の内のものと回答待ちのもの。
#: 捨てたランが statusline に残り続けないための窓で、driver が生きているかの判定ではない
RECENT = 3 * 3600
#: 指摘の評価（`findings[].rating`）。重い順
RATINGS = ("must-fix", "should-fix", "nit")
#: 開いていない指摘の状態（`findings[].status`）。件数だけを出す
SETTLED = ("closed", "rejected", "carried")
#: 読める status の形の版（`format`）
FORMAT = 2
#: `status --json --name` の終了コードのうち、そのランが無いことを表すもの
RUN_NOT_FOUND = 5


@dataclass(frozen=True)
class Listing:
    """全ランの status。`error` は、一覧そのものが取れなかった理由。"""

    runs: list[dict]
    error: str | None = None


@dataclass(frozen=True)
class Running:
    """`status` が running の実行 1 つ。"""

    task: str
    stage: str
    round: int
    #: `started_at` からの秒数。まだ始まっていなければ None
    seconds: float | None
    #: 進み具合（`progress`）のターン数と直前のツール。LLM のステージだけが書く
    turns: int
    tool: str


@dataclass(frozen=True)
class Single:
    """1 つのランの status。`error` は読めなかった理由、`gone` はそのランが無いこと。"""

    status: dict | None
    error: str | None = None
    gone: bool = False


def _failure(code: int | None, message: str, shape: str) -> str:
    if code == 0:
        return f"{shape}が返らなかった" + (f" · {message}" if message else "")
    return message or f"終了コード {code}"


def _other_format(value: Any) -> str:
    return f"形の版が違う（{FORMAT} を読む。届いたのは {value}）"


def listing(code: int | None, data: Any, message: str) -> Listing:
    """`status --json`（`--name` なし）の結果。終了コード 0 で配列が返れば一覧。

    読めないラン（`error` を持つ要素）は形の版を持たないので、版を確かめない。
    """
    if code != 0 or not isinstance(data, list):
        return Listing([], _failure(code, message, "JSON の配列"))
    for st in data:
        if not st.get("error") and st.get("format") != FORMAT:
            return Listing([], _other_format(st.get("format")))
    return Listing(data)


def single(code: int | None, data: Any, message: str) -> Single:
    """`status --json --name` の結果。終了コード `RUN_NOT_FOUND` のときだけ、ランが無いとみなす。

    終了コード 1 は、CLI の捕まえていない例外でも返るので、ランが無いとは決めない。
    """
    if code == RUN_NOT_FOUND:
        return Single(None, message or "そのランが無い", gone=True)
    if code != 0 or not isinstance(data, dict):
        return Single(None, _failure(code, message, "JSON のオブジェクト"))
    if data.get("format") != FORMAT:
        return Single(None, _other_format(data.get("format")))
    return Single(data)


def moment(stamp: Any) -> dt.datetime | None:
    """ISO 8601 の時刻。読めなければ None。

    status の時刻は `Z` で終わる。Python 3.10 の `fromisoformat` は `Z` を読めないので置き換える。
    """
    if not isinstance(stamp, str):
        return None
    try:
        at = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.astimezone()


def age(stamp: Any, now: dt.datetime) -> float | None:
    """ISO 8601 の時刻から `now` までの秒数。読めなければ None。"""
    at = moment(stamp)
    return None if at is None else (now - at).total_seconds()


def name_of(st: dict) -> str:
    return str(st.get("name") or "?")


def error_of(st: dict) -> str | None:
    """読めないランなら、その理由。読めないランには `name` と `error` しか無い。"""
    error = st.get("error")
    return str(error) if error else None


def run_of(st: dict) -> dict:
    return st["run"]


def phase(st: dict) -> str:
    return str(run_of(st).get("phase") or "")


def phase_label(st: dict) -> str:
    value = phase(st)
    return PHASE_LABEL.get(value, value or "?")


def awaiting(st: dict) -> bool:
    return bool(run_of(st).get("awaiting_answer"))


def driver_stopped(st: dict) -> bool:
    """driver が走っているはずのフェーズなのに走っておらず、回答待ちでもない。呼び直すまで進まない。"""
    return bool(run_of(st)["driver_stopped"])


def live_children(st: dict) -> tuple[int, ...]:
    """前の driver が残した、まだ生きている子の pid。"""
    return tuple(run_of(st)["live_children"])


def panic_cause(st: dict) -> str:
    """1 行にした原因。原因には claude の標準エラーがそのまま入ることがあり、改行で行が崩れる。"""
    return " ".join(str(run_of(st).get("panic_cause") or "").split())


def tasks(st: dict) -> list[dict]:
    return st["tasks"]


def questions(st: dict) -> list[dict]:
    return st["questions"]


def task_executions(task: dict) -> list[dict]:
    """タスクの実行すべて（始めた順。始めていない実行は後ろ）。

    今のフローの段の下・前のフローの版・段の外に分かれて届くので、`started_at` で並べ直す。
    段の順につなぐだけだと、直しのラウンドを回したタスクで実装 r0 / 実装 r1 / レビュー r0 と並ぶ。
    """
    flow = flow_of(task)
    placed = [e for step in flow["steps"] for e in step["executions"]] if flow else []
    every = [*placed, *task["earlier_executions"], *task["unplaced_executions"]]
    # started_at は同じ書式（UTC の `...Z`）なので、文字列のまま比べられる
    return sorted(every, key=lambda e: (e["started_at"] is None, e["started_at"] or ""))


def escalations(owner: dict) -> list[dict]:
    """ラン（`escalations[]`）かタスク（`tasks[].escalations[]`）の、開いているエスカレーション。"""
    return owner["escalations"]


def open_findings(findings: list[dict]) -> list[dict]:
    """開いている指摘を評価の重い順に（同じ評価の中は台帳に立てた順）。"""
    opened = [f for f in findings if f["status"] == "open"]
    return sorted(opened, key=lambda f: RATINGS.index(f["rating"]))


def settled_counts(findings: list[dict]) -> list[tuple[str, int]]:
    """開いていない指摘の状態ごとの件数。0 件の状態は除く。"""
    counts = [(state, sum(f["status"] == state for f in findings)) for state in SETTLED]
    return [(state, n) for state, n in counts if n]


def flow_of(task: dict) -> dict | None:
    return task["flow"]


def overview_pr(st: dict) -> int | None:
    overview = st["stack"]["overview"]
    return overview["pr"] if overview else None


def task_label(task: dict) -> str:
    """タスクの件名。計画タスクと git 管理タスクには件名が無いので、種類の名前で呼ぶ。"""
    title = task.get("title")
    if title:
        return str(title)
    return {"planning": "計画", "git": "git 管理"}.get(str(task.get("kind")), "")


def running(st: dict, now: dt.datetime) -> list[Running]:
    """走っている実行（タスクの順、タスクの中は始めた順）。前のフローの版の実行と、段の外の実行も含む。"""
    out: list[Running] = []
    for task in tasks(st):
        for execution in task_executions(task):
            if execution["status"] != "running":
                continue
            turns, tool = _progress(execution.get("progress"))
            out.append(
                Running(
                    task=str(task["id"]),
                    stage=str(execution["stage"]),
                    round=execution["round"],
                    seconds=age(execution["started_at"], now),
                    turns=turns,
                    tool=tool,
                )
            )
    return out


def _progress(progress: Any) -> tuple[int, str]:
    """`progress` のターン数と直前のツール。崩れていれば 0 と空。"""
    if not isinstance(progress, dict):
        return 0, ""
    turns = progress.get("turns")
    tool = progress.get("lastTool")
    return (
        turns if isinstance(turns, int) and not isinstance(turns, bool) else 0,
        tool if isinstance(tool, str) else "",
    )


def shown(st: dict, now: dt.datetime) -> bool:
    """statusline に出すラン。

    読めないランは出さない（古いディレクトリ 1 つで毎回赤字が出る。autodev-watch のリストでだけ見せる）。
    回答待ちとパニックは、人が動くまで進まないので、時間の窓に関わらず出し続ける。
    """
    if error_of(st) or phase(st) == "finished":
        return False
    if awaiting(st) or phase(st) == "panicked":
        return True
    updated = age(st.get("updated_at"), now)
    return updated is not None and updated <= RECENT


def order(statuses: list[dict], now: dt.datetime) -> list[dict]:
    """終えていないランを先に、あとは最後のイベントの新しい順。読めないランは最後。"""

    def key(st: dict) -> tuple[int, float]:
        if error_of(st):
            return (2, 0.0)
        updated = age(st.get("updated_at"), now)
        return (int(phase(st) == "finished"), updated if updated is not None else float("inf"))

    return sorted(statuses, key=key)


def short(seconds: float) -> str:
    """経過時間の表記。1 分未満は秒、1 時間未満は分と秒、それ以上は時と分。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{total % 3600 // 60:02d}m"
