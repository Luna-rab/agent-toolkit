from __future__ import annotations

import re

from .base import Text


class Repository(Text):
    """対象リポジトリの絶対パス。"""

    PATTERN = re.compile(r"/.*")
