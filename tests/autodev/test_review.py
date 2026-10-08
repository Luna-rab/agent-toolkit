"""ReviewLedger（`domain/aggregates/review_ledger.py`）。コマンドとイベントの列だけで確かめる。"""

from __future__ import annotations

import itertools

import pytest

from autodev.domain.aggregates.base import Rejected
from autodev.domain.aggregates.review_ledger import JudgeCapability, ReviewLedger
from autodev.domain.commands.base import Command
from autodev.domain.commands.review_ledger import (
    CarryFinding,
    CommentFinding,
    CountFix,
    JudgeFinding,
    RaiseFinding,
    RecordFindings,
    RecordGateResult,
    RecordJudgement,
    TrackProposal,
)
from autodev.domain.events.base import Event
from autodev.domain.events.review_ledger import (
    CarryRefused,
    CommentRefused,
    FindingCarried,
    FindingClosed,
    FindingCommented,
    FindingRaised,
    FindingRejected,
    FindingReopened,
    FindingRetargeted,
    FindingsEvaluated,
    FindingStalled,
    FixCounted,
    ProposalTracked,
    ResultReceived,
    ResultRefused,
)
from autodev.domain.services.stall import StallPolicy
from autodev.domain.value_objects.base import InvalidValue
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.design_cause import DesignCause
from autodev.domain.value_objects.design_judgement import DesignJudgement
from autodev.domain.value_objects.design_version import DesignVersion
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.finding_id import FindingId
from autodev.domain.value_objects.finding_origin import FindingOrigin
from autodev.domain.value_objects.finding_status import FindingStatus
from autodev.domain.value_objects.finding_summary import FindingSummary
from autodev.domain.value_objects.finding_target import FindingTarget
from autodev.domain.value_objects.finding_verdict import FindingVerdict
from autodev.domain.value_objects.gate_item import GateItem
from autodev.domain.value_objects.gate_item_result import GateItemResult
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.limits import STALL_AFTER_FIXES
from autodev.domain.value_objects.location import Location
from autodev.domain.value_objects.rating import Rating
from autodev.domain.value_objects.reported_finding import ReportedFinding
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stall_cause import StallCause
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId

TASK = TaskId("task2")
PLANNING = TaskId.planning()
TASK_STREAM = StreamId.review(TASK)
DESIGN_STREAM = StreamId.design_review()
POLICY = Issuer.policy("review-loop", EventId("task/task2#3"))
_ids = itertools.count(1)
_rounds = itertools.count(1)


def execution(stage: StageKind, task: TaskId = TASK, round: int | None = None) -> ExecutionId:
    """実行の id はタスクの一生を通して重ならないので、ラウンドを毎回変える。"""
    return ExecutionId(task, stage, next(_rounds) if round is None else round, 1)


JUDGE = execution(StageKind.JUDGE, round=1)
FIX = execution(StageKind.FIX, round=1)
REVIEW = execution(StageKind.REVIEW, round=1)
GATE = execution(StageKind.GATE, round=0)
DESIGN_JUDGE = execution(StageKind.DESIGN_JUDGE, PLANNING, round=1)


def cid() -> CommandId:
    return CommandId(f"c{next(_ids)}")


def drive(ledger: ReviewLedger, command: Command) -> list[Event]:
    """メインループの代わり。handle で決めたイベントを当てる。"""
    events = ledger.handle(command)
    for event in events:
        ledger.apply(event, command.command_id)
    return events


def raise_finding(
    ledger: ReviewLedger,
    body: str = "境界の値で落ちる",
    rating: Rating = Rating.MUST_FIX,
    **kwargs,
) -> list[Event]:
    return drive(
        ledger,
        RaiseFinding(
            command_id=cid(),
            issuer=POLICY,
            ledger=ledger.stream,
            rating=rating,
            body=body,
            **kwargs,
        ),
    )


def judge(
    ledger: ReviewLedger,
    finding: str,
    to: FindingStatus,
    comment: str = "直っている",
    by: ExecutionId = JUDGE,
) -> list[Event]:
    return drive(
        ledger,
        JudgeFinding(
            command_id=cid(),
            issuer=POLICY,
            ledger=ledger.stream,
            finding=FindingId(finding),
            to=to,
            comment=comment,
            execution=by,
        ),
    )


def count_fix(ledger: ReviewLedger, by: ExecutionId | None = None) -> list[Event]:
    by = by or execution(StageKind.FIX)
    return drive(
        ledger, CountFix(command_id=cid(), issuer=POLICY, ledger=ledger.stream, execution=by)
    )


def evaluate(
    ledger: ReviewLedger,
    by: ExecutionId | None = None,
    *verdicts: FindingVerdict,
    **kwargs,
) -> list[Event]:
    """判定を当てて締める（判定が無ければ、締めるだけ）。設計の台帳は、今の提案の版を見たとする。"""
    by = by or execution(StageKind.JUDGE)
    if ledger.is_design:
        kwargs.setdefault("design", ledger.since)
    return drive(
        ledger,
        RecordJudgement(
            command_id=cid(),
            issuer=POLICY,
            ledger=ledger.stream,
            execution=by,
            verdicts=verdicts,
            **kwargs,
        ),
    )


def record(
    ledger: ReviewLedger, by: ExecutionId, *findings: ReportedFinding, **kwargs
) -> list[Event]:
    return drive(
        ledger,
        RecordFindings(
            command_id=cid(),
            issuer=POLICY,
            ledger=ledger.stream,
            source=by,
            findings=findings,
            **kwargs,
        ),
    )


def gate(
    ledger: ReviewLedger, *failed: GateItemResult, by: ExecutionId | None = None
) -> list[Event]:
    by = by or execution(StageKind.GATE)
    return drive(
        ledger,
        RecordGateResult(
            command_id=cid(), issuer=POLICY, ledger=ledger.stream, execution=by, failed=failed
        ),
    )


