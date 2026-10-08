"""ステージの定義。ステージの種類ごとに `StageSpec` を 1 つ持つ。

`needs`・`produces` は、タスクの成果物（`ArtifactKind`）で書く。合成ステージの中のステージどうしで
受け渡すもの（指摘・判定）は、合成ステージの中の進め方が順番を決めるので、ここには書かない。
中のステージの `needs` は、合成ステージに入る時点でタスクが持っている成果物だけを書く。

ステージの性質（合成ステージの中の役・返してよい報告・結果の JSON で読む欄・結果を渡す先・決定的な
ステージが期待する証拠・衝突したときだけ走るか・変えずに終えてよいか）はここに宣言し、Task はそれを
読む 1 つの規則で進める（`aggregates/task.py`）。ステージを足すときに、Task の側に表や分岐を書き足さないため
である。git 管理タスクの仕事の種類ごとの並びも、ここに宣言する（`GIT_JOB_STAGES`）。

LLM のステージはモデルのクラス（`model_class`）を持つ。クラスに当たるモデルと effort はランごとに
決まり（`RunStarted.models`）、進め方の判断には使わない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..services.verify import VERIFY_KINDS
from ..value_objects.artifact_kind import ArtifactKind
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.finding_target import FindingTarget
from ..value_objects.git_job_kind import GitJobKind
from ..value_objects.guard import Guard
from ..value_objects.model_class import ModelClass
from ..value_objects.stage_kind import StageKind
from ..value_objects.task_kind import TaskKind
from ..value_objects.write_scope import WriteScope
from .kinds import (
    BodyTarget,
    EvidenceCheck,
    Handoff,
    InnerRole,
    ResultField,
    SessionScope,
    StageMode,
    StepArgument,
)


@dataclass(frozen=True)
class StageSpec:
    kind: StageKind
    mode: StageMode
    #: このステージを置けるタスクの種類
    task_kinds: frozenset[TaskKind]
    needs: frozenset[ArtifactKind] = frozenset()
    #: あれば使う成果物（無くても起動できる）
    optional: frozenset[ArtifactKind] = frozenset()
    produces: frozenset[ArtifactKind] = frozenset()
    #: 作ることがある成果物（無くても完了する）
    may_produce: frozenset[ArtifactKind] = frozenset()
    #: 完了したら外す成果物（Expect が期待値を書いたら、期待値待ちは外れる）。同じ完了で作り直した
    #: ものは外さない（Expect が書き残したテストがあれば、期待値待ちは残る）
    consumes: frozenset[ArtifactKind] = frozenset()
    #: フローの中で、このステージより後になければならない種類（TestGen は Impl より前）
    before: frozenset[StageKind] = frozenset()
    #: LLM のステージだけが持つ
    guard: Guard | None = None
    session: SessionScope = SessionScope.NONE
    follows: tuple[StageKind, ...] = ()
    #: 合成ステージの中のステージ（並びの順）
    inner: tuple[StageKind, ...] = ()
    #: 中のステージなら、属する合成ステージ。中のステージはフローに直に置けない
    parent: StageKind | None = None
    #: 中のステージの役
    role: InnerRole | None = None
    #: 頭の役が走る条件の成果物
    when: ArtifactKind | None = None
    #: フローの段に書ける引数
    arguments: frozenset[StepArgument] = frozenset()
    #: フローの段に必ず書く引数
    required_arguments: frozenset[StepArgument] = frozenset()
    #: 落ちたときに戻る先（Gate は直前の ReviewLoop の直す役に戻る）。フローの中でこれより前に要る
    returns_to: StageKind | None = None
    #: 結果の `report` で返してよい報告（失敗に数えず、cursor も進めない）
    reports: frozenset[EscalationKind] = frozenset()
    #: 結果の JSON でドメインが読む欄
    result: frozenset[ResultField] = frozenset()
    #: 結果を渡す先。渡した先が受けるまで cursor を進めない
    hands_to: Handoff | None = None
    #: 確かめた結果「変えない」と返して、作る成果物の実物なしに終えてよいか。前に作った成果物を
    #: そのまま使う（報告を確かめ直したときなど）。前に作っていなければ、形の誤りで落ちる
    can_keep: bool = False
    #: 決定的なステージが期待する証拠と、外れたときの報告。報告を書かなければ、外れたら失敗に数える
    #: （1 回はやり直し、続けて落ちたら stage-errors）
    expects: EvidenceCheck | None = None
    on_mismatch: EscalationKind | None = None
    #: 同じフローの前の Rebase が衝突したときだけ走る
    after_conflict_only: bool = False
    #: 結果の `body` を書き出す先（`body` を読むステージだけ）
    body: BodyTarget | None = None
    #: 完了したら、衝突で止まっていた rebase を終えている（CheckUnion は両側を残したと確かめてから
    #: 続ける）。タスクのブランチは、同じフローの Rebase が返した先（`onto`）に載った
    finishes_rebase: bool = False
    #: 流す前に、cwd の worktree に残った途中の rebase を取りやめる（実行器が流す前にする）。流し直しで
    #: 前の rebase の解きかけ（`git add` して衝突の段が消えたもの）から続けず、移るブランチも取り違えない
    abandons_rebase: bool = False
    #: 流す前に、cwd の worktree を始めた時点（`start_commit`）へ戻す（実行器が、途中の rebase を
    #: 取りやめた後にする）。driver が、中身を終えてから結果が載る前に落ちると、呼び直した driver は同じ
    #: 実行を流し直す。そのとき Task の事実（根元）は古いままなので、中身が動かした後の HEAD から流すと
    #: 同じ操作を二度かける。始めた時点から流せば、何度流しても同じ結果になる
    restores_start: bool = False
    #: 真なら、driver がステージの前後で軽い検査を流す（LLM で、検証コマンドの行があるステージだけ）
    checks_after: bool = False
    #: ReviewLoop の直す役だけが持つ。直す指摘の宛先（Fix は impl、TestFix は tests）
    fixes: FindingTarget | None = None
    #: LLM のステージだけが持つ。claude に渡すモデルと effort は、ランが記録したこのクラスの値
    model_class: ModelClass | None = None
    #: ターンの上限。値は実装のときに決める
    max_turns: int | None = None

    def __post_init__(self) -> None:  # noqa: PLR0912  宣言の組み合わせの検査ごとの分岐
        name = self.kind.value
        if (self.mode is StageMode.LLM) != (self.guard is not None):
            raise ValueError(f"{name}: Guard を持つのは LLM のステージだけ")
        if (self.mode is StageMode.LLM) != (self.model_class is not None):
            raise ValueError(f"{name}: モデルのクラスを持つのは LLM のステージだけ")
        if (self.mode is StageMode.COMPOSITE) != bool(self.inner):
            raise ValueError(f"{name}: 中のステージを持つのは合成ステージだけ")
        if (self.session is SessionScope.FOLLOWS) != bool(self.follows):
            raise ValueError(f"{name}: follows は FOLLOWS のときだけ書く")
        if self.mode is not StageMode.LLM and self.session is not SessionScope.NONE:
            raise ValueError(f"{name}: セッションを持つのは LLM のステージだけ")
        if not self.required_arguments <= self.arguments:
            raise ValueError(f"{name}: 必ず書く引数は、書ける引数に含める")
        if (self.parent is None) != (self.role is None):
            raise ValueError(f"{name}: 役を持つのは中のステージだけ")
        if (self.role is InnerRole.HEAD) != (self.when is not None):
            raise ValueError(f"{name}: 走る条件（when）を持つのは頭の役だけ")
        if self.reports and self.mode is not StageMode.LLM:
            raise ValueError(f"{name}: 結果で報告を返すのは LLM のステージだけ")
        if self.result and self.mode is StageMode.COMPOSITE:
            raise ValueError(f"{name}: 合成ステージは結果の JSON を返さない")
        reads_report = {ResultField.REPORT, ResultField.REPORT_REASON} & self.result
        if bool(reads_report) != bool(self.reports) or len(reads_report) == 1:
            raise ValueError(f"{name}: report と reportReason は、返してよい報告があるときだけ読む")
        if self.can_keep != (ResultField.UNCHANGED in self.result):
            raise ValueError(f"{name}: unchanged の欄は、変えずに終えてよいステージだけが読む")
        if self.can_keep and not self.produces:
            raise ValueError(f"{name}: 変えずに終えてよいのは、成果物を作るステージだけ")
        if self.expects is not None and self.mode is not StageMode.PROGRAM:
            raise ValueError(f"{name}: 期待する証拠を持つのは決定的なステージだけ")
        if self.restores_start and self.mode is not StageMode.PROGRAM:
            # LLM のステージは interrupt から続けるので、戻すと続ける仕事を捨てる
            raise ValueError(f"{name}: 始めた時点へ戻して流し直すのは決定的なステージだけ")
        if self.checks_after and (self.mode is not StageMode.LLM or self.kind not in VERIFY_KINDS):
            raise ValueError(
                f"{name}: 前後に軽い検査を流すのは、検証コマンドの行がある LLM のステージだけ"
            )
        if self.on_mismatch is not None and self.expects is None:
            raise ValueError(f"{name}: 外れたときの報告は、期待する証拠と一緒に書く")
        if (self.body is not None) != (ResultField.BODY in self.result):
            raise ValueError(f"{name}: body の欄を読むステージは、書き出す先（body）を宣言する")
        if (self.body is BodyTarget.TASK_PR) != (ArtifactKind.PR_BODY in self.produces):
            raise ValueError(f"{name}: タスク PR の本文を書き出すステージだけが pr-body を作る")
        if (self.body is BodyTarget.OVERVIEW_PR) != (ResultField.TITLE in self.result):
            raise ValueError(f"{name}: 概要 PR の本文を書き出すステージだけが title を返す")

    @property
    def raises(self) -> frozenset[EscalationKind]:
        """このステージの結果から上がりうるエスカレーション（Gate の項目の分は GateEvaluator が決める）。"""
        mismatch = {self.on_mismatch} if self.on_mismatch is not None else set()
        refused = {EscalationKind.RESULT_REFUSED} if self.hands_to is not None else set()
        return self.reports | mismatch | refused

    @property
    def reads_proposal(self) -> bool:
        """設計を読むとき、確定した版ではなく、確定する前の提案の版を読むか（名前の付いた規則）。

        自分か、属する合成ステージが提案（proposal）を使うと宣言していれば読む。設計の段の中の
        ステージ（DesignReview・DesignJudge・Revise）は、DesignLoop の宣言で提案を読む。
        """
        wanted = self.needs | self.optional
        if self.parent is not None:
            wanted |= STAGE_SPECS[self.parent].needs
        return ArtifactKind.PROPOSAL in wanted


_PLANNING = frozenset({TaskKind.PLANNING})
_IMPLEMENTATION = frozenset({TaskKind.IMPLEMENTATION})
_GIT = frozenset({TaskKind.GIT})
_LEAD = ModelClass.LEAD
_REVIEW = ModelClass.REVIEW
_IMPLEMENT = ModelClass.IMPLEMENT
_WRITE = ModelClass.WRITE

_READ_ONLY = Guard(WriteScope.NONE)
_PLANNER = Guard(WriteScope.NONE, can_ask=True)
_JUDGE = Guard(WriteScope.NONE, judge=True)


_NOTHING: frozenset[ArtifactKind] = frozenset()
_E = EscalationKind
_A = ArtifactKind
_S = StageKind
_R = InnerRole
_F = ResultField
_TO = Handoff


def _llm(
    kind: StageKind,
    task_kinds: frozenset[TaskKind],
    model_class: ModelClass,
    guard: Guard,
    session: SessionScope = SessionScope.FRESH,
    *,
    needs: frozenset[ArtifactKind] = _NOTHING,
    optional: frozenset[ArtifactKind] = _NOTHING,
    produces: frozenset[ArtifactKind] = _NOTHING,
    may_produce: frozenset[ArtifactKind] = _NOTHING,
    consumes: frozenset[ArtifactKind] = _NOTHING,
    before: frozenset[StageKind] = frozenset(),
    follows: tuple[StageKind, ...] = (),
    parent: StageKind | None = None,
    role: InnerRole | None = None,
    when: ArtifactKind | None = None,
    reports: frozenset[EscalationKind] = frozenset(),
    result: frozenset[ResultField] = frozenset(),
    hands_to: Handoff | None = None,
    can_keep: bool = False,
    after_conflict_only: bool = False,
    body: BodyTarget | None = None,
    checks_after: bool = False,
    fixes: FindingTarget | None = None,
) -> StageSpec:
    if reports:
        result |= {_F.REPORT, _F.REPORT_REASON}
    if can_keep:
        result |= {_F.UNCHANGED}
    if body is not None:
        result |= {_F.BODY}
    if body is BodyTarget.OVERVIEW_PR:
        result |= {_F.TITLE}
    return StageSpec(
        kind,
        StageMode.LLM,
        task_kinds,
        needs=needs,
        optional=optional,
        produces=produces,
        may_produce=may_produce,
        consumes=consumes,
        before=before,
        guard=guard,
        session=session,
        follows=follows,
        parent=parent,
        role=role,
        when=when,
        reports=reports,
        result=result,
        hands_to=hands_to,
        can_keep=can_keep,
        after_conflict_only=after_conflict_only,
        body=body,
        checks_after=checks_after,
        fixes=fixes,
        model_class=model_class,
    )


def _program(
    kind: StageKind,
    task_kinds: frozenset[TaskKind],
    *,
    needs: frozenset[ArtifactKind] = _NOTHING,
    produces: frozenset[ArtifactKind] = _NOTHING,
    before: frozenset[StageKind] = frozenset(),
    returns_to: StageKind | None = None,
    expects: EvidenceCheck | None = None,
    on_mismatch: EscalationKind | None = None,
    after_conflict_only: bool = False,
    result: frozenset[ResultField] = frozenset(),
    hands_to: Handoff | None = None,
    finishes_rebase: bool = False,
    abandons_rebase: bool = False,
    restores_start: bool = False,
) -> StageSpec:
    return StageSpec(
        kind,
        StageMode.PROGRAM,
        task_kinds,
        needs=needs,
        produces=produces,
        before=before,
        returns_to=returns_to,
        expects=expects,
        on_mismatch=on_mismatch,
        after_conflict_only=after_conflict_only,
        result=result,
        hands_to=hands_to,
        finishes_rebase=finishes_rebase,
        abandons_rebase=abandons_rebase,
        restores_start=restores_start,
    )


#: 提案（Proposal）を返すステージが返す欄。止める・破棄する・移すは、確定した設計がある再計画だけ
_PROPOSAL = frozenset(
    {_F.DESIGN, _F.TASKS, _F.QUICK_CHECKS, _F.REGRESSION_TESTS, _F.DECISIONS, _F.DEFERRALS}
)
_REPLAN = _PROPOSAL | {_F.STOP, _F.DISCARD, _F.CARRY}
_PR = frozenset({_F.PR})

_SPECS: tuple[StageSpec, ...] = (
    # --- 計画タスク ---
    _program(_S.PREPARE, _PLANNING, produces=frozenset({_A.BRIEF})),
    _llm(
        _S.PLAN,
        _PLANNING,
        _LEAD,
        Guard(WriteScope.NONE, reads_design=False, can_ask=True),
        needs=frozenset({_A.BRIEF}),
        produces=frozenset({_A.PROPOSAL, _A.CODEMAP}),
        result=_PROPOSAL | {_F.CODEMAP},
        hands_to=_TO.PROPOSAL,
    ),
    _llm(
        _S.REPLAN,
        _PLANNING,
        _LEAD,
        _PLANNER,
        needs=frozenset({_A.DESIGN}),
        produces=frozenset({_A.PROPOSAL}),
        result=_REPLAN,
        hands_to=_TO.PROPOSAL,
    ),
    StageSpec(
        _S.DESIGN_LOOP,
        StageMode.COMPOSITE,
        _PLANNING,
        needs=frozenset({_A.PROPOSAL}),
        produces=frozenset({_A.DESIGN}),
        inner=(_S.DESIGN_REVIEW, _S.DESIGN_JUDGE, _S.REVISE),
    ),
    _llm(
        _S.DESIGN_REVIEW,
        _PLANNING,
        _REVIEW,
        _READ_ONLY,
        parent=_S.DESIGN_LOOP,
        role=_R.LOOKER,
        result=frozenset({_F.FINDINGS}),
        hands_to=_TO.FINDINGS,
    ),
    _llm(
        _S.DESIGN_JUDGE,
        _PLANNING,
        _LEAD,
        _JUDGE,
        SessionScope.RUN,
        parent=_S.DESIGN_LOOP,
        role=_R.JUDGE,
        result=frozenset({_F.VERDICTS, _F.COMMENTS, _F.DESIGN_CAUSE}),
        # 設計の台帳が判定を締めた後、Design が確定・Revise・回答待ちを決める
        hands_to=_TO.JUDGEMENT,
    ),
    _llm(
        _S.REVISE,
        _PLANNING,
        _LEAD,
        _PLANNER,
        SessionScope.FOLLOWS,
        follows=(_S.PLAN, _S.REPLAN),
        parent=_S.DESIGN_LOOP,
        role=_R.FIXER,
        # 直した提案の新しい版
        produces=frozenset({_A.PROPOSAL}),
        result=_REPLAN | {_F.COMMENTS},
        hands_to=_TO.PROPOSAL,
    ),
    # --- 実装タスク ---
    _llm(
        _S.TEST_GEN,
        _IMPLEMENTATION,
        _IMPLEMENT,
        Guard(WriteScope.TESTS_AND_STUBS),
        needs=frozenset({_A.DESIGN}),
        produces=frozenset({_A.TESTS}),
        # 期待値を決められないテストは、期待値を空にして報告する。ReviewLoop の頭の Expect が書く
        may_produce=frozenset({_A.AWAITING_EXPECTATIONS}),
        before=frozenset({_S.IMPL}),
        # quick-check-failed: 軽い検査がテストの外のファイルで落ちていて、テストしか書けない自分には直せない
        reports=frozenset({_E.DESIGN_GAP, _E.QUICK_CHECK_FAILED}),
        result=frozenset({_F.AWAITING_EXPECTATIONS}),
        # test-conflict を確かめ直して、テストが正しいと決めたら変えずに終える
        can_keep=True,
        # テストを書いた後の import・型・lint の誤りを、Impl の前に見つける
        checks_after=True,
    ),
    _program(
        _S.CONFIRM_RED,
        _IMPLEMENTATION,
        needs=frozenset({_A.TESTS}),
        produces=frozenset({_A.RED_TESTS}),
        before=frozenset({_S.IMPL}),
        # テストが実装の前に全部通った、または軽い検査が落ちた。書き直させるかはタスク統括がフローで決める
        expects=EvidenceCheck.RED_AND_CLEAN,
        on_mismatch=_E.RED_CHECK_FAILED,
    ),
    _llm(
        _S.IMPL,
        _IMPLEMENTATION,
        _IMPLEMENT,
        Guard(WriteScope.NON_TESTS),
        SessionScope.TASK,
        needs=frozenset({_A.DESIGN}),
        optional=frozenset({_A.RED_TESTS}),
        produces=frozenset({_A.IMPL}),
        reports=frozenset({_E.DESIGN_GAP, _E.TEST_CONFLICT}),
        result=frozenset({_F.COMMENTS}),
        can_keep=True,
    ),
    StageSpec(
        _S.REVIEW_LOOP,
        StageMode.COMPOSITE,
        _IMPLEMENTATION,
        needs=frozenset({_A.IMPL}),
        produces=frozenset({_A.REVIEWED}),
        inner=(_S.EXPECT, _S.REVIEW, _S.ADVERSARIAL_REVIEW, _S.JUDGE, _S.TEST_FIX, _S.FIX),
        arguments=frozenset({StepArgument.REVIEWERS}),
        required_arguments=frozenset({StepArgument.REVIEWERS}),
    ),
    _llm(
        _S.EXPECT,
        _IMPLEMENTATION,
        _IMPLEMENT,
        Guard(WriteScope.TESTS_ONLY),
        needs=frozenset({_A.IMPL}),
        # 期待値を書いたら、そのコミットが tests の在りかになる（Gate の項目 4）。書き残したテストが
        # あれば、期待値待ちは残る（次のラウンドの頭でまた走る）
        may_produce=frozenset({_A.TESTS, _A.AWAITING_EXPECTATIONS}),
        consumes=frozenset({_A.AWAITING_EXPECTATIONS}),
        parent=_S.REVIEW_LOOP,
        role=_R.HEAD,
        when=_A.AWAITING_EXPECTATIONS,
        reports=frozenset({_E.DESIGN_GAP, _E.TEST_CONFLICT}),
        # 受入条件と食い違う出力は、Expect を出どころとする指摘にし、同じラウンドの Judge が判定する
        result=frozenset({_F.AWAITING_EXPECTATIONS, _F.DEFECTS}),
        hands_to=_TO.FINDINGS,
    ),
    _llm(
        _S.REVIEW,
        _IMPLEMENTATION,
        _REVIEW,
        _READ_ONLY,
        needs=frozenset({_A.IMPL}),
        parent=_S.REVIEW_LOOP,
        role=_R.LOOKER,
        result=frozenset({_F.FINDINGS}),
        hands_to=_TO.FINDINGS,
    ),
    _llm(
        _S.ADVERSARIAL_REVIEW,
        _IMPLEMENTATION,
        _REVIEW,
        Guard(WriteScope.NONE, reads_design=False),
        needs=frozenset({_A.IMPL}),
        parent=_S.REVIEW_LOOP,
        role=_R.LOOKER,
        result=frozenset({_F.FINDINGS}),
        hands_to=_TO.FINDINGS,
    ),
    _llm(
        _S.JUDGE,
        _IMPLEMENTATION,
        _LEAD,
        _JUDGE,
        SessionScope.TASK,
        parent=_S.REVIEW_LOOP,
        role=_R.JUDGE,
        result=frozenset({_F.VERDICTS, _F.COMMENTS, _F.STALL_CAUSE, _F.STALL_REASON}),
        hands_to=_TO.JUDGEMENT,
    ),
    _llm(
        _S.FIX,
        _IMPLEMENTATION,
        _IMPLEMENT,
        Guard(WriteScope.NON_TESTS),
        SessionScope.FOLLOWS,
        follows=(_S.IMPL,),
        parent=_S.REVIEW_LOOP,
        role=_R.FIXER,
        reports=frozenset({_E.DESIGN_GAP, _E.TEST_CONFLICT}),
        result=frozenset({_F.COMMENTS}),
        fixes=FindingTarget.IMPL,
    ),
    _llm(
        _S.TEST_FIX,
        _IMPLEMENTATION,
        _IMPLEMENT,
        Guard(WriteScope.TESTS_ONLY),
        SessionScope.FOLLOWS,
        follows=(_S.TEST_GEN,),
        parent=_S.REVIEW_LOOP,
        role=_R.FIXER,
        may_produce=frozenset({_A.TESTS}),
        reports=frozenset({_E.DESIGN_GAP, _E.QUICK_CHECK_FAILED}),
        result=frozenset({_F.COMMENTS}),
        checks_after=True,
        fixes=FindingTarget.TESTS,
    ),
    _program(
        _S.GATE,
        _IMPLEMENTATION,
        needs=frozenset({_A.REVIEWED}),
        produces=frozenset({_A.GATED}),
        returns_to=_S.REVIEW_LOOP,
        expects=EvidenceCheck.GATE_PASSES,
    ),
    _llm(
        _S.WRITE_PR_BODY,
        _IMPLEMENTATION,
        _WRITE,
        _READ_ONLY,
        needs=frozenset({_A.GATED}),
        produces=frozenset({_A.PR_BODY}),
        body=BodyTarget.TASK_PR,
    ),
    # 統合をやり直す差し込んだタスクと、git 管理タスクの両方に置ける。「両方は残せない」を報告する。
    # 差し込んだタスクで書いてよいファイルは、引き継いだ衝突（conflicts の成果物）
    _llm(
        _S.RESOLVE_CONFLICT,
        frozenset({TaskKind.IMPLEMENTATION, TaskKind.GIT}),
        _IMPLEMENT,
        Guard(WriteScope.LISTED),
        reports=frozenset({_E.INTEGRATION_FAILED}),
        after_conflict_only=True,
    ),
    # --- git 管理タスク ---
    # 流し直すときは、途中の rebase を取りやめてから流す。統合に失敗して取りやめていない worktree・
    # 衝突で止まった前の流しの worktree から、切ったブランチへ移す・載せ直す
    _program(
        _S.CUT_BRANCH,
        _GIT,
        result=frozenset({_F.WORKTREE_TASK, _F.TREE, _F.BRANCH, _F.BASE}),
        abandons_rebase=True,
    ),
    # 終えた rebase の後で結果が載らずに流し直すときは、載せ直す前の HEAD から古い根元で載せ直す。
    # 載せ直すコミットが無ければ、ブランチを動かさずに落ちる
    _program(
        _S.REBASE,
        _GIT,
        result=frozenset({_F.ONTO}),
        expects=EvidenceCheck.OWN_COMMITS,
        abandons_rebase=True,
        restores_start=True,
    ),
    _program(
        _S.CHECK_UNION,
        _GIT,
        expects=EvidenceCheck.UNION_KEPT,
        on_mismatch=_E.INTEGRATION_FAILED,
        after_conflict_only=True,
        finishes_rebase=True,
    ),
    # 積む直前のラン共通の検証が落ちたら、意味が変わる統合として扱う
    _program(
        _S.INTEGRATION_CHECK,
        _GIT,
        expects=EvidenceCheck.VERIFY_PASSES,
        on_mismatch=_E.INTEGRATION_FAILED,
    ),
    _program(_S.PUSH, _GIT),
    _program(_S.CREATE_PR, _GIT, result=_PR),
    # つないだ PR（このタスクの PR）の番号を返し、Stack に積んだ 1 本として渡す（AppendEntry）
    _program(_S.STACK_LINK, _GIT, result=_PR, hands_to=_TO.ENTRY),
    _program(_S.REFRESH_OVERVIEW, _GIT),
    _llm(_S.WRITE_OVERVIEW, _GIT, _WRITE, _READ_ONLY, body=BodyTarget.OVERVIEW_PR),
    # 作った概要 PR の番号を、スタックの一番下として渡す（RecordOverview）
    _program(_S.CREATE_OVERVIEW_PR, _GIT, result=_PR, hands_to=_TO.OVERVIEW),
    _program(_S.READY_OVERVIEW, _GIT),
    _program(_S.CLOSE_PRS, _GIT),
    _program(_S.UNSTACK, _GIT),
    # 作り直したら、閉じた所を Stack に渡す（UnstackFrom）
    _program(_S.RELINK, _GIT, hands_to=_TO.CUT_BACK),
)

#: ステージの種類 → 定義
STAGE_SPECS: Mapping[StageKind, StageSpec] = {spec.kind: spec for spec in _SPECS}


def spec_of(kind: StageKind) -> StageSpec:
    return STAGE_SPECS[kind]


def inner_with(composite: StageKind, role: InnerRole) -> tuple[StageKind, ...]:
    """合成ステージの中で `role` の役を持つステージ（並びの順）。"""
    return tuple(kind for kind in STAGE_SPECS[composite].inner if STAGE_SPECS[kind].role is role)


#: ReviewLoop の reviewers に書けるステージ（ReviewLoop の見る役）
REVIEWER_STAGES: frozenset[StageKind] = frozenset(inner_with(_S.REVIEW_LOOP, _R.LOOKER))

_STACKING = (
    _S.REBASE,
    _S.RESOLVE_CONFLICT,
    _S.CHECK_UNION,
    _S.INTEGRATION_CHECK,
    _S.PUSH,
    _S.CREATE_PR,
    _S.STACK_LINK,
    _S.REFRESH_OVERVIEW,
)

#: git 管理タスクの仕事の種類ごとの並び。積み直す仕事（前に
#: 積んだブランチがある）は、頭に CutBranch を足す（`flow.git_job_flow`）
GIT_JOB_STAGES: Mapping[GitJobKind, tuple[StageKind, ...]] = {
    GitJobKind.CUT_OVERVIEW: (_S.CUT_BRANCH,),
    GitJobKind.CUT_TASK: (_S.CUT_BRANCH,),
    GitJobKind.CUT_STACK_TOP: (_S.CUT_BRANCH,),
    GitJobKind.OPEN_OVERVIEW: (_S.WRITE_OVERVIEW, _S.CREATE_OVERVIEW_PR, _S.REFRESH_OVERVIEW),
    GitJobKind.REWRITE_OVERVIEW: (_S.WRITE_OVERVIEW, _S.REFRESH_OVERVIEW),
    GitJobKind.STACK: _STACKING,
    GitJobKind.DISCARD: (_S.CLOSE_PRS, _S.UNSTACK, _S.RELINK, _S.REFRESH_OVERVIEW),
    GitJobKind.FINISH: (_S.WRITE_OVERVIEW, _S.REFRESH_OVERVIEW, _S.READY_OVERVIEW),
}
