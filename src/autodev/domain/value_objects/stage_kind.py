from __future__ import annotations

from enum import Enum


class StageKind(Enum):
    """ステージの種類。値はフローの JSON に書く名前。"""

    # 計画タスク
    PREPARE = "Prepare"
    PLAN = "Plan"
    REPLAN = "Replan"
    DESIGN_LOOP = "DesignLoop"
    DESIGN_REVIEW = "DesignReview"
    DESIGN_JUDGE = "DesignJudge"
    REVISE = "Revise"
    # 実装タスク
    TEST_GEN = "TestGen"
    CONFIRM_RED = "ConfirmRed"
    IMPL = "Impl"
    REVIEW_LOOP = "ReviewLoop"
    EXPECT = "Expect"
    REVIEW = "Review"
    ADVERSARIAL_REVIEW = "AdversarialReview"
    JUDGE = "Judge"
    FIX = "Fix"
    TEST_FIX = "TestFix"
    GATE = "Gate"
    WRITE_PR_BODY = "WritePrBody"
    # 実装タスク（統合をやり直す差し込んだタスク）と git 管理タスク
    RESOLVE_CONFLICT = "ResolveConflict"
    # git 管理タスク
    CUT_BRANCH = "CutBranch"
    REBASE = "Rebase"
    CHECK_UNION = "CheckUnion"
    INTEGRATION_CHECK = "IntegrationCheck"
    PUSH = "Push"
    CREATE_PR = "CreatePR"
    STACK_LINK = "StackLink"
    REFRESH_OVERVIEW = "RefreshOverview"
    WRITE_OVERVIEW = "WriteOverview"
    CREATE_OVERVIEW_PR = "CreateOverviewPR"
    READY_OVERVIEW = "ReadyOverview"
    CLOSE_PRS = "ClosePRs"
    UNSTACK = "Unstack"
    RELINK = "Relink"

    @classmethod
    def _missing_(cls, value: object) -> StageKind | None:
        # 改名前の記録（events.db・フローの JSON）を読む
        if value == "Verify":
            return cls.INTEGRATION_CHECK
        return None
