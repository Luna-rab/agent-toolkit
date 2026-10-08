"""この turn に足した行の扱い。transcript の読み方はエージェントごとのアダプタが持つ。

**判断の対象は、この turn でエージェント自身が編集ツールで書いた行だけ。** `git diff HEAD` を
見る方式にすると、人が手で書いたものも、前の turn から未コミットで残っているものも
毎回上がってくる。git を使わないので、git リポジトリの外で編集したファイルも対象になる。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path

# 人が書いたものではないので見ない
SKIP_DIR_NAMES = frozenset(
    {
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        ".venv",
        "venv",
        "__pycache__",
        ".git",
    }
)


def is_subagent(payload: dict) -> bool:
    return payload.get("hook_event_name") == "SubagentStop"


def added_lines(old: str, new: str) -> set[str]:
    """new にあって old に無い行（前後の空白を落として比較）。

    old を引くのが要点。引かないと、再インデントやコードの移動で同じ行が old と new の
    両方に載るたびに発火してしまう。同じ内容の行が増えた分だけ数えるので Counter を使う。
    """
    grown = Counter(s.strip() for s in new.splitlines()) - Counter(
        s.strip() for s in old.splitlines()
    )
    grown.pop("", None)
    return set(grown)


def is_scratchpad(path: Path, temp_roots: Iterable[Path]) -> bool:
    """会話が終われば捨てる scratchpad の中か。autodev の worktree は一時ディレクトリの外にある。"""
    return "scratchpad" in path.parts and any(path.is_relative_to(root) for root in temp_roots)


def is_generated(path: Path) -> bool:
    """依存パッケージやビルドの出力の中か。人が書いたものではないので見ない。"""
    return bool(SKIP_DIR_NAMES.intersection(path.parts))


def shown_path(path: Path, cwd: Path) -> str:
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


def add_lines(
    result: dict[Path, set[str]],
    raw_path: str,
    lines: set[str],
    cwd: Path,
    temp_roots: Iterable[Path],
) -> None:
    """`raw_path`（相対なら cwd から引く）に足した行を result へ集める。scratchpad は除く。"""
    if not raw_path:
        return
    path = Path(raw_path)
    if not path.is_absolute():
        path = cwd / path
    if lines and not is_scratchpad(path, tuple(temp_roots)):
        result.setdefault(path, set()).update(lines)