def fails(item: GateItem, reason: str = "pytest が落ちた") -> GateItemResult:
    return GateItemResult(item, passed=False, reason=reason)


def carry(ledger: ReviewLedger, finding: str, to: str = "task3") -> list[Event]:
    return drive(
        ledger,
        CarryFinding(
            command_id=cid(),
            issuer=POLICY,
            ledger=ledger.stream,
            finding=FindingId(finding),
            to_task=TaskId(to),
        ),
    )


def track(ledger: ReviewLedger, version: int) -> list[Event]:
    return drive(
        ledger,
        TrackProposal(
            command_id=cid(), issuer=POLICY, ledger=ledger.stream, design=DesignVersion(version)
        ),
    )


def design_ledger(version: int = 1) -> ReviewLedger:
    ledger = ReviewLedger(DESIGN_STREAM)
    track(ledger, version)
    return ledger


def status(ledger: ReviewLedger, finding: str) -> FindingStatus:
    return ledger.findings[FindingId(finding)].status


def evaluated(events: list[Event]) -> FindingsEvaluated:
    last = events[-1]
    assert isinstance(last, FindingsEvaluated)
    return last


def open_ids(events: list[Event]) -> list[str]:
    return [str(f.finding) for f in evaluated(events).open_findings]


VERIFY = FindingId.gate(GateItem.VERIFY)


# --- 立てる ---


def test_指摘は台帳の中で番号を振って立つ():
    ledger = ReviewLedger(TASK_STREAM)
    first = raise_finding(ledger, location=Location("src/a.py:3"), source=REVIEW)
    raise_finding(ledger, "名前が曖昧", Rating.NIT)
    assert first == [
        FindingRaised(
            FindingId("R1"),
            Rating.MUST_FIX,
            "境界の値で落ちる",
            location=Location("src/a.py:3"),
            source=REVIEW,
        )
    ]
    assert list(ledger.findings) == [FindingId("R1"), FindingId("R2")]
    assert status(ledger, "R2") is FindingStatus.OPEN


def test_設計の台帳はDの番号で立ちレビューした版を持つ():
    ledger = design_ledger()
    assert raise_finding(ledger, design=DesignVersion(1)) == [
        FindingRaised(FindingId("D1"), Rating.MUST_FIX, "境界の値で落ちる", design=DesignVersion(1))
    ]
    assert ledger.owner == PLANNING


def test_設計の版は設計の台帳の指摘だけが持ち設計の台帳では要る():
    with pytest.raises(Rejected, match="設計の台帳の指摘だけ"):
        raise_finding(ReviewLedger(TASK_STREAM), design=DesignVersion(1))
    with pytest.raises(Rejected, match="版を持つ"):
        raise_finding(design_ledger())
    with pytest.raises(Rejected, match="TrackProposal が先"):
        raise_finding(ReviewLedger(DESIGN_STREAM), design=DesignVersion(1))


def test_本文が空の指摘は立てない():
    with pytest.raises(Rejected, match="本文が空"):
        raise_finding(ReviewLedger(TASK_STREAM), "  ")


# --- 状態の遷移 ---


def test_ジャッジはopenを閉じ却下し閉じたものを開き直せる():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    raise_finding(ledger)
    assert judge(ledger, "R1", FindingStatus.CLOSED) == [
        FindingClosed(FindingId("R1"), "直っている", JUDGE)
    ]
    assert judge(ledger, "R2", FindingStatus.REJECTED, "指摘の誤り") == [
        FindingRejected(FindingId("R2"), "指摘の誤り", JUDGE)
    ]
    assert judge(ledger, "R1", FindingStatus.OPEN, "次のラウンドで壊れた") == [
        FindingReopened(FindingId("R1"), "次のラウンドで壊れた", JUDGE)
    ]
    judge(ledger, "R2", FindingStatus.OPEN, "やはり要る")
    assert ledger.findings[FindingId("R1")].comments == ["直っている", "次のラウンドで壊れた"]


@pytest.mark.parametrize(
    ("start", "to"),
    [
        (FindingStatus.CLOSED, FindingStatus.REJECTED),
        (FindingStatus.REJECTED, FindingStatus.CLOSED),
        (FindingStatus.OPEN, FindingStatus.CARRIED),
    ],
)
def test_許されない遷移を拒む(start: FindingStatus, to: FindingStatus):
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    if start is not FindingStatus.OPEN:
        judge(ledger, "R1", start)
    with pytest.raises(Rejected, match="へは動かせない"):
        judge(ledger, "R1", to)


@pytest.mark.parametrize("status", [FindingStatus.OPEN, FindingStatus.CLOSED])
def test_もうその状態にある指摘への判定は当て済みとして何もしない(status: FindingStatus):
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    if status is not FindingStatus.OPEN:
        judge(ledger, "R1", status)
    assert judge(ledger, "R1", status) == []


def test_移した指摘は終端でジャッジも動かせない():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    carry(ledger, "R1")
    with pytest.raises(Rejected, match="carried から open へは動かせない"):
        judge(ledger, "R1", FindingStatus.OPEN)


def test_状態を変えるときはコメントが要る():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    with pytest.raises(Rejected, match="コメントが無い"):
        judge(ledger, "R1", FindingStatus.CLOSED, comment=" ")


def test_無い指摘は判定もコメントもできない():
    ledger = ReviewLedger(TASK_STREAM)
    with pytest.raises(Rejected, match="R9 の指摘が無い"):
        judge(ledger, "R9", FindingStatus.CLOSED)
    # コメントはステージの結果から来るので、拒まずに受けなかったことを残す
    comment = CommentFinding(
        command_id=cid(),
        issuer=POLICY,
        ledger=TASK_STREAM,
        finding=FindingId("R9"),
        body="x",
        author=FIX,
    )
    assert drive(ledger, comment) == [
        CommentRefused(FindingId("R9"), f"{TASK_STREAM} に R9 の指摘が無い", FIX)
    ]


