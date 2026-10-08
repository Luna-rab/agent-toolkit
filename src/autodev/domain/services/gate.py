"""GateEvaluator: 完了チェック。

集めた証拠（`GateEvidence`）から、項目ごとの合否と、落ちた項目をどう扱うかを決める。

- コードを直せば解ける項目（指摘が残っている・verify が落ちる）は、項目ごとに決まった id の指摘
  （`G-<項目>`）を開く。修正の回数が同じ指摘に積み上がるので、停滞の判定も効く
- コードを直しても解けない項目は、エスカレーションで上げる（`GateItem.escalation`）。どれか 1 つでも
  あれば上げる方を取り、G- の指摘は開かない。直せない落ちが残ったまま Fix を回しても通らない
- G- の指摘を判定するのは Gate 自身なので（ReviewLedger の JudgeCapability）、項目 2「open の指摘が
  無い」には数えない。数えると、前の Gate が開いた G- の指摘で次の Gate も必ず落ち、閉じる者が
  いなくなる

証拠を集めるのは実行器で、ここは判断だけをする。glob との照合（テストのパス・テストが要らないパス）は
アダプタが持つ規則なので、変わったファイルごとに照合の答えを添えて受け取り
（`ChangedFile`）、TestGen の有無でどちらの一覧のどの答えを見るかはここで決める。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.finding_id import FindingId
from ..value_objects.finding_summary import FindingSummary
from ..value_objects.gate_item import GateItem
from ..value_objects.gate_item_result import GateItemResult
from ..value_objects.gate_report import GateReport
from ..value_objects.rating import Rating
from ..value_objects.stage_kind import StageKind
from ..value_objects.verify_result import VerifyResult


@dataclass(frozen=True)
class ChangedFile:
    """変わったファイル 1 つと、glob との照合の答え（照合はアダプタがする）。"""

    path: str
    #: テストのパスに当たる
    is_test: bool = False
    #: テストが要らないパスに当たる
    untested_ok: bool = False


@dataclass(frozen=True)
class GateEvidence:
    #: 親ブランチからのコミットの数
    commits: int
    #: 指摘の台帳で open の指摘（carried・rejected・closed は入れない）。G- の指摘は入っていても数えない
    open_findings: tuple[FindingId, ...]
    #: フローの最後の ReviewLoop の、最後のラウンドで reviewers に挙げたレビュー
    reviewers_expected: tuple[StageKind, ...]
    #: そのうち走り終えたレビュー
    reviewers_completed: tuple[StageKind, ...]
    #: フローに TestGen があるか
    has_test_gen: bool
    #: TestGen のコミット（Expect が期待値を書いたらそのコミット）の後で変わったファイル
    changed_since_tests: tuple[ChangedFile, ...] = ()
    #: そのタスクのコミットで変わったファイル
    changed: tuple[ChangedFile, ...] = ()
    #: そのタスクの verify の結果（VerifySelector が選んだもの）
    verify: tuple[VerifyResult, ...] = ()

    @property
    def tests_changed(self) -> tuple[str, ...]:
        """TestGen の後で変わったテストのファイル（項目 4）。"""
        return tuple(f.path for f in self.changed_since_tests if f.is_test)

    @property
    def outside_untested_paths(self) -> tuple[str, ...]:
        """変わったファイルのうち、テストが要らないパスに収まらないもの（項目 5）。"""
        return tuple(f.path for f in self.changed if not f.untested_ok)


@dataclass(frozen=True)
class GateOutcome:
    report: GateReport

    @property
    def passed(self) -> bool:
        return self.report.passed

    @property
    def escalation(self) -> EscalationKind | None:
        return GateEvaluator.escalation_for(self.report.failed)

    @property
    def findings(self) -> tuple[FindingSummary, ...]:
        """開く G- の指摘。上げるときは開かない（GateFailed にならない）ので空。"""
        if self.escalation is not None:
            return ()
        return GateEvaluator.findings_for(self.report.failed)


def _item(item: GateItem, failures: Iterable[str]) -> GateItemResult:
    reasons = list(failures)
    return GateItemResult(item, passed=not reasons, reason="; ".join(reasons))


class GateEvaluator:
    @staticmethod
    def evaluate(evidence: GateEvidence) -> GateOutcome:
        """項目は `GateItem` の順。TestGen の有無で、4 と 5 のどちらか一方だけを見る。"""
        missing_reviewers = [
            kind.value
            for kind in evidence.reviewers_expected
            if kind not in evidence.reviewers_completed
        ]
        review_findings = [f for f in evidence.open_findings if f.gate_item is None]
        items = [
            _item(
                GateItem.COMMITS,
                [] if evidence.commits >= 1 else ["親ブランチからのコミットが無い"],
            ),
            _item(
                GateItem.NO_OPEN_FINDINGS,
                [f"open の指摘: {', '.join(str(f) for f in review_findings)}"]
                if review_findings
                else [],
            ),
            _item(
                GateItem.REVIEWERS_RAN,
                [f"走り終えていないレビュー: {', '.join(missing_reviewers)}"]
                if missing_reviewers
                else [],
            ),
        ]
        if evidence.has_test_gen:
            items.append(
                _item(
                    GateItem.TESTS_UNCHANGED,
                    [f"TestGen の後で変わったテスト: {', '.join(evidence.tests_changed)}"]
                    if evidence.tests_changed
                    else [],
                )
            )
        else:
            items.append(
                _item(
                    GateItem.UNTESTED_PATHS,
                    [
                        "TestGen を抜いたのに、テストが要らないパスの外を変えた: "
                        + ", ".join(evidence.outside_untested_paths)
                    ]
                    if evidence.outside_untested_paths
                    else [],
                )
            )
        items.append(
            _item(
                GateItem.VERIFY,
                [
                    f"{result.command} が終了コード {result.exit_code} で落ちた"
                    for result in evidence.verify
                    if not result.passed
                ],
            )
        )
        return GateOutcome(GateReport(tuple(items)))

    @staticmethod
    def escalation_for(failed: Iterable[GateItemResult]) -> EscalationKind | None:
        """落ちた項目のうち、直せないものがあれば、項目の順で最初のもののエスカレーション。"""
        for result in failed:
            if not result.passed and result.item.escalation is not None:
                return result.item.escalation
        return None

    @staticmethod
    def findings_for(failed: Iterable[GateItemResult]) -> tuple[FindingSummary, ...]:
        """落ちた項目のうち、直せる項目の G- の指摘（must-fix）。台帳の RecordGateResult が開く。"""
        return tuple(
            FindingSummary(FindingId.gate(result.item), Rating.MUST_FIX, result.reason)
            for result in failed
            if not result.passed and result.item.escalation is None
        )
