"""指示書のプレースホルダに埋める値の出どころ（`Sources`）と、プレースホルダから出どころを引く表（`SOURCES`）。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ....domain import codec
from ....domain.aggregates.base import Aggregate
from ....domain.aggregates.design import Design
from ....domain.aggregates.review_ledger import ReviewLedger
from ....domain.aggregates.run import Run
from ....domain.aggregates.stack import Stack
from ....domain.aggregates.task import ExecutionStatus, Task
from ....domain.events.base import Event
from ....domain.events.design import DesignRevisionStarted
from ....domain.events.run import (
    AnswerRecorded,
    EscalationAnswered,
    EscalationRaised,
    ReplanRequested,
)
from ....domain.flow.flow import FlowStep
from ....domain.stages.catalog import STAGE_SPECS
from ....domain.value_objects.artifact_kind import ArtifactKind
from ....domain.value_objects.artifact_ref import ArtifactRef
from ....domain.value_objects.design_version import DesignVersion
from ....domain.value_objects.event_id import EventId
from ....domain.value_objects.finding_status import FindingStatus
from ....domain.value_objects.stage_kind import StageKind
from ....domain.value_objects.stream_id import StreamId
from ....domain.value_objects.task_id import TaskId
from ....infra.paths import RunPaths
from ...driving.mainloop import Delivery
from .assets import launcher, package_root

_A = ArtifactKind


@dataclass(frozen=True)
class Block:
    """複数行の値。表の後ろの節に置く。"""

    text: str


Value = str | Block


@dataclass(frozen=True)
class Sources:
    """プレースホルダを埋めるのに読むもの。メインループのスレッドで作り、その場で読み切る。"""

    paths: RunPaths
    aggregates: Mapping[StreamId, Aggregate]
    history: Sequence[Delivery]
    #: 誰のための値か（ステージのタスク・タスク統括のタスク。ラン統括なら計画タスク）
    task: TaskId
    stage: StageKind | None = None
    step: FlowStep | None = None
    #: ステージの cwd（実行器が決める）
    tree: Path | None = None
    #: 統括を起こした知らせ（`render_notice`）
    notice: str | None = None
    status_command: str = ""

    # --- 集約 ---

    def get(self, stream: StreamId, cls: type[Any]) -> Any:
        found = self.aggregates.get(stream)
        return found if isinstance(found, cls) else cls(stream)

    @property
    def run(self) -> Run:
        return self.get(StreamId.run(), Run)

    def task_of(self, task: TaskId) -> Task:
        return self.get(StreamId.task(task), Task)

    @property
    def subject(self) -> Task:
        return self.task_of(self.task)

    @property
    def target(self) -> TaskId:
        """ステージが扱うタスク（`Task.working_on`）。"""
        return self.subject.working_on

    @property
    def stack(self) -> Stack:
        return self.get(StreamId.stack(), Stack)

    @property
    def design(self) -> Design:
        return self.get(StreamId.design(), Design)

    def ledger(self, stream: StreamId) -> ReviewLedger:
        return self.get(stream, ReviewLedger)

    def events(self, cls: type[Event]) -> list[tuple[EventId, Any]]:
        return [(d.event_id, d.event) for d in self.history if isinstance(d.event, cls)]

    # --- 成果物 ---

    def artifact(self, kind: ArtifactKind) -> ArtifactRef | None:
        return self.subject.artifacts.get(kind)

    def file(self, ref: ArtifactRef | None) -> str:
        """ランディレクトリからのパスの成果物の、絶対パス。無ければ空。"""
        return "" if ref is None else str(self.paths.root / ref.at)


def json_block(value: Any) -> Block:
    return Block("```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```")


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def camel_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {_camel(str(k)): camel_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [camel_keys(v) for v in value]
    return value


def _required(value: Path | None, name: str) -> str:
    if value is None:
        raise ValueError(f"<{name}> を埋める値を、呼ぶ側が渡していない")
    return str(value)


def _design(s: Sources) -> str:
    """いま扱う設計の版のファイル。提案を読むステージ（`StageSpec.reads_proposal`）には、確定前の提案の版。"""
    reads_proposal = s.stage is not None and STAGE_SPECS[s.stage].reads_proposal
    ref = s.artifact(_A.PROPOSAL if reads_proposal else _A.DESIGN)
    return "" if ref is None else str(s.paths.design(DesignVersion(int(ref.at))))


def _tests_commit(s: Sources) -> str:
    ref = s.artifact(_A.TESTS)
    return "" if ref is None else ref.at


def _task_spec(s: Sources, task: TaskId) -> Block:
    entry = s.run.tasks.get(task)
    spec = s.task_of(task).spec or (entry.spec if entry is not None else None)
    body: dict[str, Any] = {"id": task.value}
    if spec is not None:
        body |= camel_keys(codec.to_json(spec))
    if entry is not None:
        body["blockedBy"] = sorted(t.value for t in entry.blocked_by)
    return json_block(body)


def _notes(s: Sources, task: TaskId) -> Value:
    notes = s.task_of(task).notes
    if not notes:
        return ""
    return json_block([camel_keys(codec.to_json(note)) for note in notes])


def _findings(ledger: ReviewLedger, keep: Callable[[Any], bool] = lambda _: True) -> Value:
    found = [
        {
            "id": f.id.value,
            "rating": f.rating.value,
            "body": f.body,
            "location": f.location.value if f.location is not None else None,
            "target": f.target.value,
            "status": f.status.value,
            "fixes": f.fixes,
            "comments": list(f.comments),
        }
        for f in ledger.findings.values()
        if keep(f)
    ]
    return json_block(found) if found else ""


def _current_design_finding(ledger: ReviewLedger) -> Callable[[Any], bool]:
    """設計の台帳で、今の提案に付いた指摘（台帳が数えるもの）。"""
    since = ledger.since

    def keep(finding: Any) -> bool:
        return (
            since is not None and finding.design is not None and finding.design.value >= since.value
        )

    return keep


def _sent_back_reason(s: Sources) -> Value:
    """これから走るステージと同じ種類の、今のフローの最後の実行が差し戻されたなら、その理由。

    走り出した実行（requested・running）は数えない。差し戻した後の試みが完了すれば、次は空になる。
    """
    task = s.task_of(s.target)
    if s.stage is None or task.flow is None:
        return ""
    same = [
        e
        for e in task.executions.values()
        if e.id.stage is s.stage and e.flow_version == task.flow.version
    ]
    while same and same[-1].status in (ExecutionStatus.REQUESTED, ExecutionStatus.RUNNING):
        same.pop()
    if not same or same[-1].status is not ExecutionStatus.SENT_BACK:
        return ""
    return Block(same[-1].end_reason or "")


def _base_commit(s: Sources) -> str:
    """タスクのブランチの根元（`Task.base_commit`）。"""
    base = s.task_of(s.target).base_commit
    return "" if base is None else base.value


def _code_tree(s: Sources) -> str:
    """コードを読む所（`Task.code_tree`。計画タスクなら、初回は概要、再計画は stack-top）。"""
    tree = s.subject.code_tree
    if tree is None:
        raise ValueError(f"{s.task} のために切った worktree がまだ無い（<コードの置き場>）")
    return str(s.paths.root / tree)


def _conflicts(s: Sources) -> Value:
    task = s.subject
    files = [*task.conflict_files, *(task.conflicts or ())]
    return json_block(files) if files else ""


def _predecessor(s: Sources) -> str:
    ref = s.artifact(_A.CONFLICTS)
    if ref is None:
        return ""
    before = TaskId(ref.at)
    branch = s.task_of(before).branch
    return f"{before}（ブランチ {branch}）" if branch is not None else before.value


def _replan_reason(s: Sources) -> Value:
    found = s.events(ReplanRequested)
    if not found:
        return ""
    _, last = found[-1]
    body: dict[str, Any] = {"reason": last.reason}
    if last.trigger is not None:
        raised = dict(s.events(EscalationRaised)).get(last.trigger)
        body["trigger"] = last.trigger.value
        if raised is not None:
            body["triggerPointers"] = camel_keys(codec.to_json(raised.pointers))
            body["triggerReason"] = raised.reason
    return json_block(body)


def _task_list(s: Sources) -> Value:
    prs = {entry.task: entry.pr.value for entry in s.stack.entries}
    listed = []
    for entry in s.run.implementation_tasks:
        task = entry.id
        ledger = s.ledger(StreamId.review(task))
        listed.append(
            {
                "id": task.value,
                "title": entry.spec.title if entry.spec is not None else "",
                "status": entry.status.value,
                "blockedBy": sorted(t.value for t in entry.blocked_by),
                "pr": prs.get(task),
                "openFindings": [f.id.value for f in ledger.open_findings],
                "spec": camel_keys(codec.to_json(entry.spec)) if entry.spec else None,
            }
        )
    return json_block(listed)


def _pr_bodies(s: Sources) -> Value:
    paths = [s.file(s.task_of(entry.task).artifacts.get(_A.PR_BODY)) for entry in s.stack.entries]
    paths = [p for p in paths if p]
    return json_block(paths) if paths else ""


def _stack_top(s: Sources) -> str:
    stack = s.stack
    if stack.entries:
        return stack.entries[-1].branch.value
    return stack.overview_branch.value if stack.overview_branch is not None else ""


def _revise_answer(s: Sources) -> str:
    found = s.events(DesignRevisionStarted)
    return (found[-1][1].answer or "") if found else ""


def _answer_log(s: Sources) -> Value:
    """ランの中の回答の全部。ユーザーの回答（質問の id 付き）と、ラン統括が自分で答えたもの。"""
    found: list[dict[str, Any]] = []
    for eid, event in s.events(AnswerRecorded):
        entry: dict[str, Any] = {
            "origin": "user",
            "question": event.question.value,
            "escalation": event.escalation.value if event.escalation else None,
            "answer": event.answer,
            "event": eid.value,
        }
        if event.escalation_closed:
            entry["escalationClosed"] = True
        found.append(entry)
    for eid, event in s.events(EscalationAnswered):
        if event.question is None:
            found.append(
                {
                    "origin": "run-supervisor",
                    "escalation": event.escalation.value,
                    "answer": event.answer,
                    "event": eid.value,
                }
            )
    return json_block(found) if found else ""


#: プレースホルダ → 埋める値の出どころ。指示書の「入力」の表に足したら、ここにも足す
SOURCES: Mapping[str, Callable[[Sources], Value]] = {
    # 場所
    "worktree": lambda s: _required(s.tree, "worktree"),
    "コードの置き場": _code_tree,
    "ランディレクトリ": lambda s: str(s.paths.root),
    "autodev": lambda s: launcher(),
    "これまでの結果": lambda s: str(s.paths.task_results(s.target)),
    "状態を読むコマンド": lambda s: s.status_command,
    # ラン共通の成果物
    "ブリーフ": lambda s: s.file(s.artifact(_A.BRIEF)),
    "コードマップ": lambda s: s.file(s.artifact(_A.CODEMAP)),
    "設計": _design,
    "設計の置き場": lambda s: str(s.paths.designs),
    # タスク
    "タスク": lambda s: _task_spec(s, s.target),
    "決めたこと": lambda s: _notes(s, s.task),
    "基準のコミット": _base_commit,
    "テストのコミット": _tests_commit,
    "期待値待ちのテスト": lambda s: s.file(s.artifact(_A.AWAITING_EXPECTATIONS)),
    "衝突したファイル": _conflicts,
    "引き継ぎ元": _predecessor,
    "統括からの言葉": lambda s: (s.step.instruction or "") if s.step is not None else "",
    "指摘": lambda s: _findings(s.ledger(StreamId.review(s.target))),
    "差し戻しの理由": _sent_back_reason,
    # 設計の段
    "設計の指摘": lambda s: _findings(
        s.ledger(StreamId.design_review()),
        _current_design_finding(s.ledger(StreamId.design_review())),
    ),
    "却下した論点": lambda s: _findings(
        s.ledger(StreamId.design_review()), lambda f: f.status is FindingStatus.REJECTED
    ),
    "提案のタスク": lambda s: json_block(
        [camel_keys(codec.to_json(t)) for t in s.design.proposal.tasks]
        if s.design.proposal is not None
        else []
    ),
    "回答": _revise_answer,
    "再計画の理由": _replan_reason,
    # ランとスタック
    "タスクの一覧": _task_list,
    "タスク PR の本文": _pr_bodies,
    "概要ブランチ": lambda s: (
        s.stack.overview_branch.value if s.stack.overview_branch is not None else ""
    ),
    "スタックの一番上": _stack_top,
    "概要 PR の雛形": lambda s: str(package_root() / "templates" / "overview-pr-body.md"),
    # 統括
    "通知": lambda s: Block(s.notice or ""),
    "回答の記録": _answer_log,
}
