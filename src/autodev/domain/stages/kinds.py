"""ステージの宣言（`StageSpec`）に書く列挙と、それに付く小さな関数。"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum


class StageMode(Enum):
    """ステージの実体。種類で違うのは、実行器の ④（走らせる）だけである。"""

    #: `claude -p` を 1 回
    LLM = "llm"
    #: Python の関数
    PROGRAM = "program"
    #: 中のステージを並べたサブフロー（ReviewLoop・DesignLoop）
    COMPOSITE = "composite"


class SessionScope(Enum):
    """LLM のステージのセッションを、どこまで続けるか。"""

    #: セッションが無い（決定的なステージ・合成ステージ）
    NONE = "none"
    #: 毎回新しい（レビューは毎ラウンドまっさら）
    FRESH = "fresh"
    #: タスクの間続ける（Impl・Judge）。FlowStep の `fresh_session` で新しくできる
    TASK = "task"
    #: ランの間続ける（DesignJudge。前の版の形に戻ったかを見分けるのに経緯が要る）
    RUN = "run"
    #: `follows` に挙げたステージのセッションを続ける（Fix は Impl、Revise は Plan か Replan）
    FOLLOWS = "follows"


class StepArgument(Enum):
    """フローの段（FlowStep）に書ける引数。どのステージがどれを受けるかは StageSpec が持つ。"""

    REVIEWERS = "reviewers"


class InnerRole(Enum):
    """合成ステージの 1 ラウンドの中の役。並びは「頭 → 見る役 → 判定 → 直す役」。"""

    #: ラウンドの頭。`when` の成果物があるときだけ走る（Expect）
    HEAD = "head"
    #: 見る役。並列に走る（Review・AdversarialReview・DesignReview）
    LOOKER = "looker"
    #: 判定する役。判定の後は、ほかの集約の結果が届くまで止まる（Judge・DesignJudge）
    JUDGE = "judge"
    #: 直す役。直したら次のラウンドへ（Fix・Revise）
    FIXER = "fixer"


class EvidenceCheck(Enum):
    """決定的なステージが期待する証拠。外れたら、StageSpec の `on_mismatch` を報告する。"""

    #: 検証コマンドのどれかが落ちる（ConfirmRed: テストが実装の前に落ちる）
    VERIFY_FAILS = "verify-fails"
    #: 種類が TASK_TESTS の結果のどれかが落ち、かつ種類が QUICK_CHECKS の結果がすべて通る
    #: （ConfirmRed: テストは実装の前に落ち、軽い検査は通る）。タスクのテストが 0 件なら成り立たない
    RED_AND_CLEAN = "red-and-clean"
    #: 検証コマンドがすべて通る（git 管理タスクの統合検査: 積む直前の回帰）
    VERIFY_PASSES = "verify-passes"
    #: 衝突したファイルで両側の変更を残した（CheckUnion）。結果が無ければ形の誤り
    UNION_KEPT = "union-kept"
    #: 完了チェックが通る（Gate）。結果が無ければ形の誤り。外れたときの扱いは GateEvaluator が決める
    GATE_PASSES = "gate-passes"
    #: タスクのブランチに根元から上のコミットが 1 件以上ある（Rebase: 載せ直すものがある）。数え
    #: られなければ（根元が無い）形の誤り
    OWN_COMMITS = "own-commits"


def has_own_commits(commits: int | None) -> bool:
    """根元から上のコミットがあるか（`EvidenceCheck.OWN_COMMITS`）。

    Rebase の中身は、載せ直す前にこれを聞き、偽なら流さない。0 件で `git rebase --onto` を流すと、
    ブランチを一番上へ黙って動かし、タスクの仕事を消す。
    """
    return commits is not None and commits >= 1


class ResultField(Enum):
    """ステージが返す結果の JSON のうち、ドメインが読む欄。値は JSON のキー。

    LLM のステージの形（型・必須・列挙）の出所は `schemas/<ステージ>.json` で、ここは欄の名前だけを
    宣言する。検査（`test/autodev/test_contracts.py`）が、スキーマの欄とここを照らす。決定的な
    ステージの結果の JSON は実行器が組む（PR 番号・切った worktree）。読み替える規則は
    `domain/stages/results.py` にあり、読み替えた値（`StageResult`）が StageCompleted に載る。
    """

    #: 返した報告（Task が成果物より先に読む）。`reports` を持つステージだけが返す
    REPORT = "report"
    #: 報告の中身（StageReported.reason。RejectRequest の理由になる）
    REPORT_REASON = "reportReason"
    #: レビューの指摘（RecordFindings）
    FINDINGS = "findings"
    #: 指摘への直し方・判断のコメント（CommentFinding）
    COMMENTS = "comments"
    #: 指摘の状態の判定（RecordJudgement）
    VERDICTS = "verdicts"
    #: open に残した指摘が直らない原因（FindingsEvaluated.stall_cause → ConcludeReviewRound の cause）
    STALL_CAUSE = "stallCause"
    #: 停滞の分類を付けた理由（stallCause と組で書く。停滞のエスカレーションの理由になる）
    STALL_REASON = "stallReason"
    #: 設計が前の版に戻った・受入条件が曖昧（MarkReverted・MarkAmbiguous）
    DESIGN_CAUSE = "designCause"
    #: 提案（Proposal）の欄。設計の本文は driver が `design/v<版>.md` に書き出す
    DESIGN = "design"
    TASKS = "tasks"
    QUICK_CHECKS = "quickChecks"
    REGRESSION_TESTS = "regressionTests"
    STOP = "stop"
    DISCARD = "discard"
    CARRY = "carry"
    DECISIONS = "decisions"
    DEFERRALS = "deferrals"
    #: コードマップの本文。driver が `codemap.md` に書き出す
    CODEMAP = "codemap"
    #: 期待値を空けたテスト（Expect なら、まだ期待値を書いていないテスト）。空でなければ、実行器が
    #: `awaiting-expectations` の成果物にする
    AWAITING_EXPECTATIONS = "awaitingExpectations"
    #: Expect が期待値にしなかった、受入条件と食い違う出力。Expect を出どころとする指摘になる
    DEFECTS = "defects"
    #: 確かめた結果「変えない」と決めて、何も作らずに終えた（`can_keep` のステージだけ）
    UNCHANGED = "unchanged"
    #: PR の本文
    BODY = "body"
    #: 概要 PR のタイトル（印の `[autodev] ` を付ける前の 1 行）。概要 PR の本文を書くステージだけが返す
    TITLE = "title"
    #: 作った・つないだ PR の番号（決定的なステージ。RecordOverview・AppendEntry）
    PR = "pr"
    #: CutBranch が切った worktree（決定的なステージ。WorktreeReady）。使うタスク・在りか・ブランチ
    WORKTREE_TASK = "task"
    TREE = "tree"
    BRANCH = "branch"
    #: CutBranch がブランチ（stack-top なら detached の worktree）を切った元のコミット
    BASE = "base"
    #: Rebase がタスクのブランチを載せ直した先のコミット（取り出したときのスタックの一番上）
    ONTO = "onto"


class BodyTarget(Enum):
    """結果の `body`（PR の本文）を、どの PR の本文として書き出すか。"""

    #: タスク PR の本文。書き出した在りかが成果物 `pr-body` の実物になる
    TASK_PR = "task-pr"
    #: 概要 PR の本文。マーカーを入れたまま置き、RefreshOverview が毎回埋め直す（成果物にしない）
    OVERVIEW_PR = "overview-pr"


class Handoff(Enum):
    """ステージの結果を、どの集約へ、どのコマンドで渡すか。

    渡す先を宣言したステージは、完了しても cursor を進めず、渡した先の「受けた／受けられない」が
    届くまで待つ（ConfirmHandoff。判定は Conclude*Round）。受けられなければ、Task が
    `result-refused` で上げる。受け取る側は、中身の問題を拒否（Rejected）にせず、受けられない理由を
    イベント（ResultRefused）で出す。ポリシーは StageCompleted.handoff を見て、決まったコマンドを組む。
    """

    #: 提案 → Design（ProposeDesign）
    PROPOSAL = "proposal"
    #: 指摘 → 指摘の台帳（RecordFindings）
    FINDINGS = "findings"
    #: 判定 → 指摘の台帳（RecordJudgement）。受けたことは、判定を締めた後の Conclude*Round で届く
    #: （設計の判定は、台帳の後に Design が確定・Revise・回答待ちを決める）
    JUDGEMENT = "judgement"
    #: 作った概要 PR → Stack（RecordOverview）
    OVERVIEW = "overview"
    #: 積んだ 1 本 → Stack（AppendEntry）
    ENTRY = "entry"
    #: 閉じた所 → Stack（UnstackFrom）
    CUT_BACK = "cut-back"

    @property
    def receiver(self) -> str:
        """受け取る集約の名前（`Aggregate.NAME`）。"""
        return _RECEIVERS[self]


_RECEIVERS: Mapping[Handoff, str] = {
    Handoff.PROPOSAL: "Design",
    Handoff.FINDINGS: "ReviewLedger",
    Handoff.JUDGEMENT: "ReviewLedger",
    Handoff.OVERVIEW: "Stack",
    Handoff.ENTRY: "Stack",
    Handoff.CUT_BACK: "Stack",
}
