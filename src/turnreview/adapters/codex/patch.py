"""apply_patch のパッチ本文を読む。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

BEGIN = "*** Begin Patch"
END = "*** End Patch"
ADD = "*** Add File:"
UPDATE = "*** Update File:"
DELETE = "*** Delete File:"
MOVE = "*** Move to:"


@dataclass(frozen=True)
class PatchedFile:
    path: str
    action: Literal["add", "update", "delete"]
    added: tuple[str, ...]
    removed: tuple[str, ...]


class _Open:
    """読んでいる途中のファイル。"""

    def __init__(self, path: str, action: Literal["add", "update", "delete"]) -> None:
        self.path = path
        self.action = action
        self.added: list[str] = []
        self.removed: list[str] = []

    def done(self) -> PatchedFile:
        return PatchedFile(self.path, self.action, tuple(self.added), tuple(self.removed))


def parse_patch(text: str) -> list[PatchedFile]:
    """`*** Begin Patch` から `*** End Patch` までを読む。崩れていても読めた分を返す。"""
    found: list[PatchedFile] = []
    current: _Open | None = None
    inside = False
    for line in text.splitlines():
        if not inside:
            inside = line.strip() == BEGIN
            continue
        if line.strip() == END:
            break
        header = _header(line)
        if header is not None:
            if current is not None:
                found.append(current.done())
            current = header
        elif line.startswith(MOVE):
            target = line[len(MOVE) :].strip()
            if current is not None and target:
                current.path = target
        elif current is not None and current.action != "delete":
            if line.startswith("+"):
                current.added.append(line[1:])
            elif line.startswith("-") and current.action == "update":
                current.removed.append(line[1:])
    if current is not None:
        found.append(current.done())
    return found


def _header(line: str) -> _Open | None:
    if line.startswith(ADD):
        return _Open(line[len(ADD) :].strip(), "add")
    if line.startswith(UPDATE):
        return _Open(line[len(UPDATE) :].strip(), "update")
    if line.startswith(DELETE):
        return _Open(line[len(DELETE) :].strip(), "delete")
    return None
