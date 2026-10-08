"""UnionChecker: 衝突したファイルで、両側の変更を残したか。

共通の祖先・A 側・B 側・解いた結果の 4 つの本文から、次の 3 つを見る。

- 残す: A 側が足した行・B 側が足した行・祖先にあって A 側も B 側も消していない行が、解いた結果に
  すべてある（`missing`）
- 戻さない: 片方だけが消した行（もう片方が足し直していないもの）が、解いた結果に戻っていない
  （`revived`）。片方の書き換え・削除を、解いた結果が元に戻していないかを見る
- 解き終える: 衝突の印が増えていない（`markers`）

行は多重集合で数え、並びは見ない。import を並べ直す・足した 2 つの塊の順を入れ替える、のような
意味の変わらない解き方を落とさないためである。並びや構文が壊れたかは、この後の統合検査が見る。

比べる前に、行末の空白を落とし、空白だけの行を数えない。2 つの塊を末尾に足すと間の空行の数が
解き方で変わるが、それは両側の変更を残したかとは関係が無い。

**割り切り: 両側が同じ行を足したら、1 つ残せば通す。** 同じ import を両側が足したとき、1 つに
まとめる正しい解き方を落とさないためである。そのかわり、両側が別々の塊に同じ行（よくある
`    assert response.ok` など）を足して、解いた結果がそれを 1 つしか残していなくても見逃す。
塊のほかの行が残っていれば塊は残ったと見なせるので、見逃すのは同じ行だけでできた塊を片方
落としたときに限られる。

衝突の印（`<<<<<<<`・`|||||||`・`>>>>>>>` で始まる行と、`=======` だけの行）は、解いた結果に現れる
回数が、祖先・A 側・B 側のうち一番多い回数を超えた分だけを数える。Markdown の見出しの下線や
テストの入力のように、元から `=======` を持つファイルを落とさないためである。

**本文はバイト列で受ける**（`adapters/github/git.py` の `conflict_sides`・`read_file` が返すまま）。無い
ファイルは None で、空として見る。4 つのどれかが UTF-8 として読めないか、NUL を含む（バイナリ）なら、
行で比べられないので「意味が変わる統合」として通さない（`unreadable`）。読めない字を置き換えて
比べると、置き換えた字どうしが同じ行に見え、片方の変更を落としても通ってしまう。
"""

from __future__ import annotations

from collections import Counter

from ..value_objects.union_file_verdict import UnionFileVerdict

#: 行で比べられない本文の印。git も NUL を含むファイルをバイナリとして扱う
_BINARY_MARK = b"\0"

_MARKER_PREFIXES = ("<<<<<<<", "|||||||", ">>>>>>>")
_MARKER_LINE = "======="


def _lines(text: str) -> Counter[str]:
    return Counter(line.rstrip() for line in text.splitlines() if line.strip())


def _markers(text: str) -> Counter[str]:
    return Counter(
        line.rstrip()
        for line in text.splitlines()
        if line.startswith(_MARKER_PREFIXES) or line.rstrip() == _MARKER_LINE
    )


def _expand(counts: Counter[str], order: Counter[str]) -> tuple[str, ...]:
    """`counts` の行を、`order` に現れた順に、数の分だけ並べる。"""
    return tuple(line for line in order if line in counts for _ in range(counts[line]))


def _text(content: bytes | None) -> str | None:
    """行で比べられる本文。読めない（UTF-8 でない・バイナリ）なら None。無いファイルは空。"""
    if content is None:
        return ""
    if _BINARY_MARK in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


class UnionChecker:
    @staticmethod
    def check(
        path: str,
        base: bytes | None,
        ours: bytes | None,
        theirs: bytes | None,
        resolved: bytes | None,
    ) -> UnionFileVerdict:
        """`base` は共通の祖先、`ours`・`theirs` は両側、`resolved` は解いた結果。無いファイルは None。

        3 つとも None なら、index に衝突の段が無い（解きかけを `git add` した）。両側の中身が分からない
        ので、読めないものとして通さない。
        """
        if base is None and ours is None and theirs is None:
            return UnionFileVerdict(path=path, kept_both=False, unreadable=True)
        texts = [_text(content) for content in (base, ours, theirs, resolved)]
        readable = [text for text in texts if text is not None]
        if len(readable) != len(texts):
            return UnionFileVerdict(path=path, kept_both=False, unreadable=True)
        return UnionChecker._check_text(path, *readable)

    @staticmethod
    def still_conflicted(path: str) -> UnionFileVerdict:
        """rebase を続けた先のコミットで、また衝突したファイル。解いていないので、両側を残していない。"""
        return UnionFileVerdict(path=path, kept_both=False)

    @staticmethod
    def _check_text(
        path: str, base: str, ours: str, theirs: str, resolved: str
    ) -> UnionFileVerdict:
        base_lines, ours_lines, theirs_lines = _lines(base), _lines(ours), _lines(theirs)
        resolved_lines = _lines(resolved)
        # Counter の & は要素ごとの小さい方、| は大きい方、- は 0 未満を落とした差、+ は和
        kept_by_both = base_lines & ours_lines & theirs_lines
        added = (ours_lines - base_lines) | (theirs_lines - base_lines)
        required = kept_by_both + added
        missing = _expand(required - resolved_lines, required)
        # 片方でも消した行は、残す数（required）までしか解いた結果にあってはならない
        deleted = (base_lines - ours_lines) | (base_lines - theirs_lines)
        revived = _expand(
            Counter(
                {line: n for line, n in (resolved_lines - required).items() if line in deleted}
            ),
            resolved_lines,
        )
        original = _markers(base) | _markers(ours) | _markers(theirs)
        resolved_markers = _markers(resolved)
        markers = _expand(resolved_markers - original, resolved_markers)
        return UnionFileVerdict(
            path=path,
            kept_both=not missing and not revived and not markers,
            missing=missing,
            revived=revived,
            markers=markers,
        )
