"""反応と実行器が書き出す、ランディレクトリのファイル。

どれも一時ファイルに書いてから置き換え、2 回書いても同じ中身になる（反応は
落ちた後にもう一度呼ばれることがある）。書くかどうかはイベントが決めていて、ここは書くだけ。

| ファイル | 書く者 | 読む者 |
| --- | --- | --- |
| `questions/<QuestionId>.json` | 反応（QuestionPosted・QuestionAnswered・QuestionWithdrawn） | `/autodev`（Monitor） |
| `answers/<tool_use_id>.json` | 反応（ask の EscalationResolved） | ガードのフック（在るかだけ）・`autodev ask` |
| `design/v<版>.md` | 実行器（Plan・Replan・Revise の結果の本文） | ステージと統括 |
| `design/v<版>.md` の末尾 | 反応（DesignSettled.appendix） | ステージと統括 |
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from ...adapters.claude.guard import answer_path
from ...domain.value_objects.design_version import DesignVersion
from ...domain.value_objects.event_id import EventId
from ...domain.value_objects.finding_summary import FindingSummary
from ...domain.value_objects.question_id import QuestionId
from ...infra.files import write_atomic
from ...infra.paths import RunPaths

#: 設計ファイルの末尾に書き足した所の頭。ここから後ろは、書き足すたびに丸ごと書き直す
APPENDIX_MARKER = "<!-- autodev:appendix -->"


def write_design(paths: RunPaths, version: DesignVersion, body: str) -> Path:
    """設計の提案の本文を `design/v<版>.md` に書く。版は消さないので、書いた版は書き換えない。"""
    path = paths.design(version)
    write_atomic(path, body if body.endswith("\n") else body + "\n")
    return path


def append_appendix(
    paths: RunPaths, version: DesignVersion, findings: Sequence[FindingSummary]
) -> None:
    """確定した設計の、must-fix 以外の指摘を末尾に書き足す。

    前に書き足した所（`APPENDIX_MARKER` から後ろ）は書き直す。2 回呼ばれても 2 回足さない。
    """
    path = paths.design(version)
    body = path.read_text(encoding="utf-8") if path.is_file() else ""
    body = body.split(APPENDIX_MARKER, 1)[0].rstrip("\n")
    if not findings:
        write_atomic(path, body + "\n")
        return
    lines = [
        APPENDIX_MARKER,
        "",
        "## 設計レビューで残した指摘（must-fix 以外）",
        "",
        "確定した時点で開いていた指摘。設計を直さずに確定したので、実装の判断の参考にする。",
        "",
    ]
    for finding in findings:
        where = f"（{finding.location}）" if finding.location is not None else ""
        lines.append(f"- {finding.finding} [{finding.rating.value}]{where}: {finding.body}")
    write_atomic(path, f"{body}\n\n" + "\n".join(lines) + "\n")


def write_question(
    paths: RunPaths,
    question: QuestionId,
    body: str,
    escalation: EventId | None,
    answer: str | None = None,
    *,
    withdrawn: str | None = None,
) -> Path:
    """`/autodev` に渡す質問。回答が届いたら回答を、取り下げたら理由（`withdrawn`）を足して書き直す。"""
    status = "open" if answer is None else "answered"
    content: dict[str, str | None] = {
        "question": question.value,
        "body": body,
        "escalation": escalation.value if escalation is not None else None,
        "status": "withdrawn" if withdrawn is not None else status,
        "answer": answer,
    }
    if withdrawn is not None:
        # 経路のエスカレーションを閉じた理由。/autodev はこの質問をユーザーに聞かなくてよい
        content["reason"] = withdrawn
    path = paths.question(question)
    write_atomic(path, json.dumps(content, ensure_ascii=False, indent=2) + "\n")
    return path


def write_answer(paths: RunPaths, tool_use_id: str, answer: str) -> Path:
    """defer で止まった ask への回答。フックは在るかだけを見て、`autodev ask` が中身を読む。"""
    path = answer_path(paths.answers, tool_use_id)
    write_atomic(path, json.dumps({"answer": answer}, ensure_ascii=False) + "\n")
    return path