def test_コメントは状態を変えずに残る():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    events = drive(
        ledger,
        CommentFinding(
            command_id=cid(),
            issuer=POLICY,
            ledger=TASK_STREAM,
            finding=FindingId("R1"),
            body="ここを直した",
            author=FIX,
        ),
    )
    assert events == [FindingCommented(FindingId("R1"), "ここを直した", FIX)]
    assert status(ledger, "R1") is FindingStatus.OPEN


# --- JudgeCapability: 指摘にはそれぞれ判定する者がいる ---


def test_Gateの項目の指摘のidは項目を指す():
    assert VERIFY.gate_item is GateItem.VERIFY
    assert FindingId("R3").gate_item is None
    with pytest.raises(InvalidValue, match="Gate の項目に無い"):
        FindingId("G-nope")


@pytest.mark.parametrize(
    ("ledger", "finding", "judge_stage"),
    [
        (TASK_STREAM, FindingId("R1"), StageKind.JUDGE),
        (TASK_STREAM, VERIFY, StageKind.GATE),
        (DESIGN_STREAM, FindingId("D1"), StageKind.DESIGN_JUDGE),
    ],
)
def test_指摘ごとに判定する者が決まっている(
    ledger: StreamId, finding: FindingId, judge_stage: StageKind
):
    assert JudgeCapability.judge_of(ledger, finding) is judge_stage
    owner = JudgeCapability.owner(ledger)
    assert JudgeCapability.allows(ExecutionId(owner, judge_stage, 1, 1), ledger, finding)
    others = set(StageKind) - {judge_stage}
    assert not any(
        JudgeCapability.allows(ExecutionId(owner, stage, 1, 1), ledger, finding) for stage in others
    )
    assert not JudgeCapability.allows(
        ExecutionId(TaskId("task9"), judge_stage, 1, 1), ledger, finding
    )


@pytest.mark.parametrize(
    "by",
    [
        execution(StageKind.REVIEW),
        execution(StageKind.FIX),
        execution(StageKind.ADVERSARIAL_REVIEW),
        execution(StageKind.GATE),
        execution(StageKind.DESIGN_JUDGE, PLANNING),
    ],
    ids=lambda e: e.stage.value,
)
def test_レビューの指摘はJudgeの結果でしか動かない(by: ExecutionId):
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    with pytest.raises(Rejected, match="task2 の Judge の実行の結果だけ"):
        judge(ledger, "R1", FindingStatus.CLOSED, by=by)
    assert status(ledger, "R1") is FindingStatus.OPEN


def test_Gateの項目の指摘はGateの結果でしか動かない():
    ledger = ReviewLedger(TASK_STREAM)
    gate(ledger, fails(GateItem.VERIFY))
    with pytest.raises(Rejected, match="task2 の Gate の実行の結果だけ"):
        judge(ledger, str(VERIFY), FindingStatus.CLOSED)
    with pytest.raises(Rejected, match="task2 の Gate の実行の結果だけ"):
        gate(ledger, by=execution(StageKind.JUDGE))


def test_ほかの台帳のジャッジでは状態を動かせない():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    with pytest.raises(Rejected, match="task2 の Judge"):
        judge(ledger, "R1", FindingStatus.CLOSED, by=execution(StageKind.JUDGE, TaskId("task3")))
    design = design_ledger()
    raise_finding(design, design=DesignVersion(1))
    with pytest.raises(Rejected, match="planning の DesignJudge"):
        judge(design, "D1", FindingStatus.CLOSED)
    assert judge(design, "D1", FindingStatus.CLOSED, by=DESIGN_JUDGE)


# --- 修正の回数と停滞 ---


def test_修正はそのときopenの指摘だけを数える():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    raise_finding(ledger)
    judge(ledger, "R2", FindingStatus.CLOSED)
    assert count_fix(ledger, FIX) == [FixCounted(FIX, (FindingId("R1"),))]
    assert ledger.findings[FindingId("R1")].fixes == 1
    assert ledger.findings[FindingId("R2")].fixes == 0


TEST_FIX = execution(StageKind.TEST_FIX, round=1)


def raise_tests_finding(ledger: ReviewLedger, body: str = "テストが無い") -> list[Event]:
    return raise_finding(ledger, body, finding_target=FindingTarget.TESTS)


def test_TestFixの修正はそのときopenでtests宛の指摘だけを数える():
    ledger = ReviewLedger(TASK_STREAM)
    raise_tests_finding(ledger)
    raise_finding(ledger, "実装の分岐が抜けている")
    raise_tests_finding(ledger, "別のテストが無い")
    judge(ledger, "R3", FindingStatus.CLOSED)
    assert count_fix(ledger, TEST_FIX) == [FixCounted(TEST_FIX, (FindingId("R1"),))]
    assert [ledger.findings[FindingId(f"R{n}")].fixes for n in (1, 2, 3)] == [1, 0, 0]


def test_Fixの修正はimpl宛の指摘だけを数えGateの項目の指摘も含める():
    ledger = ReviewLedger(TASK_STREAM)
    raise_tests_finding(ledger)
    raise_finding(ledger, "実装の分岐が抜けている")
    gate(ledger, fails(GateItem.VERIFY), by=GATE)
    (counted,) = count_fix(ledger, FIX)
    assert isinstance(counted, FixCounted)
    assert set(counted.findings) == {FindingId("R2"), VERIFY}
    assert ledger.findings[FindingId("R1")].fixes == 0
    assert ledger.findings[FindingId("R2")].fixes == 1
    assert ledger.findings[VERIFY].fixes == 1


def test_宛先を書き換えられた指摘は書き換えた後の宛先の直す役が数える():
    ledger = ReviewLedger(TASK_STREAM)
    raise_tests_finding(ledger)
    evaluate(ledger, JUDGE, retarget("R1", FindingTarget.IMPL))
    assert count_fix(ledger, TEST_FIX) == [FixCounted(TEST_FIX, ())]
    assert count_fix(ledger, FIX) == [FixCounted(FIX, (FindingId("R1"),))]


