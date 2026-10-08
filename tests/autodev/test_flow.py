"""フローの検査と、フロー・cursor の値（`domain/flow/`・`domain/stages/`）。"""

from __future__ import annotations

import pytest

from autodev.domain.flow.flow import Cursor, Flow, FlowStep, Reviewers
from autodev.domain.flow.standard import git_job_flow, planning_flow
from autodev.domain.flow.validator import FlowValidator
from autodev.domain.stages.catalog import STAGE_SPECS
from autodev.domain.stages.kinds import SessionScope, StageMode
from autodev.domain.value_objects.artifact_kind import ArtifactKind
from autodev.domain.value_objects.base import InvalidValue
from autodev.domain.value_objects.branch_name import BranchName
from autodev.domain.value_objects.git_job import GitJob
from autodev.domain.value_objects.git_job_kind import GitJobKind
from autodev.domain.value_objects.stage_kind import StageKind
from autodev.domain.value_objects.task_id import TaskId
from autodev.domain.value_objects.task_kind import TaskKind

S = StageKind
A = ArtifactKind
IMPL = TaskKind.IMPLEMENTATION
BOTH = Reviewers((S.REVIEW, S.ADVERSARIAL_REVIEW), later=(S.REVIEW,))


def step(stage: StageKind, **kw) -> FlowStep:
    return FlowStep(stage, **kw)


def full_flow(reviewers: Reviewers = BOTH) -> list[FlowStep]:
    return [
        step(S.TEST_GEN),
        step(S.CONFIRM_RED),
        step(S.IMPL),
        step(S.REVIEW_LOOP, reviewers=reviewers),
        step(S.GATE),
        step(S.WRITE_PR_BODY),
    ]


def check(steps, kind=IMPL, available=(A.DESIGN,)) -> tuple[str, ...]:
    return FlowValidator.check(steps, kind, available)


def test_設計書のフローの例は通る():
    assert check(full_flow()) == ()
    docs_only = [
        step(S.IMPL),
        step(S.REVIEW_LOOP, reviewers=Reviewers((S.REVIEW,))),
        step(S.GATE),
        step(S.WRITE_PR_BODY),
    ]
    assert check(docs_only) == ()


def test_計画タスクのフローは初回も再計画も通る():
    planning = TaskKind.PLANNING
    assert check([step(S.PREPARE), step(S.PLAN), step(S.DESIGN_LOOP)], planning, ()) == ()
    replan = [step(S.REPLAN), step(S.DESIGN_LOOP)]
    assert check(replan, planning, (A.BRIEF, A.CODEMAP, A.DESIGN)) == ()
    # 設計がまだ一度も確定していなければ Replan は組めない（Prepare → Plan からやり直す）
    assert check(replan, planning, (A.BRIEF,)) != ()


def test_計画タスクの統括の並びは設計が確定したかで決まる():
    assert planning_flow(settled_before=False) == (
        step(S.PREPARE),
        step(S.PLAN),
        step(S.DESIGN_LOOP),
    )
    assert planning_flow(settled_before=True) == (step(S.REPLAN), step(S.DESIGN_LOOP))


STACKING = (
    S.REBASE,
    S.RESOLVE_CONFLICT,
    S.CHECK_UNION,
    S.INTEGRATION_CHECK,
    S.PUSH,
    S.CREATE_PR,
    S.STACK_LINK,
    S.REFRESH_OVERVIEW,
)


def job(kind: GitJobKind, **kw) -> GitJob:
    return GitJob(1, kind, **kw)


def test_git管理タスクの統括は仕事の種類から決まった並びを組む():
    stack = job(GitJobKind.STACK, task=TaskId("task1"))
    assert git_job_flow(stack) == tuple(step(s) for s in STACKING)
    # 積み直す仕事は、新しい名前のブランチを切り直してから rebase する
    restack = job(GitJobKind.STACK, task=TaskId("task1"), previous=BranchName("stack/r--task-1"))
    assert git_job_flow(restack) == (step(S.CUT_BRANCH), *(step(s) for s in STACKING))
    assert git_job_flow(job(GitJobKind.FINISH, ready_overview=True))[-1] == step(S.READY_OVERVIEW)
    assert step(S.READY_OVERVIEW) not in git_job_flow(job(GitJobKind.FINISH))
    assert git_job_flow(job(GitJobKind.OPEN_OVERVIEW)) == (
        step(S.WRITE_OVERVIEW),
        step(S.CREATE_OVERVIEW_PR),
        step(S.REFRESH_OVERVIEW),
    )
    assert git_job_flow(job(GitJobKind.DISCARD))[2] == step(S.RELINK)


