from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .write_scope import WriteScope

if TYPE_CHECKING:
    from ..guard import AskVerdict, Refusal, WriteTarget


@dataclass(frozen=True)
class Guard:
    """ステージの種類ごとの書き込みの範囲と権限。

    止めるのは、worktree の中の書き込み範囲を外れた書き込みと、場所の分からない書き込みだけである。
    worktree の外への書き込みと `gh`・`git push` は、フックでは止めず、最初の指示で禁じる。
    規則は `domain/guard.py` にあり、下のメソッドが呼ぶ。
    決定的なステージは claude を起動しないので、Guard を持たない。
    """

    #: worktree の中で書いてよい範囲
    writes: WriteScope
    #: 指摘の状態を動かせるか（JudgeCapability。Judge・DesignJudge）
    judge: bool = False
    #: 設計ファイルを渡すか。AdversarialReview には渡さない
    reads_design: bool = True
    #: ask で聞けるか（計画ステージ。PreToolUse のフックの defer で止める）
    can_ask: bool = False

    # 規則の置き場の domain/guard.py が Guard を import するので、循環を避けて呼ぶときに import する

    def judge_write(self, target: WriteTarget) -> Refusal | None:
        """書き込みの宛先 1 つを止めるか。止めるなら理由。"""
        from ..guard import judge_write  # noqa: PLC0415

        return judge_write(self, target)

    def judge_ask(self, answered: bool) -> AskVerdict:
        from ..guard import judge_ask  # noqa: PLC0415

        return judge_ask(self, answered)