def test_tests宛の指摘もTestFixをSTALL_AFTER_FIXES回受けてもopenなら停滞する():
    ledger = ReviewLedger(TASK_STREAM)
    raise_tests_finding(ledger)
    for _ in range(STALL_AFTER_FIXES):
        count_fix(ledger, TEST_FIX)
    events = evaluate(ledger, JUDGE)
    assert evaluated(events).stalled == (FindingId("R1"),)
    assert [f.target for f in evaluated(events).open_findings] == [FindingTarget.TESTS]


def test_FixはTestFixの数を進めずimplとtestsは宛先ごとに数え分ける():
    ledger = ReviewLedger(TASK_STREAM)
    raise_tests_finding(ledger)
    raise_finding(ledger, "実装の分岐が抜けている")
    for _ in range(STALL_AFTER_FIXES):
        count_fix(ledger, FIX)
    # Fix を何回受けても、tests 宛の R1 は停滞しない。impl 宛の R2 だけが停滞する
    assert evaluated(evaluate(ledger, JUDGE)).stalled == (FindingId("R2"),)
    for _ in range(STALL_AFTER_FIXES):
        count_fix(ledger, TEST_FIX)
    # R2 は上げたばかりなので上げ直さない。TestFix を受けた R1 が停滞する
    assert evaluated(evaluate(ledger, JUDGE)).stalled == (FindingId("R1"),)


def test_CountFixは数えるだけで停滞を出さない():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    for _ in range(STALL_AFTER_FIXES + 1):
        assert [type(e) for e in count_fix(ledger)] == [FixCounted]


def test_同じ修正のコマンドを2回受けても1回しか数えない():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    command = CountFix(command_id=cid(), issuer=POLICY, ledger=TASK_STREAM, execution=FIX)
    drive(ledger, command)
    assert drive(ledger, command) == []
    assert ledger.findings[FindingId("R1")].fixes == 1


@pytest.mark.parametrize(
    ("stream", "by", "reason"),
    [
        (TASK_STREAM, execution(StageKind.IMPL), "Fix の実行だけ"),
        (TASK_STREAM, execution(StageKind.FIX, TaskId("task3")), "Fix の実行だけ"),
        (DESIGN_STREAM, execution(StageKind.REVISE, PLANNING), "修正を数えない"),
    ],
)
def test_数えるのは持ち主のFixだけで設計の台帳は数えない(
    stream: StreamId, by: ExecutionId, reason: str
):
    with pytest.raises(Rejected, match=reason):
        count_fix(ReviewLedger(stream), by)


@pytest.mark.parametrize(
    ("stream", "by"),
    [
        (TASK_STREAM, execution(StageKind.TEST_FIX, TaskId("task3"))),
        (DESIGN_STREAM, execution(StageKind.TEST_FIX, PLANNING)),
    ],
    ids=["別のタスクのTestFix", "設計の台帳"],
)
def test_TestFixも持ち主のタスクの台帳でだけ数える(stream: StreamId, by: ExecutionId):
    with pytest.raises(Rejected):
        count_fix(ReviewLedger(stream), by)


def test_判定の後にSTALL_AFTER_FIXES回直してもopenなら停滞する():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    raise_finding(ledger, "別の指摘")
    for _ in range(STALL_AFTER_FIXES):
        count_fix(ledger)
    judge(ledger, "R2", FindingStatus.CLOSED)
    assert evaluate(ledger, JUDGE) == [
        FindingStalled(FindingId("R1"), STALL_AFTER_FIXES),
        FindingsEvaluated(
            JUDGE,
            open_findings=(FindingSummary(FindingId("R1"), Rating.MUST_FIX, "境界の値で落ちる"),),
            stalled=(FindingId("R1"),),
        ),
    ]


def test_判定で閉じた指摘は修正の回数が多くても停滞しない():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    for _ in range(STALL_AFTER_FIXES):
        count_fix(ledger)
    judge(ledger, "R1", FindingStatus.CLOSED)
    assert evaluate(ledger, JUDGE) == [FindingsEvaluated(JUDGE, open_findings=())]


def test_停滞が無くても判定を締めたことは出る():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger, rating=Rating.SHOULD_FIX)
    count_fix(ledger)
    events = evaluate(ledger)
    assert len(events) == 1
    assert evaluated(events).stalled == ()
    assert open_ids(events) == ["R1"]


def test_判定を締めるのは台帳の種類に合ったジャッジだけ():
    with pytest.raises(Rejected, match="Judge の実行の結果だけ"):
        evaluate(ReviewLedger(TASK_STREAM), FIX)
    with pytest.raises(Rejected, match="Judge の実行の結果だけ"):
        evaluate(ReviewLedger(TASK_STREAM), execution(StageKind.DESIGN_JUDGE, TASK))
    with pytest.raises(Rejected, match="DesignJudge の実行の結果だけ"):
        evaluate(design_ledger(), execution(StageKind.JUDGE, PLANNING))


def test_停滞として上げた指摘はそこからまたSTALL_AFTER_FIXES回直すまで上げ直さない():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    for _ in range(STALL_AFTER_FIXES):
        count_fix(ledger)
    assert evaluated(evaluate(ledger)).stalled == (FindingId("R1"),)
    # 回答を受けて直し始めた。1 回直しただけでは上げない
    count_fix(ledger)
    assert evaluated(evaluate(ledger)).stalled == ()
    for _ in range(STALL_AFTER_FIXES - 1):
        count_fix(ledger)
    assert evaluated(evaluate(ledger)).stalled == (FindingId("R1"),)


