"""autodev-watch のステージのリスト。今のフローの段を 1 行ずつ並べ、それぞれに属する実行を持たせる。

段の実行は status の `flow.steps[].executions` のまま持たせ、`flow_version` と `step` で振り分け直さない。
前のフローの版の実行（`earlier_executions`）と、段に当たらない今の版の実行（`unplaced_executions`）は、
段の後ろに 1 つずつ並べる。
"""

from __future__ import annotations

from dataclasses import dataclass

from hud.core.pipeline import Mark, full_name, step_of
from hud.core.runs import flow_of

#: 実行の `status` から印へ。表に無いもの（completed・reported）は済んだもの。中断・捨てた・
#: やり直したものは失敗ではないので、飛ばした段と同じ印にする
EXECUTION_MARK = {
    "running": Mark.CURRENT,
    "requested": Mark.NEXT,
    "deferred": Mark.NEXT,
    "failed": Mark.FAILED,
    "refused": Mark.FAILED,
    "interrupted": Mark.SKIPPED,
    "abandoned": Mark.SKIPPED,
    "restarted": Mark.SKIPPED,
}


@dataclass(frozen=True)
class StageItem:
    #: リストの中で項目を見分ける値。読み直してもカーソルを同じ項目に保つのに使う
    key: str
    #: 画面に出す名前（`実装`・`ジャッジ r2`）
    label: str
    mark: Mark
    #: この段の実行（始めた順）。段に入らない実行の項目なら、その実行 1 つ
    runs: tuple[dict, ...]
    #: 段に入らない実行の項目なら、その訳（`OLDER`・`OUTSIDE`）。段の項目は空
    note: str = ""


#: 書き直す前のフローの実行
OLDER = "前の版"
#: 今のフローの実行なのに、`step` が段の範囲の外か null。捨てると、走っている実行が画面から消える
OUTSIDE = "段の外"


def for_task(task: dict) -> list[StageItem]:
    flow = flow_of(task)
    out: list[StageItem] = []
    for index, entry in enumerate(flow["steps"] if flow else []):
        step = step_of(entry)
        out.append(StageItem(f"step:{index}", step.label, step.mark, tuple(entry["executions"])))
    # 前の版の実行を先に、どの段にも入らない実行を最後に並べる
    loose = [(OLDER, e) for e in task["earlier_executions"]]
    loose += [(OUTSIDE, e) for e in task["unplaced_executions"]]
    for note, e in loose:
        label = f"{full_name(str(e['stage']))} r{e['round']}"
        mark = EXECUTION_MARK.get(str(e["status"]), Mark.DONE)
        out.append(StageItem(f"exec:{e['id']}", label, mark, (e,), note))
    return out
