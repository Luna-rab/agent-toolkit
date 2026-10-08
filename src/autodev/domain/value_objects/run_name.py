from __future__ import annotations

import re

from .base import Text


class RunName(Text):
    """ラン名。ブランチ名と置き場のパスに入るので、英小文字・数字・`-` の 1〜49 字で、先頭は `-` でない。

    `--` と末尾の `-` も拒む。ブランチ名の `stack/<ラン名>--task-<番号>` で、`--` がラン名と
    タスクの区切りになっているため。
    """

    # `-` は英小文字か数字が続くときだけ置ける
    PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,48}")
