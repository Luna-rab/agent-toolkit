from __future__ import annotations

from dataclasses import dataclass

from .commit_sha import CommitSha
from .design_judgement import DesignJudgement
from .escalation_kind import EscalationKind
from .finding_comment import FindingComment
from .finding_verdict import FindingVerdict
from .pr_number import PrNumber
from .proposal import Proposal
from .reported_finding import ReportedFinding
from .stall_cause import StallCause
from .worktree_cut import WorktreeCut


@dataclass(frozen=True)
class StageResult:
    """ステージの結果の JSON を、ドメインの値に読み替えたもの（`domain/stages/results.py` が組む）。

    読むのは StageSpec.result が宣言した欄だけで、宣言していない欄は空のまま残る。ポリシーは、
    これと StageCompleted のほかの欄だけで、受け取る側へのコマンド（RecordFindings・
    RecordJudgement・ProposeDesign・AppendEntry など）を組む。
    """

    report: EscalationKind | None = None
    report_reason: str | None = None
    findings: tuple[ReportedFinding, ...] = ()
    comments: tuple[FindingComment, ...] = ()
    verdicts: tuple[FindingVerdict, ...] = ()
    stall_cause: StallCause | None = None
    #: 停滞の分類（stall_cause）を付けた理由。停滞のエスカレーションの理由になる
    stall_reason: str | None = None
    design_cause: DesignJudgement | None = None
    proposal: Proposal | None = None
    #: 確かめた結果「変えない」と決めて、何も作らずに終えた（StageSpec.can_keep のステージだけ）
    unchanged: bool = False
    pr: PrNumber | None = None
    worktree: WorktreeCut | None = None
    #: Rebase がタスクのブランチを載せ直した先のコミット（BranchRebased）
    onto: CommitSha | None = None
