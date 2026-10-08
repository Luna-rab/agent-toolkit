"""StallPolicy: 停滞の判定。

レビューの体数は決めない（ReviewLoop の reviewers でタスク統括が選ぶ）。停滞を見るのは、判定を
締めた後だけ（RecordJudgement・RecordGateResult）で、修正を数える時点（CountFix）では見ない。数えた
時点で見ると、判定で閉じるはずの指摘まで停滞として上がる。
"""

from __future__ import annotations

from ..value_objects.finding_status import FindingStatus
from ..value_objects.limits import STALL_AFTER_FIXES


class StallPolicy:
    @staticmethod
    def is_stalled(status: FindingStatus, fixes_received: int, stalled_at: int = 0) -> bool:
        """修正を STALL_AFTER_FIXES 回以上受けても、まだ open の指摘は停滞している。

        `stalled_at` は前に停滞として上げたときの修正の回数。上げた後は、そこから
        STALL_AFTER_FIXES 回の修正を受けるまで停滞にしない。回答を受けて直し始めたばかりの指摘を、
        次の判定ですぐまた上げないためである。
        """
        return status is FindingStatus.OPEN and fixes_received - stalled_at >= STALL_AFTER_FIXES
