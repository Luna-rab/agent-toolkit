"""`agent-toolkit hook <イベント> --agent <エージェント>` を、stdin から stdout まで通しで確かめる。

stop / subagent-stop は、一時ディレクトリに transcript と編集後のファイルを置いて流す。
Codex の transcript と patch の読み方は `tests/turnreview/test_adapter_codex.py` が確かめる。
"""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

from toolkit import cli, hooks

SKIP_COMMENTS = "CLAUDE_SKIP_COMMENT_REVIEW"
SKIP_TESTS = "CLAUDE_SKIP_TEST_REVIEW"
COMMENT_SECTION = "コードのコメント（"
TEST_SECTION = "足したテスト（"
REPEAT_REPORT = "最終報告をもう一度そのまま書いてください"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """報告済みの記録を tmp_path に閉じ込め、環境の skip 指定と幅を固定する。"""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "state"))
    monkeypatch.delenv(SKIP_COMMENTS, raising=False)
    monkeypatch.delenv(SKIP_TESTS, raising=False)
    monkeypatch.setenv("COLUMNS", "120")


def run_hook(monkeypatch, capsys, event: str, agent: str, stdin: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = hooks.main([event, "--agent", agent])
    return code, capsys.readouterr().out


def plain(text: str) -> str:
    """色（ANSI）と枠の罫線を落とし、行ごとに前後の空白を詰める。"""
    lines = (re.sub(r"\x1b\[[0-9;]*m", "", line).strip("│╭╮╰╯─ ") for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def flat(text: str) -> str:
    """色・枠・折り返しを落として 1 本にする。折り返しをまたぐ語も `in` で比べられる。"""
    return "".join(plain(text).split())


# ---- Claude の transcript ----------------------------------------------------


def user_says(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def edited(name: str, **tool_input) -> dict:
    block = {"type": "tool_use", "name": name, "input": tool_input}
    return {"type": "assistant", "message": {"role": "assistant", "content": [block]}}


def put(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def claude_payload(root: Path, entries: list[dict], **extra) -> str:
    transcript = root / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    payload = {
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
        "cwd": str(root),
        "session_id": "s",
    }
    return json.dumps(payload | extra)


def claude_edit_of_comment(root: Path) -> list[dict]:
    path = put(root, "a.py", "# 説明コメント\nx = 1\n")
    return [
        user_says("go"),
        edited(
            "Edit", file_path=str(path), old_string="x = 1\n", new_string="# 説明コメント\nx = 1\n"
        ),
    ]


def claude_edit_of_comment_and_test(root: Path) -> list[dict]:
    source = "# 説明コメント\ndef test_x():\n    assert True\n"
    path = put(root, "test_a.py", source)
    return [user_says("go"), edited("Write", file_path=str(path), content=source)]


def test_claudeのStopはadditionalContextにパスとコメントを入れて返す(tmp_path, monkeypatch, capsys):
    stdin = claude_payload(tmp_path, claude_edit_of_comment(tmp_path))
    code, out = run_hook(monkeypatch, capsys, "stop", "claude", stdin)
    assert code == 0
    output = json.loads(out)["hookSpecificOutput"]
    assert output["hookEventName"] == "Stop"
    assert "a.py" in plain(output["additionalContext"])
    assert "説明コメント" in plain(output["additionalContext"])
    assert "decision" not in json.loads(out)


def test_claudeの相対パスはcwdから引く(tmp_path, monkeypatch, capsys):
    put(tmp_path, "src/b.py", "# 相対で書いた\nx = 1\n")
    entries = [
        user_says("go"),
        edited("Write", file_path="src/b.py", content="# 相対で書いた\nx = 1\n"),
    ]
    code, out = run_hook(monkeypatch, capsys, "stop", "claude", claude_payload(tmp_path, entries))
    assert code == 0
    assert "src/b.py" in flat(json.loads(out)["hookSpecificOutput"]["additionalContext"])


def test_コメントとテストの見直しは1回の出力にまとまる(tmp_path, monkeypatch, capsys):
    stdin = claude_payload(tmp_path, claude_edit_of_comment_and_test(tmp_path))
    code, out = run_hook(monkeypatch, capsys, "stop", "claude", stdin)
    assert code == 0
    assert len(out.strip().splitlines()) == 1
    body = plain(json.loads(out)["hookSpecificOutput"]["additionalContext"])
    assert COMMENT_SECTION in body
    assert TEST_SECTION in body
    assert "test_x" in body


def test_CLAUDE_SKIP_COMMENT_REVIEWならテストの見直しだけが出る(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(SKIP_COMMENTS, "1")
    stdin = claude_payload(tmp_path, claude_edit_of_comment_and_test(tmp_path))
    _, out = run_hook(monkeypatch, capsys, "stop", "claude", stdin)
    body = plain(json.loads(out)["hookSpecificOutput"]["additionalContext"])
    assert TEST_SECTION in body
    assert COMMENT_SECTION not in body


def test_CLAUDE_SKIP_TEST_REVIEWならコメントの見直しだけが出る(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(SKIP_TESTS, "1")
    stdin = claude_payload(tmp_path, claude_edit_of_comment_and_test(tmp_path))
    _, out = run_hook(monkeypatch, capsys, "stop", "claude", stdin)
    body = plain(json.loads(out)["hookSpecificOutput"]["additionalContext"])
    assert COMMENT_SECTION in body
    assert TEST_SECTION not in body


def test_両方をskipすると何も出ない(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(SKIP_COMMENTS, "1")
    monkeypatch.setenv(SKIP_TESTS, "1")
    stdin = claude_payload(tmp_path, claude_edit_of_comment_and_test(tmp_path))
    assert run_hook(monkeypatch, capsys, "stop", "claude", stdin) == (0, "")


def test_subagent_stopはagent_transcript_pathを読み末尾に書き直しの一文を足す(
    tmp_path, monkeypatch, capsys
):
    parent = put(tmp_path, "parent.py", "# 親が足した\nx = 1\n")
    child = put(tmp_path, "child.py", "# 子が足した\ny = 2\n")
    parent_log = tmp_path / "parent.jsonl"
    parent_log.write_text(
        json.dumps(user_says("go"))
        + "\n"
        + json.dumps(edited("Write", file_path=str(parent), content=parent.read_text()))
        + "\n",
        encoding="utf-8",
    )
    entries = [
        user_says("サブエージェントへの指示"),
        edited("Write", file_path=str(child), content=child.read_text()),
    ]
    stdin = claude_payload(
        tmp_path,
        entries,
        hook_event_name="SubagentStop",
        transcript_path=str(parent_log),
        agent_transcript_path=str(tmp_path / "transcript.jsonl"),
    )
    code, out = run_hook(monkeypatch, capsys, "subagent-stop", "claude", stdin)
    assert code == 0
    output = json.loads(out)["hookSpecificOutput"]
    assert output["hookEventName"] == "SubagentStop"
    body = flat(output["additionalContext"])
    assert "child.py" in body
    assert "parent.py" not in body
    assert REPEAT_REPORT in body
    assert body.index(REPEAT_REPORT) > body.index("子が足した")


def test_stopでは書き直しの一文を足さない(tmp_path, monkeypatch, capsys):
    stdin = claude_payload(tmp_path, claude_edit_of_comment(tmp_path))
    _, out = run_hook(monkeypatch, capsys, "stop", "claude", stdin)
    assert REPEAT_REPORT not in flat(json.loads(out)["hookSpecificOutput"]["additionalContext"])


def test_この_turnより前の編集とBash越しの書き換えは対象外(tmp_path, monkeypatch, capsys):
    path = put(tmp_path, "a.py", "# 前の turn のコメント\nx = 1\n")
    put(tmp_path, "b.py", "# heredoc で書いたコメント\nx = 1\n")
    entries = [
        user_says("first"),
        edited("Write", file_path=str(path), content=path.read_text()),
        user_says("second"),
        edited("Bash", command="cat > b.py <<'EOF'\n# heredoc で書いたコメント\nEOF"),
    ]
    assert run_hook(monkeypatch, capsys, "stop", "claude", claude_payload(tmp_path, entries)) == (
        0,
        "",
    )


# ---- どちらのエージェントでも静かに終わる場合 -------------------------------


def codex_payload(root: Path, **extra) -> str:
    """a.py に `# 説明コメント` を足した Codex の transcript を指す stdin。"""
    put(root, "a.py", "# 説明コメント\nx = 1\n")
    patch = "*** Begin Patch\n*** Add File: a.py\n+# 説明コメント\n+x = 1\n*** End Patch"
    lines = [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "go"}},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch},
        },
    ]
    transcript = root / "rollout.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in lines) + "\n", encoding="utf-8")
    payload = {
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
        "cwd": str(root),
        "session_id": "s",
    }
    return json.dumps(payload | extra)


def stdin_for(agent: str, root: Path, **extra) -> str:
    if agent == "codex":
        return codex_payload(root, **extra)
    return claude_payload(root, claude_edit_of_comment(root), **extra)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_stdinの中身と見直しが出ることの土台(agent, tmp_path, monkeypatch, capsys):
    """以降の「何も出ない」テストが、土台の stdin では出力があることを先に確かめる。"""
    code, out = run_hook(monkeypatch, capsys, "stop", agent, stdin_for(agent, tmp_path))
    assert code == 0
    assert out != ""


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_stop_hook_activeが真なら何も出さない(agent, tmp_path, monkeypatch, capsys):
    stdin = stdin_for(agent, tmp_path, stop_hook_active=True)
    assert run_hook(monkeypatch, capsys, "stop", agent, stdin) == (0, "")


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", "null"])
def test_stdinがJSONの辞書でなければ何も出さず0で終わる(agent, stdin, monkeypatch, capsys):
    assert run_hook(monkeypatch, capsys, "stop", agent, stdin) == (0, "")


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("path", [None, "missing.jsonl"])
def test_transcript_pathがnullか存在しないなら何も出さず0で終わる(
    agent, path, tmp_path, monkeypatch, capsys
):
    stdin = json.dumps(
        {
            "hook_event_name": "Stop",
            "transcript_path": None if path is None else str(tmp_path / path),
            "cwd": str(tmp_path),
            "session_id": "s",
        }
    )
    assert run_hook(monkeypatch, capsys, "stop", agent, stdin) == (0, "")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_同じセッションの同じ内容は2回目に出さない(agent, tmp_path, monkeypatch, capsys):
    stdin = stdin_for(agent, tmp_path)
    assert run_hook(monkeypatch, capsys, "stop", agent, stdin)[1] != ""
    assert run_hook(monkeypatch, capsys, "stop", agent, stdin) == (0, "")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_別のセッションなら同じ内容でも出す(agent, tmp_path, monkeypatch, capsys):
    stdin = stdin_for(agent, tmp_path)
    assert run_hook(monkeypatch, capsys, "stop", agent, stdin)[1] != ""
    other = json.dumps(json.loads(stdin) | {"session_id": "other"})
    assert run_hook(monkeypatch, capsys, "stop", agent, other)[1] != ""


# ---- コマンドの入口 ----------------------------------------------------------


@pytest.mark.parametrize("argv", [["--help"], ["hook", "--help"]])
def test_helpは終了コード0で終わる(argv):
    with pytest.raises(SystemExit) as exited:
        cli.main(argv)
    assert exited.value.code == 0


def test_agent_toolkitのhookサブコマンドはhooksと同じ出力を返す(tmp_path, monkeypatch, capsys):
    stdin = claude_payload(tmp_path, claude_edit_of_comment(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert cli.main(["hook", "stop", "--agent", "claude"]) == 0
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["hookEventName"] == "Stop"
    assert "説明コメント" in plain(output["additionalContext"])
