"""GateEvaluator（`domain/services/gate.py`）と VerifySelector（`domain/services/verify.py`）。"""

from __future__ import annotations

import dataclasses

import pytest

from autodev.domain.services.gate import ChangedFile, GateEvaluator, GateEvidence
from autodev.domain.services.verify import VerifySelector
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.finding_id import FindingId
from autodev.domain.value_objects.finding_summary import FindingSummary
from autodev.domain.value_objects.gate_item import GateItem
from autodev.domain.value_objects.rating import Rating
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.verify_command import VerifyCommand
from autodev.domain.value_objects.verify_kind import VerifyKind
from autodev.domain.value_objects.verify_result import VerifyResult

PYTEST = VerifyCommand("uv run pytest -q")
RUFF = VerifyCommand("uv run ruff check .")

PASSING = GateEvidence(
    commits=2,
    open_findings=(),
    reviewers_expected=(StageKind.REVIEW, StageKind.ADVERSARIAL_REVIEW),
    reviewers_completed=(StageKind.ADVERSARIAL_REVIEW, StageKind.REVIEW),
    has_test_gen=True,
    verify=(VerifyResult(PYTEST, 0),),
)


#: TestGen の後でテストのファイルを変えた
TESTS_CHANGED = {"changed_since_tests": (ChangedFile("tests/test_a.py", is_test=True),)}
#: テストが要らないパスの外を変えた
OUTSIDE_UNTESTED = {"changed": (ChangedFile("src/a.py"),)}


def evaluate(**changes):
    return GateEvaluator.evaluate(dataclasses.replace(PASSING, **changes))


def failed_items(**changes) -> list[GateItem]:
    return [result.item for result in evaluate(**changes).report.failed]


# --- 項目 ---


def test_証拠が揃えば通りTestGenの有無で見る項目が変わる():
    outcome = GateEvaluator.evaluate(PASSING)
    assert outcome.passed
    assert [r.item for r in outcome.report.items] == [
        GateItem.COMMITS,
        GateItem.NO_OPEN_FINDINGS,
        GateItem.REVIEWERS_RAN,
        GateItem.TESTS_UNCHANGED,
        GateItem.VERIFY,
    ]
    without = evaluate(has_test_gen=False)
    assert GateItem.UNTESTED_PATHS in [r.item for r in without.report.items]
    assert GateItem.TESTS_UNCHANGED not in [r.item for r in without.report.items]


@pytest.mark.parametrize(
    ("changes", "item"),
    [
        ({"commits": 0}, GateItem.COMMITS),
        ({"open_findings": (FindingId("R2"),)}, GateItem.NO_OPEN_FINDINGS),
        ({"reviewers_completed": (StageKind.REVIEW,)}, GateItem.REVIEWERS_RAN),
        (TESTS_CHANGED, GateItem.TESTS_UNCHANGED),
        ({"has_test_gen": False, **OUTSIDE_UNTESTED}, GateItem.UNTESTED_PATHS),
        ({"verify": (VerifyResult(PYTEST, 0), VerifyResult(RUFF, 1))}, GateItem.VERIFY),
    ],
    ids=lambda v: v.value if isinstance(v, GateItem) else "",
)
def test_項目ごとに落ちる(changes: dict, item: GateItem):
    assert failed_items(**changes) == [item]


def test_TestGenがあればテストが要らないパスの外を変えてもよい():
    assert failed_items(**OUTSIDE_UNTESTED) == []


def test_TestGenが無ければテストを変えてもよい():
    assert failed_items(has_test_gen=False, **TESTS_CHANGED) == []


def test_照合の答えで見るファイルを絞る():
    # TestGen の後で変わってもテストでなければ、テストが要らないパスに収まれば、落ちない
    assert failed_items(changed_since_tests=(ChangedFile("src/a.py"),)) == []
    docs = (ChangedFile("docs/a.md", untested_ok=True),)
    assert failed_items(has_test_gen=False, changed=docs) == []


def test_verifyが空なら通る():
    assert failed_items(verify=()) == []


def test_落ちた理由にコマンドと終了コードとidを書く():
    outcome = evaluate(
        open_findings=(FindingId("R2"), FindingId("G-verify")),
        verify=(VerifyResult(RUFF, 1),),
    )
    reasons = {result.item: result.reason for result in outcome.report.failed}
    assert reasons[GateItem.NO_OPEN_FINDINGS] == "open の指摘: R2"
    assert reasons[GateItem.VERIFY] == "uv run ruff check . が終了コード 1 で落ちた"


def test_Gateが自分で判定するG_の指摘は残っている指摘に数えない():
    # 前の Gate が開いた G- の指摘で、次の Gate が必ず落ちることが無い
    assert (
        failed_items(open_findings=(FindingId("G-verify"), FindingId("G-no-open-findings"))) == []
    )


# --- 落ちた項目の扱い ---


def test_直せる項目だけが落ちたらG_の指摘を開き上げない():
    outcome = evaluate(open_findings=(FindingId("R2"),), verify=(VerifyResult(RUFF, 2),))
    assert outcome.escalation is None
    assert [(f.finding, f.rating) for f in outcome.findings] == [
        (FindingId("G-no-open-findings"), Rating.MUST_FIX),
        (FindingId("G-verify"), Rating.MUST_FIX),
    ]


@pytest.mark.parametrize(
    ("changes", "kind"),
    [
        ({"commits": 0}, EscalationKind.GATE_UNFIXABLE),
        ({"reviewers_completed": ()}, EscalationKind.GATE_UNFIXABLE),
        (TESTS_CHANGED, EscalationKind.GATE_UNFIXABLE),
        ({"has_test_gen": False, **OUTSIDE_UNTESTED}, EscalationKind.UNTESTED_CHANGE),
    ],
)
def test_直せない項目が落ちたら上げる(changes: dict, kind: EscalationKind):
    assert evaluate(**changes).escalation is kind


