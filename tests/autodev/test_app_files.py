"""反応と実行器が書き出すランディレクトリのファイル（`app/stages/files.py`）。2 回書いても同じ中身になる。"""

from __future__ import annotations

import json
from pathlib import Path

from autodev.app.stages.files import (
    APPENDIX_MARKER,
    append_appendix,
    write_answer,
    write_design,
    write_question,
)
from autodev.domain.value_objects.design_version import DesignVersion
from autodev.domain.value_objects.event_id import EventId
from autodev.domain.value_objects.finding_id import FindingId
from autodev.domain.value_objects.finding_summary import FindingSummary
from autodev.domain.value_objects.location import Location
from autodev.domain.value_objects.question_id import QuestionId
from autodev.domain.value_objects.rating import Rating
from autodev.domain.value_objects.run_name import RunName
from autodev.infra.paths import RunPaths

V2 = DesignVersion(2)
NIT = FindingSummary(FindingId("D2"), Rating.NIT, "見出しを揃える", Location("design.md:3"))


def paths(tmp_path: Path) -> RunPaths:
    return RunPaths(RunName("demo"), tmp_path / "demo")


def test_確定した設計の末尾の書き足しは何度呼んでも1回分(tmp_path: Path):
    run = paths(tmp_path)
    write_design(run, V2, "# 設計\n\n本文")
    append_appendix(run, V2, (NIT,))
    once = run.design(V2).read_text(encoding="utf-8")
    append_appendix(run, V2, (NIT,))
    assert run.design(V2).read_text(encoding="utf-8") == once
    assert once.startswith("# 設計\n\n本文\n\n" + APPENDIX_MARKER)
    assert "- D2 [nit]（design.md:3）: 見出しを揃える" in once
    # 書き足すものが無ければ、本文だけに戻す
    append_appendix(run, V2, ())
    assert run.design(V2).read_text(encoding="utf-8") == "# 設計\n\n本文\n"


def test_質問のファイルは回答が届いたら回答を足して書き直す(tmp_path: Path):
    run = paths(tmp_path)
    q = QuestionId("ttl-default")
    write_question(run, q, "A か B か", EventId("run#3"))
    asked = json.loads(run.question(q).read_text(encoding="utf-8"))
    assert asked == {
        "question": "ttl-default",
        "body": "A か B か",
        "escalation": "run#3",
        "status": "open",
        "answer": None,
    }
    write_question(run, q, "A か B か", EventId("run#3"), "A")
    answered = json.loads(run.question(q).read_text(encoding="utf-8"))
    assert (answered["status"], answered["answer"]) == ("answered", "A")


def test_取り下げた質問のファイルは状態と閉じた理由を書き直す(tmp_path: Path):
    run = paths(tmp_path)
    q = QuestionId("ttl-default")
    write_question(run, q, "A か B か", EventId("run#3"))
    write_question(run, q, "A か B か", EventId("run#3"), withdrawn="タスクを止めた")
    withdrawn = json.loads(run.question(q).read_text(encoding="utf-8"))
    assert (withdrawn["status"], withdrawn["answer"], withdrawn["reason"]) == (
        "withdrawn",
        None,
        "タスクを止めた",
    )


def test_askの回答はtool_use_idの名前で回答のディレクトリに書く(tmp_path: Path):
    run = paths(tmp_path)
    path = write_answer(run, "toolu_01", "A にする")
    assert path == run.answer("toolu_01")
    assert json.loads(path.read_text(encoding="utf-8")) == {"answer": "A にする"}