def test_git管理タスクのフローは仕事を1つ持ちその並びだけを受ける():
    stack = job(GitJobKind.STACK, task=TaskId("task1"))
    git = TaskKind.GIT
    assert FlowValidator.check(git_job_flow(stack), git, (), stack) == ()
    assert FlowValidator.check(git_job_flow(stack), git, ()) == (
        "git 管理タスクのフローは、取り出した仕事を 1 つ持つ",
    )
    assert FlowValidator.check([step(S.PUSH)], git, (), stack) == ("stack の仕事の並びではない",)
    assert FlowValidator.check(full_flow(), IMPL, (A.DESIGN,), stack) == (
        "仕事を持つのは git 管理タスクのフローだけ",
    )


def test_統括からの言葉は空なら書かない():
    assert (
        check([*full_flow()[:2], step(S.IMPL, instruction="R2 の方針で直す"), *full_flow()[3:]])
        == ()
    )
    assert check([*full_flow()[:2], step(S.IMPL, instruction=" "), *full_flow()[3:]]) == (
        "Impl の instruction が空。渡す言葉が無ければ書かない",
    )


def test_空のフローを拒む():
    assert check([]) == ("フローが空",)


def test_要る成果物がそれより前に無ければ拒む():
    assert check(full_flow(), available=()) == (
        "TestGen に要る design が、それより前に作られていない",
        "Impl に要る design が、それより前に作られていない",
    )
    assert check([step(S.GATE), step(S.WRITE_PR_BODY)]) == (
        "Gate に要る reviewed が、それより前に作られていない",
        "Gate は落ちたときに ReviewLoop へ戻るので、ReviewLoop をそれより前に置く",
    )


def test_タスクがすでに持つ成果物は要るものに数える():
    rest = [step(S.REVIEW_LOOP, reviewers=BOTH), step(S.GATE), step(S.WRITE_PR_BODY)]
    assert check(rest, available=(A.DESIGN, A.IMPL)) == ()


def test_実装タスクのフローは終わりにgatedとpr_bodyを作る():
    assert check(full_flow()[:-1]) == ("フローの終わりまでに pr-body が作られない",)
    # 前のフローで作ったものが残っていても、このフローで作り直す
    assert check(full_flow()[:-1], available=(A.DESIGN, A.PR_BODY)) != ()


def test_テスト作成は実装より前に置く():
    flow = [step(S.IMPL), step(S.TEST_GEN), *full_flow()[3:]]
    assert check(flow) == ("TestGen は Impl より前に置く",)


def test_中のステージはフローに直に置けない():
    flow = [step(S.IMPL), step(S.REVIEW), *full_flow()[3:]]
    assert check(flow) == ("Review は ReviewLoop の中のステージで、フローに直に置けない",)


def test_タスクの種類に合わないステージを拒む():
    assert "Gate は planning のタスクに置けない" in check([step(S.GATE)], TaskKind.PLANNING, ())
    assert "CutBranch は implementation のタスクに置けない" in check([step(S.CUT_BRANCH)])


def test_ReviewLoopにはreviewersが要る():
    flow = full_flow()
    flow[3] = step(S.REVIEW_LOOP)
    assert check(flow) == ("ReviewLoop に reviewers が無い",)


@pytest.mark.parametrize(
    ("reviewers", "reason"),
    [
        (Reviewers(()), "reviewers の first が空"),
        (Reviewers((S.REVIEW,), later=()), "reviewers の later が空"),
        (
            Reviewers((S.REVIEW, S.JUDGE)),
            "reviewers の first に書けるのは AdversarialReview, Review だけ（Judge）",
        ),
        (Reviewers((S.REVIEW, S.REVIEW)), "reviewers の first に同じステージが 2 つある"),
        (
            Reviewers((S.REVIEW,), later=(S.ADVERSARIAL_REVIEW, S.ADVERSARIAL_REVIEW)),
            "reviewers の later に同じステージが 2 つある",
        ),
    ],
)
def test_reviewersの顔ぶれの規則(reviewers: Reviewers, reason: str):
    assert check(full_flow(reviewers)) == (reason,)