def test_直せない項目と直せる項目が両方落ちたら上げる方を取る():
    outcome = evaluate(commits=0, verify=(VerifyResult(RUFF, 1),))
    assert outcome.escalation is EscalationKind.GATE_UNFIXABLE
    # 上げるので GateFailed にならず、G- の指摘も開かない
    assert outcome.findings == ()
    # 項目ごとの合否から直せる項目を引くことはできる（台帳の RecordGateResult が使う）
    assert [f.finding for f in GateEvaluator.findings_for(outcome.report.failed)] == [
        FindingId("G-verify")
    ]


def test_通った項目からは何も開かない():
    assert GateEvaluator.findings_for(GateEvaluator.evaluate(PASSING).report.items) == ()
    assert GateEvaluator.escalation_for(GateEvaluator.evaluate(PASSING).report.items) is None


def test_GateFailedの項目から開く指摘の中身():
    (result,) = evaluate(verify=(VerifyResult(RUFF, 1),)).report.failed
    assert GateEvaluator.findings_for((result,)) == (
        FindingSummary(FindingId("G-verify"), Rating.MUST_FIX, result.reason),
    )


# --- VerifySelector ---


TY = VerifyCommand("uv run ty check")


def test_ConfirmRedはタスクのテストと軽い検査を重なりを除いて流し回帰テストは流さない():
    selected = VerifySelector.select(
        StageKind.CONFIRM_RED,
        task_tests=(PYTEST, RUFF),
        quick_checks=(RUFF, TY),
        regression_tests=(PYTEST, VerifyCommand("uv run pytest -m slow")),
    )
    assert selected == (PYTEST, RUFF, TY)


def test_TestGenは軽い検査だけを流しタスクのテストは流さない():
    selected = VerifySelector.select(
        StageKind.TEST_GEN,
        task_tests=(PYTEST,),
        quick_checks=(RUFF, TY),
        regression_tests=(PYTEST,),
    )
    assert selected == (RUFF, TY)


def test_planは流す順にコマンドと種類の組を返す():
    kinds = VerifyKind
    plan = VerifySelector.plan(StageKind.CONFIRM_RED, task_tests=(PYTEST,), quick_checks=(RUFF,))
    assert plan == ((PYTEST, kinds.TASK_TESTS), (RUFF, kinds.QUICK_CHECKS))
    assert VerifySelector.plan(StageKind.TEST_GEN, task_tests=(PYTEST,), quick_checks=(RUFF,)) == (
        (RUFF, kinds.QUICK_CHECKS),
    )
    assert VerifySelector.plan(
        StageKind.INTEGRATION_CHECK, quick_checks=(RUFF,), regression_tests=(PYTEST,)
    ) == ((RUFF, kinds.QUICK_CHECKS), (PYTEST, kinds.REGRESSION_TESTS))


def test_planは同じコマンドが2つの種類に出たら先に出た種類で1回だけ返す():
    plan = VerifySelector.plan(StageKind.CONFIRM_RED, task_tests=(RUFF,), quick_checks=(RUFF,))
    assert plan == ((RUFF, VerifyKind.TASK_TESTS),)
    gate = VerifySelector.plan(StageKind.GATE, task_tests=(PYTEST, RUFF), quick_checks=(RUFF, TY))
    assert gate == (
        (PYTEST, VerifyKind.TASK_TESTS),
        (RUFF, VerifyKind.TASK_TESTS),
        (TY, VerifyKind.QUICK_CHECKS),
    )


def test_planは検証コマンドを流さないステージには選ばない():
    with pytest.raises(ValueError, match="Impl は検証コマンドを流さない"):
        VerifySelector.plan(StageKind.IMPL, task_tests=(PYTEST,))


def test_Gateはタスクのテストと軽い検査を重なりを除いて初めに出た順に流す():
    selected = VerifySelector.select(
        StageKind.GATE,
        task_tests=(PYTEST, RUFF),
        quick_checks=(RUFF, TY),
        regression_tests=(PYTEST,),
    )
    assert selected == (PYTEST, RUFF, TY)


def test_統合検査は軽い検査と回帰テストを流しタスクのテストは入れない():
    selected = VerifySelector.select(
        StageKind.INTEGRATION_CHECK,
        task_tests=(TY,),
        quick_checks=(RUFF,),
        regression_tests=(PYTEST, RUFF),
    )
    assert selected == (RUFF, PYTEST)


@pytest.mark.parametrize(
    "stage",
    [StageKind.CONFIRM_RED, StageKind.TEST_GEN, StageKind.GATE, StageKind.INTEGRATION_CHECK],
)
def test_3つとも空なら流すものが無い(stage: StageKind):
    assert VerifySelector.select(stage) == ()
    assert VerifySelector.plan(stage) == ()


def test_回帰テストは軽い検査に移らずGateとConfirmRedでは流れない():
    assert VerifySelector.select(StageKind.GATE, regression_tests=(PYTEST,)) == ()
    assert VerifySelector.select(StageKind.CONFIRM_RED, regression_tests=(PYTEST,)) == ()
    assert VerifySelector.select(StageKind.TEST_GEN, regression_tests=(PYTEST,)) == ()


def test_検証コマンドを流さないステージには選ばない():
    with pytest.raises(ValueError, match="Impl は検証コマンドを流さない"):
        VerifySelector.select(StageKind.IMPL, task_tests=(PYTEST,), quick_checks=(RUFF,))
