"""Design（`domain/aggregates/design.py`）。コマンドとイベントの列だけで確かめる。"""

from __future__ import annotations

import itertools

import pytest

from autodev.domain.aggregates.base import Rejected
from autodev.domain.aggregates.design import Design
from autodev.domain.commands.base import Command
from autodev.domain.commands.design import (
    DiscardProposal,
    MarkAmbiguous,
    MarkReverted,
    ProposeDesign,
    ResumeDesign,
    ReviseDesign,
    SettleDesign,
)
from autodev.domain.events.base import Event
from autodev.domain.events.design import (
    DesignAmbiguous,
    DesignProposalAbandoned,
    DesignProposed,
    DesignReverted,
    DesignRevised,
    DesignRevisionStarted,
    DesignRoundsExhausted,
    DesignRoundsReset,
    DesignSettled,
)
from autodev.domain.events.review_ledger import ResultReceived, ResultRefused
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.artifact_ref import ArtifactRef
from autodev.domain.value_objects.command_id import CommandId
from autodev.domain.value_objects.design_version import DesignVersion
from autodev.domain.value_objects.escalation_kind import EscalationKind
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.execution_id import ExecutionId
from autodev.domain.value_objects.finding_id import FindingId
from autodev.domain.value_objects.finding_summary import FindingSummary
from autodev.domain.value_objects.issuer import Issuer
from autodev.domain.value_objects.limits import MAX_DESIGN_ROUNDS
from autodev.domain.value_objects.planned_task import PlannedTask
from autodev.domain.value_objects.proposal import Proposal
from autodev.domain.value_objects.rating import Rating
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.stream_id import StreamId
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_spec import TaskSpec

STREAM = StreamId.design()
PLANNING = TaskId.planning()
POLICY = Issuer.policy("design-loop", EventId("task/planning#4"))
_ids = itertools.count(1)

EXHAUSTED = EscalationKind.DESIGN_ROUNDS_EXHAUSTED
REVERTED = EscalationKind.DESIGN_REVERTED
AMBIGUOUS = EscalationKind.DESIGN_AMBIGUOUS


def execution(stage: StageKind, round: int = 0, task: TaskId = PLANNING) -> ExecutionId:
    return ExecutionId(task, stage, round, 1)


PLAN = execution(StageKind.PLAN)
REPLAN = execution(StageKind.REPLAN)
JUDGE = execution(StageKind.DESIGN_JUDGE, 1)


def proposal(version: int, decisions: tuple[str, ...] = (), deferrals: tuple[str, ...] = ()):
    return Proposal(
        DesignVersion(version),
        (PlannedTask(TaskId("task1"), TaskSpec("キャッシュを足す")),),
        decisions=decisions,
        deferrals=deferrals,
    )


def cid() -> CommandId:
    return CommandId(f"c{next(_ids)}")


def drive(design: Design, command: Command) -> list[Event]:
    events = design.handle(command)
    for event in events:
        design.apply(event, command.command_id)
    return events


def refused(events: list[Event]) -> str:
    """受けられなかった（中身の問題は拒否ではなくイベントで返る）。理由を返す。"""
    (event,) = events
    assert isinstance(event, ResultRefused)
    return event.reason


BRIEF = ArtifactRef(ArtifactKind.BRIEF, "brief.md")
CODEMAP = ArtifactRef(ArtifactKind.CODEMAP, "codemap.md")


def propose(
    design: Design,
    version: int,
    by: ExecutionId = PLAN,
    artifacts: tuple[ArtifactRef, ...] = (),
    **kwargs,
) -> list[Event]:
    return drive(
        design,
        ProposeDesign(
            command_id=cid(),
            issuer=POLICY,
            proposal=proposal(version, **kwargs),
            execution=by,
            artifacts=artifacts,
        ),
    )


def revise(design: Design, by: ExecutionId = JUDGE) -> list[Event]:
    return drive(design, ReviseDesign(command_id=cid(), issuer=POLICY, execution=by))


def revised(design: Design, version: int) -> list[Event]:
    return propose(design, version, by=execution(StageKind.REVISE, design.round))


def current(design: Design) -> DesignVersion:
    assert design.proposal is not None
    return design.proposal.design


def settle(
    design: Design,
    *open_findings: FindingSummary,
    by: ExecutionId = JUDGE,
    version: int | None = None,
) -> list[Event]:
    judged = DesignVersion(version) if version is not None else current(design)
    return drive(
        design,
        SettleDesign(
            command_id=cid(),
            issuer=POLICY,
            execution=by,
            design=judged,
            open_findings=open_findings,
        ),
    )


