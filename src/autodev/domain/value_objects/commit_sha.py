from __future__ import annotations

import re

from .base import Text


class CommitSha(Text):
    PATTERN = re.compile(r"[0-9a-f]{40}")
