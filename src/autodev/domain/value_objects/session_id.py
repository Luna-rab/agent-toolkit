from __future__ import annotations

import re

from .base import Text


class SessionId(Text):
    """claude のセッション id。driver が `--session-id` で決める UUID。"""

    PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
