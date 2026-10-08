from __future__ import annotations

from enum import Enum


class GitJobOutcome(Enum):
    """処理していた仕事の終わり方（GitJobFinished）。Stack が、フローの終わり方と仕事の記録から決める。"""

    #: フローを最後まで終えた
    DONE = "done"
    #: 相手のタスクを止めたので、列から外した（GitJobWithdrawn）
    WITHDRAWN = "withdrawn"
    #: 統合に失敗した（IntegrationFailed）。引き継ぐタスクが新しい仕事として積み直す
    INTEGRATION_FAILED = "integration-failed"
    #: 上のどれでもなくフローを捨てた（ステージが落ち続けて上げたエスカレーションを閉じた など）。
    #: 済ませないとランが終わらない仕事なので、列の先頭へ戻した
    ABANDONED = "abandoned"
    #: 捨てたが、済ませなくてもランが終わる仕事（`GitJobKind.must_finish` が偽）なので消した
    DROPPED = "dropped"
    #: 戻した回数が上限（MAX_JOB_RETURNS）に達した。戻さずに止め、ラン統括が続けるかやめるかを決める
    #: まで、後ろの仕事も取り出さない
    STUCK = "stuck"

    @property
    def returns_to_queue(self) -> bool:
        """仕事を列の先頭へ戻すか（名前の付いた規則）。終えてもいず、やめる理由も無い仕事は消さない。"""
        return self is GitJobOutcome.ABANDONED

    @property
    def keeps_job(self) -> bool:
        """仕事がまだ残っているか（列に戻した・止めてラン統括を待つ）。積む仕事なら相手は積む列で待つ。"""
        return self in (GitJobOutcome.ABANDONED, GitJobOutcome.STUCK)