def test_停滞の判定はopenで前に上げた所から回数が届いたものだけ():
    assert StallPolicy.is_stalled(FindingStatus.OPEN, STALL_AFTER_FIXES)
    assert not StallPolicy.is_stalled(FindingStatus.OPEN, STALL_AFTER_FIXES - 1)
    assert not StallPolicy.is_stalled(FindingStatus.CLOSED, STALL_AFTER_FIXES + 3)
    assert not StallPolicy.is_stalled(FindingStatus.CARRIED, STALL_AFTER_FIXES)
    assert not StallPolicy.is_stalled(FindingStatus.OPEN, STALL_AFTER_FIXES + 1, 2)
    assert StallPolicy.is_stalled(FindingStatus.OPEN, 2 + STALL_AFTER_FIXES, 2)


def test_Judgeが締めるときGateの項目の指摘は数えない():
    ledger = ReviewLedger(TASK_STREAM)
    gate(ledger, fails(GateItem.VERIFY))
    raise_finding(ledger)
    judge(ledger, "R1", FindingStatus.CLOSED)
    # G-verify が open でも、Judge から見て残りは無いので ReviewLoop を抜けられる
    assert open_ids(evaluate(ledger)) == []


# --- Gate の項目の指摘（G-） ---


def test_Gateが落ちたら項目ごとに決まったidの指摘を開き番号を使わない():
    ledger = ReviewLedger(TASK_STREAM)
    events = gate(ledger, fails(GateItem.VERIFY, "pytest が落ちた"), by=GATE)
    assert events == [
        FindingRaised(VERIFY, Rating.MUST_FIX, "pytest が落ちた", source=GATE),
        FindingsEvaluated(
            GATE, open_findings=(FindingSummary(VERIFY, Rating.MUST_FIX, "pytest が落ちた"),)
        ),
    ]
    raise_finding(ledger)
    assert list(ledger.findings) == [VERIFY, FindingId("R1")]


def test_Gateが通ればopenのGateの項目の指摘を閉じる():
    ledger = ReviewLedger(TASK_STREAM)
    gate(ledger, fails(GateItem.VERIFY), fails(GateItem.NO_OPEN_FINDINGS, "R1 が open"))
    events = gate(ledger, fails(GateItem.NO_OPEN_FINDINGS, "R1 が open"), by=GATE)
    assert events[0] == FindingClosed(VERIFY, "Gate の G-verify が通った", GATE)
    assert open_ids(events) == ["G-no-open-findings"]
    events = gate(ledger, by=GATE)
    assert open_ids(events) == []
    assert status(ledger, "G-no-open-findings") is FindingStatus.CLOSED


def test_閉じたGateの項目がまた落ちたら開き直しopenならそのまま():
    ledger = ReviewLedger(TASK_STREAM)
    gate(ledger, fails(GateItem.VERIFY, "1 回目"))
    assert [type(e) for e in gate(ledger, fails(GateItem.VERIFY, "2 回目"))] == [FindingsEvaluated]
    gate(ledger)
    events = gate(ledger, fails(GateItem.VERIFY, "3 回目"), by=GATE)
    assert events[0] == FindingReopened(VERIFY, "3 回目", GATE)
    assert status(ledger, str(VERIFY)) is FindingStatus.OPEN


def test_Gateがまた落ちたとき修正をSTALL_AFTER_FIXES回受けていれば停滞する():
    ledger = ReviewLedger(TASK_STREAM)
    gate(ledger, fails(GateItem.VERIFY))
    for _ in range(STALL_AFTER_FIXES - 1):
        count_fix(ledger)
        assert evaluated(gate(ledger, fails(GateItem.VERIFY))).stalled == ()
    count_fix(ledger)
    events = gate(ledger, fails(GateItem.VERIFY), by=GATE)
    assert FindingStalled(VERIFY, STALL_AFTER_FIXES) in events
    assert evaluated(events).stalled == (VERIFY,)


def test_閉じた後に開き直しても修正の回数は積み上がり停滞に掛かる():
    ledger = ReviewLedger(TASK_STREAM)
    gate(ledger, fails(GateItem.VERIFY))
    count_fix(ledger)
    gate(ledger)
    count_fix(ledger)  # ほかの指摘の Fix。G-verify は閉じているので数えない
    assert ledger.findings[VERIFY].fixes == 1
    gate(ledger, fails(GateItem.VERIFY))
    count_fix(ledger)
    assert evaluated(gate(ledger, fails(GateItem.VERIFY))).stalled == (VERIFY,)


def test_Gateと修正を回しても停滞で必ず止まる():
    """Gate → Fix → Judge → Gate の輪は、STALL_AFTER_FIXES 回で停滞として上がる。"""
    ledger = ReviewLedger(TASK_STREAM)
    gates = 0
    while gates < 10:
        gates += 1
        if evaluated(gate(ledger, fails(GateItem.VERIFY))).stalled:
            break
        count_fix(ledger)
        assert open_ids(evaluate(ledger)) == []  # Judge には残りが無い
    assert gates == STALL_AFTER_FIXES + 1


@pytest.mark.parametrize(
    ("failed", "reason"),
    [
        ((GateItemResult(GateItem.VERIFY, passed=True),), "通った項目がある"),
        ((fails(GateItem.COMMITS),), "直せない項目"),
        ((fails(GateItem.VERIFY), fails(GateItem.UNTESTED_PATHS)), "直せない項目"),
    ],
)
def test_Gateの結果の誤った渡し方を拒む(failed: tuple[GateItemResult, ...], reason: str):
    with pytest.raises(Rejected, match=reason):
        gate(ReviewLedger(TASK_STREAM), *failed)


def test_設計の台帳にGateの項目の指摘は立てない():
    with pytest.raises(Rejected, match="設計の台帳に Gate"):
        gate(design_ledger(), fails(GateItem.VERIFY), by=execution(StageKind.GATE, PLANNING))


def carry_refused(events: list[Event]) -> str:
    """移せなかった（再計画の提案の中身の問題は、拒否ではなくイベントで残る）。理由を返す。"""
    (event,) = events
    assert isinstance(event, CarryRefused)
    return event.reason


def test_Gateの項目の指摘は移さない():
    ledger = ReviewLedger(TASK_STREAM)
    gate(ledger, fails(GateItem.VERIFY))
    assert "Gate の項目の指摘" in carry_refused(carry(ledger, str(VERIFY)))


