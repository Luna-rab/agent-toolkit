from __future__ import annotations

from dataclasses import dataclass

from .verify_command import VerifyCommand
from .verify_kind import VerifyKind


@dataclass(frozen=True)
class VerifyResult:
    command: VerifyCommand
    exit_code: int
    #: 出力の末尾
    tail: str = ""
    #: どの種類として流したか。種類を付けずに流した結果は None
    kind: VerifyKind | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0
