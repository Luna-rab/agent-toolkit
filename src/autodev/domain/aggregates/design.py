"""Design: 設計ファイルの版と、確定前の提案。

ストリームは `design`。版の本文は `design/v<版>.md` にあり、イベントは版の番号だけを持つ。

1 つの提案の進み方:

```mermaid
stateDiagram-v2
    [*] --> judging: DesignProposed（Plan・Replan。ラウンド 1）
    judging --> revising: DesignRevisionStarted（must-fix が残った。ラウンド +1）
    judging --> awaiting: DesignRoundsExhausted・DesignReverted・DesignAmbiguous
    awaiting --> revising: ResumeDesign（回答。DesignRoundsReset → DesignRevisionStarted でラウンド 1）
    revising --> judging: DesignRevised（Revise の結果）
    judging --> [*]: DesignSettled（must-fix が 0 件）
    judging --> [*]: DesignProposalAbandoned
    revising --> [*]: DesignProposalAbandoned
    awaiting --> [*]: DesignProposalAbandoned
```

不変条件:

- 提案を確定してよいのは、今の提案の版を判定した DesignJudge が、open の must-fix を 0 件と
  締めたときだけ。must-fix 以外の open の指摘は、確定したときに設計ファイルの末尾に書き足し
  （`DesignSettled.appendix`）、rejected にする。設計は直すたびに細かい指摘が立ち、must-fix 以外で
  直し続けると往復が終わらない
- 版は消さず、番号は増えるだけ（DesignJudge が過去の版と見比べる）。番号を選ぶのは本文を
  `design/v<版>.md` に書く側で、ここは使った番号と重ねないことだけを確かめる
- 1 つの提案で回せる設計のラウンドは MAX_DESIGN_ROUNDS まで。Revise を起動する前に ReviseDesign を
  出し、上限なら DesignRoundsExhausted で上げる
- **回答を待っている間は Revise を始めず、確定もしない。** 待つ原因は、ラウンドを使い切った・前の版に
  戻った・受入条件が曖昧、の 3 つで、どれも回答を持った ResumeDesign でだけ抜ける。回答は
  DesignRevisionStarted に載せて Revise の入力に渡し、ラウンドは 1 から数え直す（回答は新しい材料
  なので、残りのラウンドで打ち切らない）
- 判定（ReviseDesign・MarkReverted・MarkAmbiguous・SettleDesign）は、提案があり、Revise の途中でも
  回答待ちでもないときだけ、計画タスクの DesignJudge の実行の結果から受ける
- 新しい提案（Plan・Replan）は、確定していない提案が無いときだけ受ける。Plan は設計が一度も
  確定していないとき、Replan は確定した設計があるときだけ
- **ステージの結果（提案・判定）の中身の問題は、拒否ではなく ResultRefused で返す**（今の状態と
  合わない・使った版の番号・判定した版が今の提案と違う・戻った先の版が無い など）。拒否
  （Rejected）にするのは、出した者の取り違え（計画タスクのステージでない・判定する者でない・
  must-fix が残っているのに確定を頼む）だけである。ポリシーは ResultRefused を受けて、結果を返した
  ステージの Task に知らせ、Task が result-refused で上げる
- 提案を出した計画タスクのラン共通の成果物（brief・codemap の在りか）は、提案と一緒に受け、確定した
  ときに DesignSettled に載せる（RecordSettledPlan の artifacts）
"""

from __future__ import annotations

from enum import Enum