def mark_reverted(design: Design, to: int, by: ExecutionId = JUDGE) -> list[Event]:
    return drive(
        design,
        MarkReverted(command_id=cid(), issuer=POLICY, to_version=DesignVersion(to), execution=by),
    )


def mark_ambiguous(design: Design, by: ExecutionId = JUDGE) -> list[Event]:
    return drive(design, MarkAmbiguous(command_id=cid(), issuer=POLICY, execution=by))


def resume(design: Design, kind: EscalationKind, answer: str = "B の形でよい") -> list[Event]:
    return drive(design, ResumeDesign(command_id=cid(), issuer=POLICY, kind=kind, answer=answer))


def discard(design: Design) -> list[Event]:
    return drive(design, DiscardProposal(command_id=cid(), issuer=POLICY, reason="再計画"))


def must_fix(number: int) -> FindingSummary:
    return FindingSummary(FindingId.design(number), Rating.MUST_FIX, "依存が輪になる")


def nit(number: int) -> FindingSummary:
    return FindingSummary(FindingId.design(number), Rating.NIT, "見出しの言い方")


def use_all_rounds(design: Design) -> None:
    """提案から、MAX_DESIGN_ROUNDS ラウンド目の判定までを回す。"""
    for version in range(2, MAX_DESIGN_ROUNDS + 1):
        revise(design)
        revised(design, version)


def revised_twice() -> Design:
    design = Design(STREAM)
    propose(design, 1)
    revise(design)
    revised(design, 2)
    return design


# --- 提案 ---


def test_計画の提案はラウンド1から始まり受けたことを知らせる():
    design = Design(STREAM)
    assert propose(design, 1, artifacts=(BRIEF, CODEMAP)) == [
        DesignProposed(proposal(1), (BRIEF, CODEMAP)),
        ResultReceived(PLAN),
    ]
    assert (design.round, design.versions) == (1, [DesignVersion(1)])


def test_確定していない提案があれば新しい提案を受けない():
    design = Design(STREAM)
    propose(design, 1)
    assert "確定していない提案がすでにある" in refused(propose(design, 2))
    assert design.versions == [DesignVersion(1)]


def test_PlanとReplanは設計が確定したかで使い分ける():
    design = Design(STREAM)
    assert "Plan から" in refused(propose(design, 1, by=REPLAN))
    propose(design, 1)
    settle(design)
    assert "Replan から" in refused(propose(design, 2))
    assert propose(design, 2, by=REPLAN) == [DesignProposed(proposal(2)), ResultReceived(REPLAN)]


@pytest.mark.parametrize("stage", [StageKind.DESIGN_REVIEW, StageKind.PREPARE])
def test_提案を返さないステージの結果は受けない(stage: StageKind):
    with pytest.raises(Rejected, match="設計の提案を返すステージではない"):
        propose(Design(STREAM), 1, by=execution(stage))


@pytest.mark.parametrize("stage", [StageKind.PLAN, StageKind.REVISE, StageKind.IMPL])
def test_提案を出せるのは計画タスクのステージだけ(stage: StageKind):
    with pytest.raises(Rejected, match="計画タスクのステージだけ"):
        propose(Design(STREAM), 1, by=execution(stage, task=TaskId("task2")))


def test_版の番号は使った番号と重ねず増えるだけ():
    design = Design(STREAM)
    propose(design, 3)
    revise(design)
    assert "使った番号以下" in refused(revised(design, 3))
    assert "使った番号以下" in refused(revised(design, 2))
    revised(design, 4)
    discard(design)
    assert "版は消さない" in refused(propose(design, 4))
    assert design.versions == [DesignVersion(3), DesignVersion(4)]


# --- Revise とラウンドの上限 ---


def test_ReviseDesignはReviseの前にラウンドを1つ使い結果はProposeDesignで入る():
    design = Design(STREAM)
    propose(design, 1)
    # 直すと決めた判定を載せる（計画タスクが今のラウンドの判定かを確かめる）
    assert revise(design) == [DesignRevisionStarted(2, execution=JUDGE)]
    revise_run = execution(StageKind.REVISE, 2)
    assert revised(design, 2) == [DesignRevised(proposal(2)), ResultReceived(revise_run)]
    assert (design.round, design.revising, design.proposal) == (2, False, proposal(2))


def test_Reviseを始めていなければ直した版を受けない():
    design = Design(STREAM)
    propose(design, 1)
    # 再計画で提案を捨てた後に、走っていた Revise の結果が届くことがある
    assert "Revise を始めていない" in refused(revised(design, 2))


