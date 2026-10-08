from __future__ import annotations

from enum import Enum


class FlowEnding(Enum):
    """git 管理タスクのフローの終わり方（FinishGitJob）。ポリシーが FlowFinished・FlowAbandoned から写す。"""

    FINISHED = "finished"
    ABANDONED = "abandoned"