# --- 設計の台帳は今の提案の指摘だけを数える ---


def test_設計の台帳は捨てた提案の指摘を判定の締めに数えない():
    ledger = design_ledger(1)
    raise_finding(ledger, "古い提案の指摘", design=DesignVersion(1))
    raise_finding(ledger, "直した版の指摘", design=DesignVersion(1))
    assert open_ids(evaluate(ledger, DESIGN_JUDGE)) == ["D1", "D2"]
    assert track(ledger, 3) == [ProposalTracked(DesignVersion(3))]
    raise_finding(ledger, "新しい提案の指摘", design=DesignVersion(4))
    assert open_ids(evaluate(ledger, execution(StageKind.DESIGN_JUDGE, PLANNING))) == ["D3"]
    assert status(ledger, "D1") is FindingStatus.OPEN  # 台帳には残る


def test_今の提案より前の版には指摘を立てず追う版は戻らない():
    ledger = design_ledger(3)
    with pytest.raises(Rejected, match="今の提案（版 3 から）より前"):
        raise_finding(ledger, design=DesignVersion(2))
    with pytest.raises(Rejected, match="より後ではない"):
        track(ledger, 3)
    with pytest.raises(Rejected, match="設計の台帳だけ"):
        track(ReviewLedger(TASK_STREAM), 1)


# --- 移管 ---


def test_移す元はcarriedになり未解決に数えない():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger, location=Location("src/a.py"))
    assert carry(ledger, "R1") == [
        FindingCarried(
            FindingId("R1"),
            TaskId("task3"),
            Rating.MUST_FIX,
            "境界の値で落ちる",
            Location("src/a.py"),
        )
    ]
    assert status(ledger, "R1") is FindingStatus.CARRIED
    assert ledger.open_findings == ()
    assert open_ids(evaluate(ledger)) == []


def test_2回は移さずopenでない指摘も移さない():
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    raise_finding(ledger)
    carry(ledger, "R1")
    assert "すでに task3 へ移した" in carry_refused(carry(ledger, "R1", "task4"))
    judge(ledger, "R2", FindingStatus.CLOSED)
    assert "open の指摘だけ" in carry_refused(carry(ledger, "R2"))
    assert "R9 の指摘が無い" in carry_refused(carry(ledger, "R9"))
    assert status(ledger, "R2") is FindingStatus.CLOSED


@pytest.mark.parametrize("to", ["task2", "planning", "git"])
def test_移す先は別の実装タスク(to: str):
    ledger = ReviewLedger(TASK_STREAM)
    raise_finding(ledger)
    assert "別の実装タスク" in carry_refused(carry(ledger, "R1", to))


def test_移した先の台帳には1回だけ立つ():
    origin = FindingOrigin(TASK_STREAM, FindingId("R1"))
    target = ReviewLedger(StreamId.review(TaskId("task3")))
    assert raise_finding(target, carried_from=origin)[0] == FindingRaised(
        FindingId("R1"), Rating.MUST_FIX, "境界の値で落ちる", carried_from=origin
    )
    with pytest.raises(Rejected, match="すでにこの台帳へ移してある"):
        raise_finding(target, carried_from=origin)


def test_設計の台帳の指摘は移さず設計の台帳へも移さない():
    design = design_ledger()
    raise_finding(design, design=DesignVersion(1))
    with pytest.raises(Rejected, match="設計の台帳の指摘は移さない"):
        carry(design, "D1")
    with pytest.raises(Rejected, match="設計の台帳へは指摘を移さない"):
        raise_finding(
            design,
            design=DesignVersion(1),
            carried_from=FindingOrigin(TASK_STREAM, FindingId("R1")),
        )


# --- ステージの結果をまとめて受ける ---

FOUND = ReportedFinding(Rating.MUST_FIX, "境界の値で落ちる", Location("src/a.py:3"))
EXPECT = execution(StageKind.EXPECT, round=1)
DESIGN_REVIEW = execution(StageKind.DESIGN_REVIEW, PLANNING, round=1)


def test_見る役とExpectの指摘をまとめて立て受けたことを知らせる():
    ledger = ReviewLedger(TASK_STREAM)
    events = record(ledger, REVIEW, FOUND, ReportedFinding(Rating.NIT, "名前"))
    assert events == [
        FindingRaised(FindingId("R1"), Rating.MUST_FIX, FOUND.body, FOUND.location, source=REVIEW),
        FindingRaised(FindingId("R2"), Rating.NIT, "名前", source=REVIEW),
        ResultReceived(REVIEW),
    ]
    # Expect の defects も、Expect を出どころとする指摘になり、Judge が判定する
    assert record(ledger, EXPECT, FOUND)[0] == FindingRaised(
        FindingId("R3"), Rating.MUST_FIX, FOUND.body, FOUND.location, source=EXPECT
    )
    assert JudgeCapability.judge_of(TASK_STREAM, FindingId("R3")) is StageKind.JUDGE
    # 指摘が無くても、受けたことは知らせる
    assert record(ledger, REVIEW) == [ResultReceived(REVIEW)]


def test_指摘の中身の問題は拒まずに受けられないと返し何も立てない():
    ledger = ReviewLedger(TASK_STREAM)
    assert record(ledger, REVIEW, ReportedFinding(Rating.NIT, " ")) == [
        ResultRefused(REVIEW, "本文が空の指摘がある")
    ]
    assert ledger.findings == {}
    design = design_ledger(3)
    refused = record(design, DESIGN_REVIEW, FOUND, design=DesignVersion(2))
    assert refused == [ResultRefused(DESIGN_REVIEW, "見た版 2 は今の提案（版 3 から）より前")]