def test_Reviseの結果を待つ間は次のReviseも判定もしない():
    design = Design(STREAM)
    propose(design, 1)
    revise(design)
    for attempt in (revise, settle, mark_ambiguous):
        assert "Revise の結果を待っている" in refused(attempt(design))


def test_提案が無ければReviseしない():
    assert "確定していない提案が無い" in refused(revise(Design(STREAM)))


def test_ReviseDesignは計画タスクのDesignJudgeの判定からだけ受ける():
    design = Design(STREAM)
    propose(design, 1)
    with pytest.raises(Rejected, match="計画タスクの DesignJudge だけ"):
        revise(design, by=execution(StageKind.JUDGE, 1, TaskId("task2")))


def test_上限まで回ってもmust_fixが残ったら上げる():
    design = Design(STREAM)
    propose(design, 1)
    use_all_rounds(design)
    assert design.round == MAX_DESIGN_ROUNDS
    assert revise(design) == [DesignRoundsExhausted(MAX_DESIGN_ROUNDS, JUDGE)]
    assert design.awaiting is EXHAUSTED


def test_回答が来たら数を0に戻して回答を持ってReviseから続けもう一度上限まで回せる():
    design = Design(STREAM)
    propose(design, 1)
    use_all_rounds(design)
    revise(design)
    assert resume(design, EXHAUSTED) == [
        DesignRoundsReset("B の形でよい"),
        DesignRevisionStarted(1, answer="B の形でよい", execution=JUDGE),
    ]
    revised(design, MAX_DESIGN_ROUNDS + 1)
    for version in range(MAX_DESIGN_ROUNDS + 2, 2 * MAX_DESIGN_ROUNDS + 1):
        assert isinstance(revise(design)[0], DesignRevisionStarted)
        revised(design, version)
    assert revise(design) == [DesignRoundsExhausted(MAX_DESIGN_ROUNDS, JUDGE)]


# --- 回答待ち（ラウンドの上限・前の版に戻った・曖昧）は 1 つの規則 ---


def wait_on(kind: EscalationKind) -> Design:
    design = revised_twice()
    if kind is EXHAUSTED:
        design = Design(STREAM)
        propose(design, 1)
        use_all_rounds(design)
        revise(design)
    elif kind is REVERTED:
        mark_reverted(design, 1)
    else:
        mark_ambiguous(design)
    assert design.awaiting is kind
    return design


@pytest.mark.parametrize("kind", [EXHAUSTED, REVERTED, AMBIGUOUS], ids=lambda k: k.value)
def test_回答を待つ間はReviseも確定も判定もしない(kind: EscalationKind):
    design = wait_on(kind)
    for attempt in (revise, settle, mark_ambiguous, lambda d: mark_reverted(d, 1)):
        assert "回答を待っている" in refused(attempt(design))


@pytest.mark.parametrize("kind", [EXHAUSTED, REVERTED, AMBIGUOUS], ids=lambda k: k.value)
def test_回答を持ったResumeDesignでだけ抜けReviseに回答を渡す(kind: EscalationKind):
    design = wait_on(kind)
    events = resume(design, kind, "A の形に戻してよい")
    # 回答を待つことになった判定を、回答の後の Revise にも載せる
    assert events[-1] == DesignRevisionStarted(1, answer="A の形に戻してよい", execution=JUDGE)
    assert (design.awaiting, design.revising, design.round) == (None, True, 1)


def test_待っている原因と違う回答では抜けない():
    design = wait_on(REVERTED)
    with pytest.raises(Rejected, match="待っているのは design-reverted への回答"):
        resume(design, AMBIGUOUS)
    with pytest.raises(Rejected, match="回答が空"):
        resume(design, REVERTED, " ")


def test_待っていなければ回答で続けない():
    design = Design(STREAM)
    propose(design, 1)
    with pytest.raises(Rejected, match="回答を待っていない"):
        resume(design, EXHAUSTED)


def test_前の版に戻ったと曖昧はDesignJudgeの判定から受ける():
    design = revised_twice()
    assert mark_reverted(design, 1) == [DesignReverted(DesignVersion(1), JUDGE)]
    design = revised_twice()
    assert mark_ambiguous(design) == [DesignAmbiguous(JUDGE)]
    for by in (
        execution(StageKind.DESIGN_REVIEW, 1),
        execution(StageKind.JUDGE, 1, TaskId("task2")),
    ):
        with pytest.raises(Rejected, match="計画タスクの DesignJudge だけ"):
            mark_reverted(revised_twice(), 1, by)
        with pytest.raises(Rejected, match="計画タスクの DesignJudge だけ"):
            mark_ambiguous(revised_twice(), by)


