"""応答を終える前に、この turn で足したテストを 1 本ずつ見直させる。

`CLAUDE_SKIP_TEST_REVIEW=1` で丸ごと止まる（止めるのは `app.turn`）。
"""

from __future__ import annotations

from pathlib import Path

from turnreview.core import testcases
from turnreview.core.report import session_digest
from turnreview.core.review import Pending
from turnreview.core.testcases import FileTests
from turnreview.core.turn import shown_path
from turnreview.ports import files
from turnreview.ports.store import ReportStore
from turnreview.syntax import testcases as syntax


def review(added: dict[Path, set[str]], cwd: Path, agent: str, session_id: str) -> Pending | None:
    candidates: list[FileTests] = []
    for path in sorted(added):
        suite = testcases.suite_of(path)
        lines = files.read_lines(path) if suite else None
        if suite is None or lines is None:
            continue
        declared = syntax.declared(suite, "\n".join(lines))
        candidates.append(FileTests(shown_path(path, cwd), lines, added[path], declared))
    if not candidates:
        return None

    store = ReportStore(f"{agent}-test-review", session_digest(session_id))
    reported = store.load()
    found, fresh = testcases.review_of(candidates, reported)
    if not fresh:
        return None
    return found, lambda: store.save(reported | fresh)