def test_指摘を立てられるのは持ち主のタスクの指摘を挙げるステージだけ():
    for by in (JUDGE, FIX, execution(StageKind.REVIEW, TaskId("task3"))):
        with pytest.raises(Rejected, match="指摘を挙げるステージの実行だけ"):
            record(ReviewLedger(TASK_STREAM), by, FOUND)
    with pytest.raises(Rejected, match="見た提案の版を持つ"):
        record(design_ledger(), DESIGN_REVIEW, FOUND)


def test_判定をまとめて当てて締め停滞の分類を写す():
    ledger = ReviewLedger(TASK_STREAM)
    record(ledger, REVIEW, FOUND, FOUND)
    for _ in range(STALL_AFTER_FIXES):
        count_fix(ledger)
    closed = FindingVerdict(FindingId("R2"), FindingStatus.CLOSED, "直っている")
    events = evaluate(ledger, JUDGE, closed, stall_cause=StallCause.TESTS)
    assert events == [
        FindingClosed(FindingId("R2"), "直っている", JUDGE),
        FindingStalled(FindingId("R1"), STALL_AFTER_FIXES),
        FindingsEvaluated(
            JUDGE,
            open_findings=(
                FindingSummary(FindingId("R1"), Rating.MUST_FIX, FOUND.body, FOUND.location),
            ),
            stalled=(FindingId("R1"),),
            stall_cause=StallCause.TESTS,
        ),
    ]
    # 同じ判定の中で閉じてから開き直してもよい（判定の順に当てる）
    reopen = FindingVerdict(FindingId("R2"), FindingStatus.OPEN, "壊れていた")
    assert open_ids(evaluate(ledger, execution(StageKind.JUDGE), reopen)) == ["R1", "R2"]


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        (FindingVerdict(FindingId("R9"), FindingStatus.CLOSED, "x"), "R9 の指摘が無い"),
        (FindingVerdict(FindingId("R1"), FindingStatus.CLOSED, " "), "コメントが無い"),
        # 同じ判定の前の件で閉じた後なので、closed から動かすことになる
        (
            FindingVerdict(FindingId("R1"), FindingStatus.REJECTED, "x"),
            "closed から rejected へは動かせない",
        ),
        (FindingVerdict(VERIFY, FindingStatus.CLOSED, "x"), "Judge ではない"),
    ],
)
def test_判定の中身の問題は拒まずに受けられないと返しどれも当てない(
    verdict: FindingVerdict, reason: str
):
    ledger = ReviewLedger(TASK_STREAM)
    record(ledger, REVIEW, FOUND)
    gate(ledger, fails(GateItem.VERIFY))
    good = FindingVerdict(FindingId("R1"), FindingStatus.CLOSED, "直っている")
    (event,) = evaluate(ledger, JUDGE, good, verdict)
    assert isinstance(event, ResultRefused) and reason in event.reason
    assert status(ledger, "R1") is FindingStatus.OPEN


def test_走らせ直した判定が当て済みの判定を返しても受けて締め直す():
    ledger = ReviewLedger(TASK_STREAM)
    record(ledger, REVIEW, FOUND)
    good = FindingVerdict(FindingId("R1"), FindingStatus.CLOSED, "直っている")
    evaluate(ledger, JUDGE, good)
    # 受け取る側が受けずに、同じ判定のステージを走らせ直した
    again = execution(StageKind.JUDGE)
    events = evaluate(ledger, again, good)
    assert [type(e) for e in events] == [FindingsEvaluated]
    assert evaluated(events).execution == again
    # 当て済みでも、コメントの無い判定は受けない
    blank = FindingVerdict(FindingId("R1"), FindingStatus.CLOSED, " ")
    (refused,) = evaluate(ledger, execution(StageKind.JUDGE), blank)
    assert isinstance(refused, ResultRefused) and "コメントが無い" in refused.reason


def test_当て済みかは同じ判定の前の件を当てた後の状態と比べる():
    ledger = ReviewLedger(TASK_STREAM)
    record(ledger, REVIEW, FOUND)
    good = FindingVerdict(FindingId("R1"), FindingStatus.CLOSED, "直っている")
    events = evaluate(ledger, JUDGE, good, good)
    assert [type(e) for e in events] == [FindingClosed, FindingsEvaluated]


def test_設計の台帳の判定は見た版と設計の分類を写す():
    ledger = design_ledger(2)
    reverted = DesignJudgement(DesignCause.REVERTED, DesignVersion(1))
    events = evaluate(ledger, DESIGN_JUDGE, design=DesignVersion(2), design_cause=reverted)
    assert evaluated(events) == FindingsEvaluated(
        DESIGN_JUDGE, open_findings=(), design=DesignVersion(2), design_cause=reverted
    )
    stale = evaluate(ledger, DESIGN_JUDGE, design=DesignVersion(1))
    assert stale == [ResultRefused(DESIGN_JUDGE, "見た版 1 は今の提案（版 2 から）より前")]
    with pytest.raises(Rejected, match="停滞の分類を持つのはタスクの台帳だけ"):
        evaluate(ledger, DESIGN_JUDGE, stall_cause=StallCause.SCOPE)
    with pytest.raises(Rejected, match="設計の台帳の判定だけ"):
        evaluate(ReviewLedger(TASK_STREAM), JUDGE, design=DesignVersion(1))


def test_計画タスクの指摘と判定は設計の台帳に入る():
    assert JudgeCapability.ledger_of(PLANNING) == DESIGN_STREAM
    assert JudgeCapability.ledger_of(TASK) == TASK_STREAM


# --- 再生 ---


