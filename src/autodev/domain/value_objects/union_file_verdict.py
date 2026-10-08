from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UnionFileVerdict:
    """衝突したファイル 1 つで、両側が足した行を残し、どちらも消していない行を消していないか。

    片方だけが消した行が戻っていても、衝突の印（`<<<<<<<` など）が残っていても、`kept_both` は
    偽にする。
    """

    path: str
    kept_both: bool
    #: 残っていなければならないのに、解いた結果に無い行
    missing: tuple[str, ...] = ()
    #: 片方が消したのに（もう片方は足し直していない）、解いた結果に戻った行
    revived: tuple[str, ...] = ()
    #: 解いた結果に増えた衝突の印の行（元のファイルにあった数を超えた分）
    markers: tuple[str, ...] = ()
    #: どれかの本文が UTF-8 として読めないかバイナリで、行で比べられなかった（意味が変わる統合とする）
    unreadable: bool = False