@pytest.mark.parametrize("version", [2, 5])
def test_戻った先は今の提案より前の版(version: int):
    # 戻った先の版は DesignJudge の結果から来るので、中身の問題として受けない
    assert "今の提案より前の版" in refused(mark_reverted(revised_twice(), version))


def test_提案が無ければ判定を受けない():
    assert "確定していない提案が無い" in refused(mark_ambiguous(Design(STREAM)))


# --- 確定 ---


def test_must_fixが0件なら確定しmust_fix以外を設計ファイルの末尾に回す():
    design = Design(STREAM)
    propose(design, 1, artifacts=(BRIEF, CODEMAP))
    # ラン共通の成果物（提案と一緒に受けた brief・codemap と、確定した design の版）を載せる
    shared = (BRIEF, CODEMAP, ArtifactRef(ArtifactKind.DESIGN, "1"))
    assert settle(design, nit(2), nit(3)) == [
        DesignSettled(proposal(1), JUDGE, appendix=(nit(2), nit(3)), artifacts=shared)
    ]
    assert (design.proposal, design.settled, design.round) == (None, proposal(1), 0)


def test_must_fixが残っていれば確定しない():
    design = Design(STREAM)
    propose(design, 1)
    with pytest.raises(Rejected, match="must-fix が残っている: D1"):
        settle(design, must_fix(1), nit(2))
    assert design.settled is None


def test_判定した版が今の提案の版でなければ確定しない():
    design = revised_twice()
    assert "判定した版 1 は今の提案の版 2 ではない" in refused(settle(design, version=1))
    assert isinstance(settle(design, version=2)[0], DesignSettled)


def test_確定を決められるのは設計のジャッジの判定だけ():
    design = Design(STREAM)
    propose(design, 1)
    with pytest.raises(Rejected, match="計画タスクの DesignJudge だけ"):
        settle(design, by=execution(StageKind.DESIGN_REVIEW, 1))
    with pytest.raises(Rejected, match="計画タスクの DesignJudge だけ"):
        settle(design, by=execution(StageKind.JUDGE, 1, TaskId("task2")))


def test_提案が無ければ確定しない():
    assert "確定していない提案が無い" in refused(settle(Design(STREAM), version=1))


def test_確定した提案のdecisionsとdeferralsを重ねずに集める():
    design = Design(STREAM)
    propose(design, 1, decisions=("層ごとに割る",), deferrals=("CLI は後",))
    settle(design)
    propose(design, 2, by=REPLAN, decisions=("層ごとに割る", "task3 を足す"))
    settle(design)
    assert design.decisions == ("層ごとに割る", "task3 を足す")
    assert design.deferrals == ("CLI は後",)


# --- 捨てる ---


def test_確定していない提案を捨てるとラウンドも待ちも消える():
    design = Design(STREAM)
    propose(design, 1)
    use_all_rounds(design)
    revise(design)
    assert discard(design) == [DesignProposalAbandoned(DesignVersion(MAX_DESIGN_ROUNDS), "再計画")]
    assert (design.proposal, design.round, design.awaiting) == (None, 0, None)
    assert propose(design, MAX_DESIGN_ROUNDS + 1)


def test_捨てる提案が無ければ何も出さない():
    design = Design(STREAM)
    assert discard(design) == []
    propose(design, 1)
    settle(design)
    assert discard(design) == []
    assert design.settled == proposal(1)


# --- 再生 ---


def test_イベントの列を再生すると同じ状態になる():
    live = Design(STREAM)
    history: list[tuple[Event, CommandId]] = []

    def step(command: Command) -> None:
        history.extend((event, command.command_id) for event in drive(live, command))

    step(ProposeDesign(command_id=cid(), issuer=POLICY, proposal=proposal(1), execution=PLAN))
    step(MarkAmbiguous(command_id=cid(), issuer=POLICY, execution=JUDGE))
    step(ResumeDesign(command_id=cid(), issuer=POLICY, kind=AMBIGUOUS, answer="A"))
    step(
        ProposeDesign(
            command_id=cid(),
            issuer=POLICY,
            proposal=proposal(2, decisions=("x",)),
            execution=execution(StageKind.REVISE, 1),
        )
    )
    step(
        SettleDesign(
            command_id=cid(),
            issuer=POLICY,
            execution=JUDGE,
            design=DesignVersion(2),
            open_findings=(nit(1),),
        )
    )
    replayed = Design.replay(STREAM, history)
    assert vars(replayed) == vars(live)