def test_イベントの列を再生すると同じ状態になる():
    live = ReviewLedger(TASK_STREAM)
    history: list[tuple[Event, CommandId]] = []

    def step(command: Command) -> None:
        history.extend((event, command.command_id) for event in drive(live, command))

    step(
        RaiseFinding(
            command_id=cid(), issuer=POLICY, ledger=TASK_STREAM, rating=Rating.NIT, body="a"
        )
    )
    step(
        RecordGateResult(
            command_id=cid(),
            issuer=POLICY,
            ledger=TASK_STREAM,
            execution=GATE,
            failed=(fails(GateItem.VERIFY),),
        )
    )
    for _ in range(STALL_AFTER_FIXES):
        step(
            CountFix(
                command_id=cid(),
                issuer=POLICY,
                ledger=TASK_STREAM,
                execution=execution(StageKind.FIX),
            )
        )
    step(
        JudgeFinding(
            command_id=cid(),
            issuer=POLICY,
            ledger=TASK_STREAM,
            finding=FindingId("R1"),
            to=FindingStatus.REJECTED,
            comment="nit",
            execution=JUDGE,
        )
    )
    step(
        RecordGateResult(
            command_id=cid(),
            issuer=POLICY,
            ledger=TASK_STREAM,
            execution=execution(StageKind.GATE),
            failed=(fails(GateItem.VERIFY),),
        )
    )
    replayed = ReviewLedger.replay(TASK_STREAM, history)
    assert replayed.findings == live.findings
    assert replayed.findings[VERIFY].stalled_at == STALL_AFTER_FIXES
    # 番号の続きも再生から決まる
    assert raise_finding(replayed)[0] == FindingRaised(
        FindingId("R2"), Rating.MUST_FIX, "境界の値で落ちる"
    )


# --- 指摘の宛先 ---

TESTS_FOUND = ReportedFinding(
    Rating.MUST_FIX, "テストが無い", Location("src/a.py"), FindingTarget.TESTS
)


def retarget(finding: str, target: FindingTarget | None, to: FindingStatus = FindingStatus.OPEN):
    return FindingVerdict(FindingId(finding), to, "実装の分岐が抜けている", target)


def test_見る役が挙げた指摘は宛先を持って立つ():
    ledger = ReviewLedger(TASK_STREAM)
    events = record(ledger, REVIEW, TESTS_FOUND, ReportedFinding(Rating.NIT, "名前"), FOUND)
    raised = [e for e in events if isinstance(e, FindingRaised)]
    assert [e.target for e in raised] == [
        FindingTarget.TESTS,
        FindingTarget.IMPL,
        FindingTarget.IMPL,
    ]


def test_Gateの項目の指摘の宛先はimplになる():
    ledger = ReviewLedger(TASK_STREAM)
    events = gate(ledger, fails(GateItem.VERIFY), by=GATE)
    raised = [e for e in events if isinstance(e, FindingRaised)]
    assert [e.finding for e in raised] == [VERIFY]
    assert raised[0].target is FindingTarget.IMPL
    assert evaluated(events).open_findings[0].target is FindingTarget.IMPL


def test_FindingsEvaluatedの未解決は指摘の宛先を載せる():
    ledger = ReviewLedger(TASK_STREAM)
    record(ledger, REVIEW, TESTS_FOUND, FOUND)
    events = evaluate(ledger, JUDGE)
    assert [(f.finding, f.target) for f in evaluated(events).open_findings] == [
        (FindingId("R1"), FindingTarget.TESTS),
        (FindingId("R2"), FindingTarget.IMPL),
    ]


def test_Judgeがopenに残す指摘の宛先を書き換えると同じ締めの未解決に反映される():
    ledger = ReviewLedger(TASK_STREAM)
    record(ledger, REVIEW, TESTS_FOUND)
    events = evaluate(ledger, JUDGE, retarget("R1", FindingTarget.IMPL))
    assert FindingRetargeted(FindingId("R1"), FindingTarget.IMPL, JUDGE) in events
    assert [f.target for f in evaluated(events).open_findings] == [FindingTarget.IMPL]
    # 書き換えは台帳に残り、次の締めでも新しい宛先のまま
    again = evaluate(ledger, execution(StageKind.JUDGE))
    assert [f.target for f in evaluated(again).open_findings] == [FindingTarget.IMPL]


@pytest.mark.parametrize("target", [None, FindingTarget.TESTS], ids=["null", "同じ値"])
def test_宛先がnullか今と同じなら書き換えず何も出さない(target: FindingTarget | None):
    ledger = ReviewLedger(TASK_STREAM)
    record(ledger, REVIEW, TESTS_FOUND)
    events = evaluate(ledger, JUDGE, retarget("R1", target))
    assert not [e for e in events if isinstance(e, FindingRetargeted)]
    assert [f.target for f in evaluated(events).open_findings] == [FindingTarget.TESTS]


@pytest.mark.parametrize("to", [FindingStatus.CLOSED, FindingStatus.REJECTED])
def test_closedとrejectedへの判定に宛先を書いても書き換えない(to: FindingStatus):
    ledger = ReviewLedger(TASK_STREAM)
    record(ledger, REVIEW, TESTS_FOUND, FOUND)
    events = evaluate(ledger, JUDGE, retarget("R1", FindingTarget.IMPL, to))
    assert not [e for e in events if isinstance(e, FindingRetargeted)]
    assert open_ids(events) == ["R2"]


def test_移した指摘は移す元の宛先のまま移した先に立つ():
    ledger = ReviewLedger(TASK_STREAM)
    record(ledger, REVIEW, TESTS_FOUND)
    (carried,) = carry(ledger, "R1")
    assert isinstance(carried, FindingCarried)
    assert carried.target is FindingTarget.TESTS
    origin = FindingOrigin(TASK_STREAM, FindingId("R1"))
    moved = ReviewLedger(StreamId.review(TaskId("task3")))
    (raised,) = raise_finding(moved, carried_from=origin, finding_target=carried.target)
    assert isinstance(raised, FindingRaised) and raised.target is FindingTarget.TESTS
    assert [
        f.target
        for f in evaluated(
            evaluate(moved, execution(StageKind.JUDGE, TaskId("task3")))
        ).open_findings
    ] == [FindingTarget.TESTS]


def test_宛先を指定せずに立てた指摘はimplになる():
    ledger = ReviewLedger(TASK_STREAM)
    (raised,) = raise_finding(ledger)
    assert isinstance(raised, FindingRaised) and raised.target is FindingTarget.IMPL
