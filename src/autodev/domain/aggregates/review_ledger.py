"""ReviewLedger: タスク 1 つ（または設計）の指摘の台帳。

ストリームは `review/<TaskId>`（タスクの台帳。レビューの指摘 `R<番号>` と Gate の項目の指摘
`G-<項目>`）と `review/design`（設計の台帳。指摘は `D<番号>`。持ち主は計画タスク）。

不変条件:

- 状態の遷移は `open` → `closed` / `rejected` / `carried` と、`closed` / `rejected` → `open` だけ。
  `carried` は終端
- **指摘にはそれぞれ判定する者がいる**（JudgeCapability）。タスクの台帳のレビューの指摘は Judge、
  Gate の項目の指摘は Gate、設計の台帳の指摘は DesignJudge。指摘の状態を動かせるのは、その台帳の
  持ち主のタスクで走った、その指摘を判定する者の実行の結果だけ。判定を締める（RecordJudgement・
  RecordGateResult）ときに見るのも、その判定する者が判定する指摘だけである。Judge が G- の指摘を
  見ると、Judge には閉じられない G- の指摘で ReviewLoop を抜けられず、Gate との間で回り続ける
- 状態を変えるときは、コメントを必ず残す
- 修正（Fix）を受けるたびに、そのとき open の指摘の修正を受けた回数を 1 足す（CountFix は数えるだけ）。
  停滞は、判定を締めたとき（RecordJudgement・RecordGateResult）に StallPolicy で見る。停滞として
  上げた指摘は、そこからまた STALL_AFTER_FIXES 回の修正を受けるまで上げ直さない
- ステージの結果（RecordFindings・RecordJudgement）の中身の問題は、拒否ではなく ResultRefused で
  返す。結果を待つ Task がいないもの（コメント・再計画の移管）は、CommentRefused・CarryRefused で
  受けなかったことを残す。拒否（Rejected）にするのは、出した者の取り違え（判定する者でない実行・
  設計の台帳に版を付けずに立てる など）だけである
- 移した指摘は、移した先の台帳に 1 回だけ立てる
- 設計の台帳は修正を数えない。設計は直すたびに細かい指摘が立ち、同じ指摘を数える停滞には掛からない
  ので、Design のラウンドの上限で止める
- 設計の台帳が判定を締めるときは、今の提案（TrackProposal で始めた版から後）に付いた指摘だけを
  数える。捨てた提案の指摘は台帳に残るが、確定を止めない

同じコマンドを 2 回受けないこと（同じ Fix を 2 回数えるなど）は、集約の土台が command_id で弾く。
実行の id はタスクの一生を通して重ならない（Task が試行の回数を数えて決める）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..commands.review_ledger import (
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
from ..events.base import Event
from ..events.review_ledger import (
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
from ..services.gate import GateEvaluator
from ..services.stall import StallPolicy
from ..stages.catalog import STAGE_SPECS
from ..stages.kinds import Handoff
from ..value_objects.design_judgement import DesignJudgement
from ..value_objects.design_version import DesignVersion
from ..value_objects.execution_id import ExecutionId
from ..value_objects.finding_id import FindingId
from ..value_objects.finding_origin import FindingOrigin
from ..value_objects.finding_status import FindingStatus
from ..value_objects.finding_summary import FindingSummary
from ..value_objects.finding_target import FindingTarget
from ..value_objects.finding_verdict import FindingVerdict
from ..value_objects.location import Location
from ..value_objects.rating import Rating
from ..value_objects.stage_kind import StageKind
from ..value_objects.stall_cause import StallCause
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId
from ..value_objects.task_kind import TaskKind
from .base import Aggregate, Rejected, applies, handles

_S = FindingStatus

#: 判定する者が動かしてよい遷移（行き先 → 元の状態）。carried へは CarryFinding だけが動かす
_JUDGED_TRANSITIONS: dict[FindingStatus, frozenset[FindingStatus]] = {
    _S.CLOSED: frozenset({_S.OPEN}),
    _S.REJECTED: frozenset({_S.OPEN}),
    _S.OPEN: frozenset({_S.CLOSED, _S.REJECTED}),
}


class JudgeCapability:
    """指摘の状態を動かす権限。指摘ごとに判定する者が決まっている。

    出した者はステージの名乗りではなく、driver が記録した実行の id で見る。
    """

    @staticmethod
    def owner(ledger: StreamId) -> TaskId:
        """台帳の持ち主。設計の台帳は計画タスク。"""
        if ledger == StreamId.design_review():
            return TaskId.planning()
        return TaskId(ledger.value.split("/", 1)[1])

    @staticmethod
    def ledger_of(task: TaskId) -> StreamId:
        """そのタスクのステージの指摘と判定が入る台帳。計画タスクは設計の台帳。"""
        if task.kind is TaskKind.PLANNING:
            return StreamId.design_review()
        return StreamId.review(task)

    @staticmethod
    def review_judge(ledger: StreamId) -> StageKind:
        """その台帳のレビューの指摘（R・D）を判定するステージ。"""
        return StageKind.DESIGN_JUDGE if ledger == StreamId.design_review() else StageKind.JUDGE

    @staticmethod
    def judge_of(ledger: StreamId, finding: FindingId) -> StageKind:
        """その指摘を判定するステージ。Gate の項目の指摘は Gate 自身が判定する。"""
        if finding.gate_item is not None:
            return StageKind.GATE
        return JudgeCapability.review_judge(ledger)

    @staticmethod
    def allows(execution: ExecutionId, ledger: StreamId, finding: FindingId) -> bool:
        return execution.task == JudgeCapability.owner(ledger) and (
            execution.stage is JudgeCapability.judge_of(ledger, finding)
        )


@dataclass
class Finding:
    """台帳の中の指摘 1 件（エンティティ）。台帳の外から直接は変えない。"""

    id: FindingId
    rating: Rating
    body: str
    location: Location | None
    status: FindingStatus = _S.OPEN
    #: 修正を受けた回数。開き直しても戻さない（G- の指摘に修正の回数を積み上げる）
    fixes: int = 0
    #: 最後に停滞として上げたときの修正の回数
    stalled_at: int = 0
    comments: list[str] = field(default_factory=list)
    carried_from: FindingOrigin | None = None
    carried_to: TaskId | None = None
    #: 設計の台帳の指摘だけが持つ。立てたときにレビューした設計の版
    design: DesignVersion | None = None
    target: FindingTarget = FindingTarget.IMPL

    @property
    def summary(self) -> FindingSummary:
        return FindingSummary(self.id, self.rating, self.body, self.location, self.target)

    @property
    def stalled(self) -> bool:
        return StallPolicy.is_stalled(self.status, self.fixes, self.stalled_at)


class ReviewLedger(Aggregate):
    NAME = "ReviewLedger"

    def __init__(self, stream: StreamId) -> None:
        super().__init__(stream)
        self.findings: dict[FindingId, Finding] = {}
        #: 設計の台帳が数える指摘の、一番古い版（今の提案の最初の版）
        self.since: DesignVersion | None = None
        self._numbered = 0

    # --- 読む ---

    @property
    def is_design(self) -> bool:
        return self.stream == StreamId.design_review()

    @property
    def owner(self) -> TaskId:
        return JudgeCapability.owner(self.stream)

    @property
    def open_findings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings.values() if f.status is _S.OPEN)

    def _is_current(self, finding: Finding) -> bool:
        """設計の台帳で、今の提案に付いた指摘か。タスクの台帳ではどれも今の指摘。"""
        if not self.is_design:
            return True
        return (
            self.since is not None
            and finding.design is not None
            and finding.design.value >= self.since.value
        )

    def _judged_open(self, judge: StageKind) -> tuple[Finding, ...]:
        """`judge` が判定する、今の open の指摘。"""
        return tuple(
            f
            for f in self.open_findings
            if JudgeCapability.judge_of(self.stream, f.id) is judge and self._is_current(f)
        )

    def _next_id(self) -> FindingId:
        number = self._numbered + 1
        return FindingId.design(number) if self.is_design else FindingId.review(number)

    def _finding(self, finding: FindingId) -> Finding:
        if (found := self.findings.get(finding)) is None:
            raise Rejected(f"{self.stream} に {finding} の指摘が無い")
        return found

    def _check_judge(self, execution: ExecutionId, judge: StageKind) -> None:
        if execution.stage is not judge or execution.task != self.owner:
            raise Rejected(
                f"判定できるのは {self.owner} の {judge.value} の実行の結果だけ（{execution}）"
            )

    # --- 受ける ---

    @handles(RecordFindings)
    def _record_findings(self, command: RecordFindings) -> list[Event]:
        """ステージの結果の指摘をまとめて立てる。中身の問題は ResultRefused で返し、何も立てない。"""
        source = command.source
        if source.task != self.owner or STAGE_SPECS[source.stage].hands_to is not Handoff.FINDINGS:
            raise Rejected(
                f"指摘を立てられるのは {self.owner} の、指摘を挙げるステージの実行だけ（{source}）"
            )
        if self.is_design:
            if command.design is None:
                raise Rejected("設計の台帳の指摘は、見た提案の版を持つ")
            if refused := self._stale(command.design):
                return [ResultRefused(source, refused)]
        elif command.design is not None:
            raise Rejected("設計の版を持つのは設計の台帳の指摘だけ")
        if any(not finding.body.strip() for finding in command.findings):
            return [ResultRefused(source, "本文が空の指摘がある")]
        events: list[Event] = []
        for offset, finding in enumerate(command.findings, start=1):
            number = self._numbered + offset
            events.append(
                FindingRaised(
                    finding=FindingId.design(number)
                    if self.is_design
                    else FindingId.review(number),
                    rating=finding.rating,
                    body=finding.body,
                    location=finding.location,
                    source=source,
                    design=command.design,
                    target=finding.target,
                )
            )
        return [*events, ResultReceived(source)]

    def _stale(self, design: DesignVersion) -> str | None:
        """設計の台帳で、見た版が今の提案より古いなら、その理由。"""
        if self.since is None:
            return "設計の台帳が提案を追い始めていない（TrackProposal が先）"
        if design.value < self.since.value:
            return f"見た版 {design} は今の提案（版 {self.since} から）より前"
        return None

    @handles(RecordJudgement)
    def _record_judgement(self, command: RecordJudgement) -> list[Event]:
        """判定をすべて当て、判定を締める。中身の問題があれば、どれも当てずに ResultRefused で返す。"""
        judge = JudgeCapability.review_judge(self.stream)
        self._check_judge(command.execution, judge)
        if self.is_design:
            if command.stall_cause is not None:
                raise Rejected("停滞の分類を持つのはタスクの台帳だけ")
            if command.design is None:
                raise Rejected("設計の台帳の判定は、見た提案の版を持つ")
            if refused := self._stale(command.design):
                return [ResultRefused(command.execution, refused)]
        elif command.design is not None or command.design_cause is not None:
            raise Rejected("設計の版と設計の分類を持つのは設計の台帳の判定だけ")
        statuses = {finding.id: finding.status for finding in self.findings.values()}
        targets = {finding.id: finding.target for finding in self.findings.values()}
        events: list[Event] = []
        for verdict in command.verdicts:
            judged = self._judged_event(verdict, judge, statuses, command.execution)
            if isinstance(judged, str):
                return [ResultRefused(command.execution, judged)]
            if judged is not None:
                statuses[verdict.finding] = verdict.to
                events.append(judged)
            if (
                verdict.to is _S.OPEN
                and verdict.target is not None
                and verdict.target is not targets[verdict.finding]
            ):
                targets[verdict.finding] = verdict.target
                events.append(FindingRetargeted(verdict.finding, verdict.target, command.execution))
                if judged is None:
                    # 開き直しなら状態のイベントがコメントを残す。すでに open なら、宛先を変えた理由をここで残す
                    events.append(
                        FindingCommented(verdict.finding, verdict.comment, command.execution)
                    )
        remaining = tuple(
            _as_open(finding, targets[finding.id])
            for finding in self.findings.values()
            if statuses[finding.id] is _S.OPEN
            and JudgeCapability.judge_of(self.stream, finding.id) is judge
            and self._is_current(finding)
        )
        return events + self._conclude(
            command.execution,
            remaining,
            stall_cause=command.stall_cause,
            design=command.design,
            design_cause=command.design_cause,
        )

    def _judged_event(  # noqa: PLR0911  当てられない理由と行き先ごとの分岐
        self,
        verdict: FindingVerdict,
        judge: StageKind,
        statuses: dict[FindingId, FindingStatus],
        execution: ExecutionId,
    ) -> Event | str | None:
        """判定 1 件のイベント。当てられないなら、その理由。もうその状態なら None（当て済み）。

        `statuses` は、同じ判定の前の件を当てた後の状態。当て済みかもこれと比べるので、判定の並び順に
        よらない。受け取る側（Design）が受けずに走らせ直した判定は、前の判定を当てた後の台帳に同じ
        判定を返すので、当て済みとして読み飛ばす（判定する者とコメントは確かめる）。
        """
        finding = self.findings.get(verdict.finding)
        if finding is None:
            return f"{self.stream} に {verdict.finding} の指摘が無い"
        if JudgeCapability.judge_of(self.stream, finding.id) is not judge:
            return f"{finding.id} を判定するのは {judge.value} ではない"
        if not verdict.comment.strip():
            return f"{finding.id} の判定にコメントが無い"
        now = statuses[finding.id]
        if now is verdict.to:
            return None
        if now not in _JUDGED_TRANSITIONS.get(verdict.to, frozenset()):
            return f"{finding.id} を {now.value} から {verdict.to.value} へは動かせない"
        if verdict.to is _S.CLOSED:
            return FindingClosed(finding.id, verdict.comment, execution)
        if verdict.to is _S.REJECTED:
            return FindingRejected(finding.id, verdict.comment, execution)
        return FindingReopened(finding.id, verdict.comment, execution)

    @handles(RaiseFinding)
    def _raise(self, command: RaiseFinding) -> list[Event]:
        if not command.body.strip():
            raise Rejected("指摘の本文が空")
        if self.is_design:
            self._check_design_version(command.design)
            if command.carried_from is not None:
                raise Rejected("設計の台帳へは指摘を移さない")
        elif command.design is not None:
            raise Rejected("設計の版を持つのは設計の台帳の指摘だけ")
        if command.carried_from is not None and any(
            f.carried_from == command.carried_from for f in self.findings.values()
        ):
            raise Rejected(f"{command.carried_from.finding} はすでにこの台帳へ移してある")
        return [
            FindingRaised(
                finding=self._next_id(),
                rating=command.rating,
                body=command.body,
                location=command.location,
                carried_from=command.carried_from,
                source=command.source,
                design=command.design,
                target=command.finding_target,
            )
        ]

    def _check_design_version(self, design: DesignVersion | None) -> None:
        """ステージの結果でない指摘（RaiseFinding）の版の取り違えは拒む。結果なら `_stale` で返す。"""
        if design is None:
            raise Rejected("設計の台帳の指摘は、レビューした設計の版を持つ")
        if stale := self._stale(design):
            raise Rejected(stale)

    @handles(TrackProposal)
    def _track(self, command: TrackProposal) -> list[Event]:
        if not self.is_design:
            raise Rejected("提案を追うのは設計の台帳だけ")
        if self.since is not None and command.design.value <= self.since.value:
            raise Rejected(f"版 {command.design} は今の提案（版 {self.since} から）より後ではない")
        return [ProposalTracked(command.design)]

    @handles(CommentFinding)
    def _comment(self, command: CommentFinding) -> list[Event]:
        if not command.body.strip():
            raise Rejected("コメントが空")
        if command.finding not in self.findings:
            # ステージの結果が指す指摘が無い（中身の問題）。拒まずに、受けなかったことを残す
            reason = f"{self.stream} に {command.finding} の指摘が無い"
            return [CommentRefused(command.finding, reason, command.author)]
        return [FindingCommented(command.finding, command.body, command.author)]

    @handles(JudgeFinding)
    def _judge(self, command: JudgeFinding) -> list[Event]:
        """ポリシーが判定する 1 件（確定した設計の末尾に回した指摘など）。当てられなければ取り違え。"""
        finding = self._finding(command.finding)
        judge = JudgeCapability.judge_of(self.stream, finding.id)
        self._check_judge(command.execution, judge)
        verdict = FindingVerdict(finding.id, command.to, command.comment)
        statuses = {f.id: f.status for f in self.findings.values()}
        judged = self._judged_event(verdict, judge, statuses, command.execution)
        if isinstance(judged, str):
            raise Rejected(judged)
        return [] if judged is None else [judged]

    @handles(CountFix)
    def _count_fix(self, command: CountFix) -> list[Event]:
        execution = command.execution
        if self.is_design:
            raise Rejected("設計の台帳は修正を数えない（Design のラウンドの上限で止める）")
        fixes = STAGE_SPECS[execution.stage].fixes
        if fixes is None or execution.task != self.owner:
            raise Rejected(f"数えるのは {self.owner} の Fix・TestFix の実行だけ（{execution}）")
        # 直す役ごとに、自分の宛先の指摘だけを数える
        return [FixCounted(execution, tuple(f.id for f in self.open_findings if f.target is fixes))]

    @handles(RecordGateResult)
    def _record_gate(self, command: RecordGateResult) -> list[Event]:
        if self.is_design:
            raise Rejected("設計の台帳に Gate の項目の指摘は立てない")
        self._check_judge(command.execution, StageKind.GATE)
        if passed := [r.item.value for r in command.failed if r.passed]:
            raise Rejected(f"落ちた項目に、通った項目がある: {', '.join(passed)}")
        if GateEvaluator.escalation_for(command.failed) is not None:
            raise Rejected("直せない項目が落ちた Gate は上げるので、指摘にしない")
        events: list[Event] = []
        failing = {f.finding: f for f in GateEvaluator.findings_for(command.failed)}
        for finding_id, failure in failing.items():
            existing = self.findings.get(finding_id)
            if existing is None:
                events.append(
                    FindingRaised(
                        finding_id, Rating.MUST_FIX, failure.body, source=command.execution
                    )
                )
            elif existing.status is not _S.OPEN:
                events.append(FindingReopened(finding_id, failure.body, command.execution))
        for finding in self._judged_open(StageKind.GATE):
            if finding.id not in failing:
                events.append(
                    FindingClosed(finding.id, f"Gate の {finding.id} が通った", command.execution)
                )
        # 落ちた項目の指摘は、この Gate の後に open で残る。修正の回数は既存の指摘から引き継ぐ
        remaining = tuple(
            _as_open(self.findings.get(finding_id) or Finding(finding_id, *_gate_body(failure)))
            for finding_id, failure in failing.items()
        )
        return events + self._conclude(command.execution, remaining)

    def _conclude(
        self,
        execution: ExecutionId,
        judged: tuple[Finding, ...],
        *,
        stall_cause: StallCause | None = None,
        design: DesignVersion | None = None,
        design_cause: DesignJudgement | None = None,
    ) -> list[Event]:
        """判定を締める。停滞した指摘ごとの記録と、締めたこと（ジャッジの分類を写す）を出す。"""
        stalled = tuple(f for f in judged if f.stalled)
        return [
            *(FindingStalled(f.id, f.fixes) for f in stalled),
            FindingsEvaluated(
                execution,
                open_findings=tuple(f.summary for f in judged),
                stalled=tuple(f.id for f in stalled),
                stall_cause=stall_cause,
                design=design,
                design_cause=design_cause,
            ),
        ]

    @handles(CarryFinding)
    def _carry(self, command: CarryFinding) -> list[Event]:
        """移す指摘は再計画の提案（Replan の結果）が決めるので、移せない理由は CarryRefused で残す。"""
        if self.is_design:
            raise Rejected("設計の台帳の指摘は移さない")
        if refused := self._why_not_carry(command):
            return [CarryRefused(command.finding, command.to_task, refused)]
        finding = self.findings[command.finding]
        return [
            FindingCarried(
                finding.id,
                command.to_task,
                finding.rating,
                finding.body,
                finding.location,
                finding.target,
            )
        ]

    def _why_not_carry(self, command: CarryFinding) -> str | None:
        finding = self.findings.get(command.finding)
        if finding is None:
            return f"{self.stream} に {command.finding} の指摘が無い"
        if finding.status is _S.CARRIED:
            return f"{finding.id} はすでに {finding.carried_to} へ移した"
        if finding.status is not _S.OPEN:
            return f"移せるのは open の指摘だけ（{finding.id} は {finding.status.value}）"
        if finding.id.gate_item is not None:
            return "Gate の項目の指摘は、そのタスクの Gate が開き直すので移さない"
        if command.to_task == self.owner or command.to_task.kind is not TaskKind.IMPLEMENTATION:
            return f"移す先は別の実装タスク（{command.to_task}）"
        return None

    # --- 当てる ---

    @applies(FindingRaised)
    def _raised(self, event: FindingRaised) -> None:
        self.findings[event.finding] = Finding(
            event.finding,
            event.rating,
            event.body,
            event.location,
            carried_from=event.carried_from,
            design=event.design,
            target=event.target,
        )
        if event.finding.gate_item is None:
            self._numbered += 1

    @applies(FindingRetargeted)
    def _retargeted(self, event: FindingRetargeted) -> None:
        self.findings[event.finding].target = event.target

    @applies(ProposalTracked)
    def _tracked(self, event: ProposalTracked) -> None:
        self.since = event.design

    @applies(FindingCommented)
    def _commented(self, event: FindingCommented) -> None:
        self.findings[event.finding].comments.append(event.body)

    @applies(FindingClosed)
    def _closed(self, event: FindingClosed) -> None:
        self._move(event.finding, _S.CLOSED, event.comment)

    @applies(FindingRejected)
    def _rejected(self, event: FindingRejected) -> None:
        self._move(event.finding, _S.REJECTED, event.comment)

    @applies(FindingReopened)
    def _reopened(self, event: FindingReopened) -> None:
        self._move(event.finding, _S.OPEN, event.comment)

    @applies(FixCounted)
    def _fix_counted(self, event: FixCounted) -> None:
        for finding in event.findings:
            self.findings[finding].fixes += 1

    @applies(FindingStalled)
    def _stalled(self, event: FindingStalled) -> None:
        # 上げた時点の回数を覚え、そこから STALL_AFTER_FIXES 回直すまで上げ直さない
        self.findings[event.finding].stalled_at = event.fixes

    @applies(FindingsEvaluated)
    def _evaluated(self, event: FindingsEvaluated) -> None:
        pass  # 状態は変えない。ポリシーが次の手を決めるための記録

    @applies(CommentRefused)
    def _comment_refused(self, event: CommentRefused) -> None:
        pass  # 状態は変えない。受けなかったことの記録

    @applies(CarryRefused)
    def _carry_refused(self, event: CarryRefused) -> None:
        pass

    @applies(ResultReceived)
    def _received(self, event: ResultReceived) -> None:
        pass  # 状態は変えない。結果を渡したステージの Task に知らせるための記録

    @applies(ResultRefused)
    def _refused(self, event: ResultRefused) -> None:
        pass

    @applies(FindingCarried)
    def _carried(self, event: FindingCarried) -> None:
        finding = self.findings[event.finding]
        finding.status = _S.CARRIED
        finding.carried_to = event.to_task
        # 状態を変えたらコメントを残す。CarryFinding はコメントを持たないので、移した先を書く
        finding.comments.append(f"再計画で {event.to_task} へ移した")

    def _move(self, finding: FindingId, to: FindingStatus, comment: str) -> None:
        target = self.findings[finding]
        target.status = to
        target.comments.append(comment)


def _gate_body(failure: FindingSummary) -> tuple[Rating, str, Location | None]:
    return failure.rating, failure.body, failure.location


def _as_open(finding: Finding, target: FindingTarget | None = None) -> Finding:
    """handle の中で、当てた後の状態を先に見るための写し（台帳の状態は変えない）。"""
    return Finding(
        finding.id,
        finding.rating,
        finding.body,
        finding.location,
        status=_S.OPEN,
        fixes=finding.fixes,
        stalled_at=finding.stalled_at,
        target=target or finding.target,
    )
