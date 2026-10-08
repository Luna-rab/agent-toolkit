"""タスク 1 本のフローの段の並び（`tasks[].flow.steps`）。済んだ段・今の段・これからの段を並べる。

段の進み具合は status の `state` をそのまま使う。実行（`executions[]`）から組み直さない。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hud.core.runs import flow_of

#: ステージの種類（`StageKind` の値）から、画面に出す名前へ
FULL = {
    "Prepare": "準備",
    "Plan": "計画",
    "Replan": "再計画",
    "DesignLoop": "設計ループ",
    "DesignReview": "設計レビュー",
    "DesignJudge": "設計ジャッジ",
    "Revise": "設計の修正",
    "TestGen": "テスト作成",
    "ConfirmRed": "赤の確認",
    "Impl": "実装",
    "ReviewLoop": "レビュー",
    "Expect": "期待値を決める",
    "Review": "通常レビュー",
    "AdversarialReview": "敵対的レビュー",
    "Judge": "ジャッジ",
    "Fix": "修正",
    "Gate": "完了チェック",
    "WritePrBody": "PR 本文",
    "ResolveConflict": "衝突の解消",
    "CutBranch": "ブランチを切る",
    "Rebase": "リベース",
    "CheckUnion": "両側の確認",
    "IntegrationCheck": "統合検査",
    "Verify": "検証",
    "Push": "push",
    "CreatePR": "PR 作成",
    "StackLink": "スタックにつなぐ",
    "RefreshOverview": "概要の更新",
    "WriteOverview": "概要 PR 本文",
    "CreateOverviewPR": "概要 PR 作成",
    "ReadyOverview": "概要 PR を ready に",
    "ClosePRs": "PR を閉じる",
    "Unstack": "スタックから外す",
    "Relink": "つなぎ直す",
}
#: 見出しでは、同じラウンドに並んで走るレビュー 2 つを 1 つにまとめる
SHORT = {**FULL, "Review": "レビュー", "AdversarialReview": "レビュー"}
#: 済んだ段（飛ばした段を含む）がこれより多いと、古いものを 1 つの「…」にまとめる
MAX_DONE = 3
#: まとめたときに残す、済んだ段の数
KEEP_DONE = 2


class Mark(Enum):
    DONE = "done"
    SKIPPED = "skipped"
    #: 終えたが成功ではない実行（autodev-watch の実行の行だけで使う。フローの段には無い）
    FAILED = "failed"
    CURRENT = "current"
    NEXT = "next"
    #: 古い済んだ段をまとめたもの
    ELIDED = "elided"


#: `flow.steps[].state` から印へ
STATE_MARK = {
    "done": Mark.DONE,
    "skipped": Mark.SKIPPED,
    "current": Mark.CURRENT,
    "pending": Mark.NEXT,
}


@dataclass(frozen=True)
class Step:
    name: str
    #: 合成ステージの今のラウンド。それ以外は None
    round: int | None
    mark: Mark

    @property
    def label(self) -> str:
        """ラウンドは 2 ラウンド目から添える。"""
        if self.round is not None and self.round >= 2:
            return f"{self.name} r{self.round}"
        return self.name


def full_name(stage: str) -> str:
    return FULL.get(stage, stage)


def short_name(stage: str) -> str:
    return SHORT.get(stage, stage)


def step_of(entry: dict) -> Step:
    """段 1 つ。今の段が合成ステージなら、中で走っているステージの名前を出す。"""
    mark = STATE_MARK.get(str(entry.get("state")), Mark.NEXT)
    inner = entry.get("inner")
    if mark is Mark.CURRENT and inner:
        round_value = entry.get("round")
        return Step(
            full_name(str(inner)), round_value if isinstance(round_value, int) else None, mark
        )
    return Step(full_name(str(entry.get("stage") or "?")), None, mark)


def steps(task: dict) -> list[Step]:
    """今のフローの段の並び。フローが無ければ空。"""
    flow = flow_of(task)
    if flow is None:
        return []
    all_steps = [step_of(e) for e in flow["steps"]]
    # まとめるのは先頭から続く済んだ段だけにして、段の順を入れ替えない
    head = 0
    while head < len(all_steps) and all_steps[head].mark in (Mark.DONE, Mark.SKIPPED):
        head += 1
    done, rest = all_steps[:head], all_steps[head:]
    if len(done) > MAX_DONE:
        done = [Step("…", None, Mark.ELIDED), *done[-KEEP_DONE:]]
    return done + rest
