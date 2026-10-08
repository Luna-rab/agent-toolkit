from __future__ import annotations

from dataclasses import dataclass

from .union_file_verdict import UnionFileVerdict


@dataclass(frozen=True)
class UnionVerdict:
    files: tuple[UnionFileVerdict, ...]

    @property
    def passed(self) -> bool:
        return all(file.kept_both for file in self.files)
