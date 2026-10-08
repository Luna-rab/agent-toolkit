"""利用枠（5 時間・7 日・月次のクレジット）の使い方が、窓の時間の進みに対して速すぎないか。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limit:
    label: str
    #: 使った割合（0〜100）
    used: float
    #: リセットまでの秒数。Claude Code が `resets_at` を渡さなければ None
    remaining: float | None
    #: 窓の長さ（秒）
    window: int
    #: 金額で数える枠（月次のクレジット）なら、使った額と上限（ドル）
    money: tuple[float, float] | None = None

    @property
    def elapsed_pct(self) -> float | None:
        """窓の時間が、どれだけ過ぎたか（0〜100）。"""
        if self.remaining is None:
            return None
        return (1 - max(0.0, min(self.remaining, self.window)) / self.window) * 100

    @property
    def pace(self) -> int | None:
        """使った割合から、窓の時間が過ぎた割合を引いたもの。正なら、このままでは窓の途中で尽きる。"""
        elapsed = self.elapsed_pct
        return None if elapsed is None else round(self.used - elapsed)


def until(seconds: float) -> str:
    """リセットまでの残り。1 日以上は日と時、それ未満は時と分。"""
    total = max(0, int(seconds))
    if total >= 86400:
        return f"{total // 86400}d{total % 86400 // 3600:02d}h"
    if total >= 3600:
        return f"{total // 3600}h{total % 3600 // 60:02d}m"
    return f"{total // 60}m"
