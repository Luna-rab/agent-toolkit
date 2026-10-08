from __future__ import annotations

from enum import Enum

from .escalation_kind import EscalationKind


class GateItem(Enum):
    """完了チェックの項目。"""

    #: そのタスクのコミットが親ブランチから 1 件以上ある
    COMMITS = "commits"
    #: 指摘の台帳に `open` の指摘が無い
    NO_OPEN_FINDINGS = "no-open-findings"
    #: フローの最後の ReviewLoop で、その回の reviewers がすべて走り終えている
    REVIEWERS_RAN = "reviewers-ran"
    #: TestGen があれば、そのコミットの後でテストのファイルが変わっていない
    TESTS_UNCHANGED = "tests-unchanged"
    #: TestGen が無ければ、変わったファイルがすべてテストが要らないパスに収まっている
    UNTESTED_PATHS = "untested-paths"
    #: そのタスクの verify がすべて通る
    VERIFY = "verify"

    @property
    def escalation(self) -> EscalationKind | None:
        """この項目が落ちたときに上げるエスカレーション。None ならコードを直せば解けるので、
        上げずに G- の指摘を開く。"""
        return _GATE_ESCALATIONS[self]


GATE_ITEM_VALUES = frozenset(item.value for item in GateItem)

_GATE_ESCALATIONS: dict[GateItem, EscalationKind | None] = {
    GateItem.COMMITS: EscalationKind.GATE_UNFIXABLE,
    GateItem.NO_OPEN_FINDINGS: None,
    GateItem.REVIEWERS_RAN: EscalationKind.GATE_UNFIXABLE,
    GateItem.TESTS_UNCHANGED: EscalationKind.GATE_UNFIXABLE,
    GateItem.UNTESTED_PATHS: EscalationKind.UNTESTED_CHANGE,
    GateItem.VERIFY: None,
}
