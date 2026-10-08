"""`turnreview.core.report` の報告済みの鍵を、ファイルを置かずに確かめる。

Claude の transcript の読み方（turn の区切り・Edit/Write/MultiEdit・サブエージェントの
transcript）と出力の形は、`test_app_comments.py` と `tests/toolkit/test_toolkit_hooks.py` が
`agent-toolkit hook` の入口から確かめる。
"""

from __future__ import annotations

from turnreview.core.report import report_key


def test_報告済みの鍵は本文が変われば変わる():
    assert report_key("line", "a.py", "# x") == report_key("line", "a.py", "# x")
    assert report_key("line", "a.py", "# x") != report_key("line", "a.py", "# y")
