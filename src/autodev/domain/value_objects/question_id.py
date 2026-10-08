from __future__ import annotations

import re

from .base import Text


class QuestionId(Text):
    PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")
