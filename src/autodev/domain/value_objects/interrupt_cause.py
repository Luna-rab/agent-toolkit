from __future__ import annotations

from enum import Enum


class InterruptCause(Enum):
    """走っていた実行を止めた理由（StageInterrupted）。止めた実行を再開するかは、これで決まる。"""

    #: driver の起動時の後始末。前の driver が走らせていて、誰も見ていない（MarkInterrupted）
    STARTUP = "startup"
    #: パニックで止めた（MarkInterrupted・InterruptStage）
    PANIC = "panic"
    #: タスクを止めた（StopTask）。タスクはもうステージを始めない
    STOPPED = "stopped"
    #: フローを捨てた（AbandonFlow）・ポリシーが止めた（InterruptStage）。そのフローは続けない
    REQUESTED = "requested"

    @property
    def resumes_on_restart(self) -> bool:
        """呼び直したとき（RunResumed）に、同じ実行を続きから再開するか。

        止めたのが driver の都合（落ちた・パニック）なら再開する。タスクやフローを止めると決めて
        止めたなら再開しない。
        """
        return self in (InterruptCause.STARTUP, InterruptCause.PANIC)