def test_reviewersを書けるのはReviewLoopだけ():
    flow = full_flow()
    flow[2] = step(S.IMPL, reviewers=Reviewers((S.REVIEW,)))
    assert check(flow) == ("reviewers を書けるのは ReviewLoop だけ（Impl に書いてある）",)


def test_新しいセッションでやり直せるのはタスクの間続くセッションのステージだけ():
    flow = full_flow()
    flow[2] = step(S.IMPL, fresh_session=True)
    assert check(flow) == ()
    flow[4] = step(S.GATE, fresh_session=True)
    assert check(flow) == (
        "Gate はタスクの間続くセッションを持たないので、fresh_session を書けない",
    )


def test_落ちた理由をすべて集める():
    flow = [step(S.REVIEW), step(S.REVIEW_LOOP), step(S.GATE)]
    assert check(flow, available=()) == (
        "Review は ReviewLoop の中のステージで、フローに直に置けない",
        "ReviewLoop に reviewers が無い",
        "Review に要る impl が、それより前に作られていない",
        "ReviewLoop に要る impl が、それより前に作られていない",
        "フローの終わりまでに pr-body が作られない",
    )


def test_戻り先を持つステージは戻り先をそれより前に置く():
    # 前のフローの reviewed が残っていても、Gate が落ちたときに戻る ReviewLoop が要る
    flow = [step(S.GATE), step(S.WRITE_PR_BODY)]
    assert check(flow, available=(A.DESIGN, A.IMPL, A.REVIEWED)) == (
        "Gate は落ちたときに ReviewLoop へ戻るので、ReviewLoop をそれより前に置く",
    )


def test_2ラウンド目からのレビューは省くと1ラウンド目と同じ():
    assert BOTH.for_round(1) == (S.REVIEW, S.ADVERSARIAL_REVIEW)
    assert BOTH.for_round(2) == (S.REVIEW,)
    assert Reviewers((S.REVIEW,)).for_round(3) == (S.REVIEW,)


def test_Gateが戻る先の直前のReviewLoopを探す():
    flow = Flow(tuple(full_flow()), version=1)
    assert flow.last_index_of(S.REVIEW_LOOP, before=4) == 3
    assert flow.last_index_of(S.REVIEW_LOOP, before=3) is None
    assert flow.at(Cursor(4)) == step(S.GATE)
    assert flow.at(Cursor(6)) is None
    assert Cursor(6).is_done(flow)


def test_フローとcursorの形():
    with pytest.raises(InvalidValue):
        Flow((), version=1)
    with pytest.raises(InvalidValue):
        Flow((step(S.IMPL),), version=0)
    with pytest.raises(InvalidValue):
        Cursor(3, inner=S.FIX)
    with pytest.raises(InvalidValue):
        Cursor(3, round=1)
    inside = Cursor(3).enter(S.FIX, 2)
    assert (inside.step, inside.inner, inside.round) == (3, S.FIX, 2)
    assert inside.next_step() == Cursor(4)


# --- ステージの定義 ---


def test_ステージの種類ごとに定義が1つある():
    assert set(STAGE_SPECS) == set(StageKind)


def test_合成ステージと中のステージの対応():
    for spec in STAGE_SPECS.values():
        for inner in spec.inner:
            assert STAGE_SPECS[inner].parent is spec.kind
        if spec.parent is not None:
            assert spec.kind in STAGE_SPECS[spec.parent].inner


def test_続けるセッションの先はLLMのステージ():
    for spec in STAGE_SPECS.values():
        for followed in spec.follows:
            assert STAGE_SPECS[followed].mode is StageMode.LLM
    assert STAGE_SPECS[S.FIX].follows == (S.IMPL,)
    assert STAGE_SPECS[S.DESIGN_JUDGE].session is SessionScope.RUN


def test_指摘を動かす権限はジャッジだけが持ち敵対的レビューには設計を渡さない():
    judges = {kind for kind, spec in STAGE_SPECS.items() if spec.guard and spec.guard.judge}
    assert judges == {S.JUDGE, S.DESIGN_JUDGE}
    adversarial = STAGE_SPECS[S.ADVERSARIAL_REVIEW].guard
    assert adversarial is not None
    assert not adversarial.reads_design
    askers = {kind for kind, spec in STAGE_SPECS.items() if spec.guard and spec.guard.can_ask}
    assert askers == {S.PLAN, S.REPLAN, S.REVISE}