from ..commands.design import (
    DiscardProposal,
    MarkAmbiguous,
    MarkReverted,
    ProposeDesign,
    ResumeDesign,
    ReviseDesign,
    SettleDesign,
)
from ..events.base import Event
from ..events.design import (
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
from ..events.review_ledger import ResultReceived, ResultRefused
from ..stages.catalog import spec_of
from ..value_objects.artifact_kind import ArtifactKind
from ..value_objects.artifact_ref import ArtifactRef
from ..value_objects.design_version import DesignVersion
from ..value_objects.escalation_kind import EscalationKind
from ..value_objects.execution_id import ExecutionId
from ..value_objects.limits import MAX_DESIGN_ROUNDS
from ..value_objects.proposal import Proposal
from ..value_objects.rating import Rating
from ..value_objects.stage_kind import StageKind
from ..value_objects.stream_id import StreamId
from ..value_objects.task_id import TaskId
from .base import Aggregate, Rejected, applies, handles

#: 設計を判定するステージ
_DESIGN_JUDGE = StageKind.DESIGN_JUDGE

#: 回答を持って ResumeDesign で抜ける、回答待ちの原因（答えたエスカレーションの種類）
DESIGN_WAITS: frozenset[EscalationKind] = frozenset(
    {
        EscalationKind.DESIGN_ROUNDS_EXHAUSTED,
        EscalationKind.DESIGN_REVERTED,
        EscalationKind.DESIGN_AMBIGUOUS,
    }
)


class ProposalState(Enum):
    """確定していない提案の進み方（モジュールの docstring の図の状態）。"""

    JUDGING = "judging"
    REVISING = "revising"
    #: 回答を待っている（原因は `Design.awaiting`）
    AWAITING = "awaiting"


class Design(Aggregate):
    NAME = "Design"

    def __init__(self, stream: StreamId) -> None:
        super().__init__(stream)
        #: これまでに入った版（提案と、その直した版）。消さない
        self.versions: list[DesignVersion] = []
        #: 確定していない提案（の最新の版）
        self.proposal: Proposal | None = None
        #: 確定していない提案を出した計画タスクの、ラン共通の成果物（brief・codemap）
        self.artifacts: tuple[ArtifactRef, ...] = ()
        #: 今の提案で、いま何ラウンド目か（1 から）。提案が無ければ 0
        self.round = 0
        #: Revise を始めて、結果を待っている
        self.revising = False
        #: 回答を待っている原因（上げたエスカレーションの種類）。待っていなければ None
        self.awaiting: EscalationKind | None = None
        #: 回答を待つことになった判定（DesignJudge の実行）。回答の後の DesignRevisionStarted に載せる
        self.awaiting_judge: ExecutionId | None = None
        #: 最後に確定した提案
        self.settled: Proposal | None = None
        #: 確定した提案の decisions と deferrals を、確定した順に重ねずに集めたもの（概要 PR に載せる）
        self.decisions: tuple[str, ...] = ()
        self.deferrals: tuple[str, ...] = ()

    # --- 読む ---

    @property
    def proposal_state(self) -> ProposalState | None:
        """確定していない提案が無ければ None。"""
        if self.proposal is None:
            return None
        if self.awaiting is not None:
            return ProposalState.AWAITING
        if self.revising:
            return ProposalState.REVISING
        return ProposalState.JUDGING

    def _not_judging(self) -> str | None:
        """判定を受けられないなら、その理由（提案が無い・Revise の途中・回答待ち）。"""
        if self.proposal is None:
            return "確定していない提案が無い"
        if self.revising:
            return "Revise の結果を待っている"
        if self.awaiting is not None:
            return f"回答を待っている（{self.awaiting.value}）"
        return None

    @staticmethod
    def _check_judge(execution: ExecutionId) -> None:
        """判定を出せるのは、計画タスクの DesignJudge の実行だけ（取り違えは拒否）。"""
        if execution.stage is not _DESIGN_JUDGE or execution.task != TaskId.planning():
            raise Rejected(f"設計を判定できるのは計画タスクの DesignJudge だけ（{execution}）")

    def _used(self, version: DesignVersion) -> str | None:
        if self.versions and version.value <= max(v.value for v in self.versions):
            used = ", ".join(str(v) for v in self.versions)
            return f"版 {version} は使った番号以下（使った版: {used}）。版は消さない"
        return None

    # --- 受ける ---

    @handles(ProposeDesign)
    def _propose(self, command: ProposeDesign) -> list[Event]:
        execution = command.execution
        if execution.task != TaskId.planning():
            raise Rejected(f"設計の提案を出せるのは計画タスクのステージだけ（{execution}）")
        if ArtifactKind.PROPOSAL not in spec_of(execution.stage).produces:
            raise Rejected(f"{execution.stage.value} は設計の提案を返すステージではない")
        if refused := self._why_not_take(execution.stage, command.proposal.design):
            return [ResultRefused(execution, refused)]
        received = ResultReceived(execution)
        if execution.stage is StageKind.REVISE:
            return [DesignRevised(command.proposal, command.artifacts), received]
        return [DesignProposed(command.proposal, command.artifacts), received]

    def _why_not_take(self, stage: StageKind, version: DesignVersion) -> str | None:
        """提案を受けられないなら、その理由（今の状態と合わない・使った版の番号）。"""
        if stage is StageKind.REVISE:
            if not self.revising:
                return "Revise を始めていない（ReviseDesign が先）"
        elif self.proposal is not None:
            return "確定していない提案がすでにある"
        elif stage is StageKind.PLAN and self.settled is not None:
            return "確定した設計があるので、新しい提案は Replan から"
        elif stage is StageKind.REPLAN and self.settled is None:
            return "確定した設計がまだ無いので、新しい提案は Plan から"
        return self._used(version)

    @handles(ReviseDesign)
    def _revise(self, command: ReviseDesign) -> list[Event]:
        # 直すと決めた判定を載せて、計画タスクが今のラウンドの判定かを確かめられるようにする
        self._check_judge(command.execution)
        if refused := self._not_judging():
            return [ResultRefused(command.execution, refused)]
        if self.round >= MAX_DESIGN_ROUNDS:
            return [DesignRoundsExhausted(self.round, command.execution)]
        return [DesignRevisionStarted(self.round + 1, execution=command.execution)]

    @handles(ResumeDesign)
    def _resume(self, command: ResumeDesign) -> list[Event]:
        if self.proposal is None:
            raise Rejected("確定していない提案が無い")
        if self.awaiting is None:
            raise Rejected("回答を待っていない")
        if command.kind is not self.awaiting:
            raise Rejected(f"待っているのは {self.awaiting.value} への回答（{command.kind.value}）")
        if not command.answer.strip():
            raise Rejected("回答が空")
        return [
            DesignRoundsReset(command.answer),
            DesignRevisionStarted(1, answer=command.answer, execution=self.awaiting_judge),
        ]

    @handles(SettleDesign)
    def _settle(self, command: SettleDesign) -> list[Event]:
        self._check_judge(command.execution)
        open_findings = command.open_findings
        if must_fix := [f.finding for f in open_findings if f.rating is Rating.MUST_FIX]:
            # 確定を頼むのは、must-fix が 0 件と締めたときだけ。残っていれば頼み方の取り違え
            raise Rejected(f"must-fix が残っている: {', '.join(str(f) for f in must_fix)}")
        if refused := self._not_judging():
            return [ResultRefused(command.execution, refused)]
        proposal = self.proposal
        assert proposal is not None
        if command.design != proposal.design:
            refused = f"判定した版 {command.design} は今の提案の版 {proposal.design} ではない"
            return [ResultRefused(command.execution, refused)]
        artifacts = (
            *(a for a in self.artifacts if a.kind is not ArtifactKind.DESIGN),
            ArtifactRef(ArtifactKind.DESIGN, str(proposal.design.value)),
        )
        return [DesignSettled(proposal, command.execution, open_findings, artifacts)]

    @handles(MarkReverted)
    def _mark_reverted(self, command: MarkReverted) -> list[Event]:
        self._check_judge(command.execution)
        if refused := self._not_judging():
            return [ResultRefused(command.execution, refused)]
        assert self.proposal is not None
        if command.to_version not in self.versions or command.to_version == self.proposal.design:
            refused = f"戻った先は今の提案より前の版（{command.to_version}）"
            return [ResultRefused(command.execution, refused)]
        return [DesignReverted(command.to_version, command.execution)]

    @handles(MarkAmbiguous)
    def _mark_ambiguous(self, command: MarkAmbiguous) -> list[Event]:
        self._check_judge(command.execution)
        if refused := self._not_judging():
            return [ResultRefused(command.execution, refused)]
        return [DesignAmbiguous(command.execution)]

    @handles(DiscardProposal)
    def _discard(self, command: DiscardProposal) -> list[Event]:
        # 再計画を頼まれたポリシーは、提案があるかを知らずに出す。無ければ捨てるものが無い
        if self.proposal is None:
            return []
        return [DesignProposalAbandoned(self.proposal.design, command.reason)]

    # --- 当てる ---

    @applies(DesignProposed)
    def _proposed(self, event: DesignProposed) -> None:
        self.proposal = event.proposal
        self.artifacts = event.artifacts
        self.versions.append(event.proposal.design)
        self.round = 1
        self.revising = False

    @applies(DesignRevised)
    def _revised(self, event: DesignRevised) -> None:
        self.proposal = event.proposal
        if event.artifacts:
            self.artifacts = event.artifacts
        self.versions.append(event.proposal.design)
        self.revising = False

    @applies(DesignRevisionStarted)
    def _revision_started(self, event: DesignRevisionStarted) -> None:
        self.round = event.round
        self.revising = True

    @applies(DesignRoundsExhausted)
    def _exhausted(self, event: DesignRoundsExhausted) -> None:
        self.awaiting = EscalationKind.DESIGN_ROUNDS_EXHAUSTED
        self.awaiting_judge = event.execution

    @applies(DesignReverted)
    def _reverted(self, event: DesignReverted) -> None:
        self.awaiting = EscalationKind.DESIGN_REVERTED
        self.awaiting_judge = event.execution

    @applies(DesignAmbiguous)
    def _ambiguous(self, event: DesignAmbiguous) -> None:
        self.awaiting = EscalationKind.DESIGN_AMBIGUOUS
        self.awaiting_judge = event.execution

    @applies(DesignRoundsReset)
    def _rounds_reset(self, event: DesignRoundsReset) -> None:
        self.round = 0
        self.awaiting = None

    @applies(DesignSettled)
    def _settled(self, event: DesignSettled) -> None:
        self.settled = event.proposal
        self.decisions = tuple(dict.fromkeys((*self.decisions, *event.proposal.decisions)))
        self.deferrals = tuple(dict.fromkeys((*self.deferrals, *event.proposal.deferrals)))
        self._clear()

    @applies(DesignProposalAbandoned)
    def _abandoned(self, event: DesignProposalAbandoned) -> None:
        self._clear()

    @applies(ResultReceived)
    def _received(self, event: ResultReceived) -> None:
        pass  # 状態は変えない。提案を返したステージの Task に知らせるための記録

    @applies(ResultRefused)
    def _refused(self, event: ResultRefused) -> None:
        pass

    def _clear(self) -> None:
        self.proposal = None
        self.artifacts = ()
        self.round = 0
        self.revising = False
        self.awaiting = None
        self.awaiting_judge = None
