"""Codex のアダプタ（apply_patch の本文と rollout の transcript）を確かめる。

`parse_patch` は直に呼ぶ。transcript は一時ディレクトリに作り、`agent-toolkit hook stop --agent codex`
（`toolkit.hooks.main`）の入口から流す。
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

import pytest
from turnreview_screen import plain

from toolkit import hooks
from turnreview.adapters.codex.patch import PatchedFile, parse_patch

# ---- parse_patch ---------------------------------------------------------------

MIXED = (
    "*** Begin Patch\n"
    "*** Add File: a.py\n"
    "+x = 1\n"
    "+# note\n"
    "*** Update File: b.py\n"
    "@@\n"
    "-old\n"
    "+new\n"
    " ctx\n"
    "*** Delete File: c.py\n"
    "*** End Patch"
)


def test_追加と更新と削除を順に返す():
    assert parse_patch(MIXED) == [
        PatchedFile("a.py", "add", ("x = 1", "# note"), ()),
        PatchedFile("b.py", "update", ("new",), ("old",)),
        PatchedFile("c.py", "delete", (), ()),
    ]


def test_Move_toがあればpathは移動先になる():
    text = (
        "*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n@@\n-a\n+b\n*** End Patch"
    )
    assert parse_patch(text) == [PatchedFile("new.py", "update", ("b",), ("a",))]


def test_更新の文脈行と_atat行は数えない():
    text = (
        "*** Begin Patch\n"
        "*** Update File: b.py\n"
        "@@ def f():\n"
        " keep\n"
        "-gone\n"
        "+came\n"
        " keep too\n"
        "@@\n"
        "+second hunk\n"
        "*** End Patch"
    )
    assert parse_patch(text) == [PatchedFile("b.py", "update", ("came", "second hunk"), ("gone",))]


def test_複数の更新hunkをまたいで足した行と消した行を集める():
    text = (
        "*** Begin Patch\n*** Update File: a.py\n@@\n-1\n+2\n@@\n-3\n+4\n"
        "*** Add File: b.py\n+5\n*** End Patch"
    )
    assert parse_patch(text) == [
        PatchedFile("a.py", "update", ("2", "4"), ("1", "3")),
        PatchedFile("b.py", "add", ("5",), ()),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain text with no markers\n+not a patch line",
        "+x = 1\n+y = 2",
        "*** Update File: a.py\n+x\n",
        "*** End Patch",
    ],
    ids=["空", "ただの文", "Begin が無い 1", "Begin が無い 2", "End だけ"],
)
def test_Begin_Patchの無い文字列は例外を出さず何も返さない(text):
    assert parse_patch(text) == []


def test_Begin_Patchの前と_End_Patchの後は読まない():
    text = (
        "+before\n*** Begin Patch\n*** Add File: a.py\n+inside\n*** End Patch\n"
        "*** Add File: b.py\n+after"
    )
    assert parse_patch(text) == [PatchedFile("a.py", "add", ("inside",), ())]


@pytest.mark.parametrize(
    "text",
    [
        "*** Begin Patch\n*** Add File: a.py\n+x = 1\n+# half",
        "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+ne",
        "*** Begin Patch\n*** Add File: ",
        "*** Begin Patch\n",
        "*** Begin Patch\n*** Move to: new.py\n+orphan\n*** End Patch",
        "*** Begin Patch\n*** Bogus: x\n+y\n*** End Patch",
    ],
    ids=["追加の途中", "更新の途中", "パスが空", "Begin だけ", "Move to が単独", "知らない行"],
)
def test_途中で切れた本文や崩れた本文でも例外を出さない(text):
    found = parse_patch(text)
    assert isinstance(found, list)
    assert all(isinstance(item, PatchedFile) for item in found)


def test_途中で切れた追加でも読めた分を返す():
    found = parse_patch("*** Begin Patch\n*** Add File: a.py\n+x = 1\n+# half")
    assert found == [PatchedFile("a.py", "add", ("x = 1", "# half"), ())]


# ---- Codex の transcript ----------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "state"))
    monkeypatch.delenv("CLAUDE_SKIP_COMMENT_REVIEW", raising=False)
    monkeypatch.delenv("CLAUDE_SKIP_TEST_REVIEW", raising=False)
    monkeypatch.setenv("COLUMNS", "120")


def user_message(text: str = "go") -> dict:
    return {"type": "event_msg", "payload": {"type": "user_message", "message": text}}


def injected_user_item(text: str = "# AGENTS.md instructions") -> dict:
    """Codex が環境の説明や AGENTS.md を差し込む user の response_item。turn の区切りではない。"""
    content = [{"type": "input_text", "text": text}]
    payload = {"type": "message", "role": "user", "content": content}
    return {"type": "response_item", "payload": payload}


def custom_tool_call(patch: str) -> dict:
    payload = {"type": "custom_tool_call", "name": "apply_patch", "input": patch}
    return {"type": "response_item", "payload": payload}


def function_call_apply_patch(patch: str) -> dict:
    payload = {
        "type": "function_call",
        "name": "apply_patch",
        "arguments": json.dumps({"input": patch}),
    }
    return {"type": "response_item", "payload": payload}


def function_call_shell(patch: str) -> dict:
    payload = {
        "type": "function_call",
        "name": "shell",
        "arguments": json.dumps({"command": ["apply_patch", patch]}),
    }
    return {"type": "response_item", "payload": payload}


def put(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def stop(monkeypatch, capsys, root: Path, entries: list[dict], **extra) -> dict | None:
    """Codex の Stop として流し、stdout の JSON（無ければ None）を返す。"""
    transcript = root / "rollout.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    payload = {
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
        "cwd": str(root),
        "session_id": extra.pop("session", "s"),
    } | extra
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hooks.main(["stop", "--agent", "codex"]) == 0
    out = capsys.readouterr().out
    return json.loads(out) if out else None


def add_file_patch(name: str, *body: str) -> str:
    lines = "".join(f"+{line}\n" for line in body)
    return f"*** Begin Patch\n*** Add File: {name}\n{lines}*** End Patch"


COMMENT_AND_TEST = ("# 説明コメント", "def test_x():", "    assert True")


def test_apply_patchで足したコメントとテストを1回のblockにまとめる(tmp_path, monkeypatch, capsys):
    put(tmp_path, "test_a.py", "\n".join(COMMENT_AND_TEST) + "\n")
    patch = add_file_patch("test_a.py", *COMMENT_AND_TEST)
    result = stop(monkeypatch, capsys, tmp_path, [user_message(), custom_tool_call(patch)])
    assert result is not None
    assert result["decision"] == "block"
    assert "hookSpecificOutput" not in result
    reason = plain(result["reason"])
    assert "test_a.py" in reason
    assert "説明コメント" in reason
    assert "コードのコメント（" in reason
    assert "足したテスト（" in reason
    assert "test_x" in reason


def test_ユーザー発言の後に差し込まれるuserのresponse_itemでは区切らない(
    tmp_path, monkeypatch, capsys
):
    put(tmp_path, "a.py", "# 説明コメント\nx = 1\n")
    patch = add_file_patch("a.py", "# 説明コメント", "x = 1")
    entries = [user_message(), custom_tool_call(patch), injected_user_item()]
    result = stop(monkeypatch, capsys, tmp_path, entries)
    assert result is not None
    assert "説明コメント" in plain(result["reason"])


def test_user_message以外のevent_msgでは区切らない(tmp_path, monkeypatch, capsys):
    put(tmp_path, "a.py", "# 説明コメント\nx = 1\n")
    patch = add_file_patch("a.py", "# 説明コメント", "x = 1")
    other = {"type": "event_msg", "payload": {"type": "agent_message", "message": "書きます"}}
    result = stop(monkeypatch, capsys, tmp_path, [user_message(), other, custom_tool_call(patch)])
    assert result is not None
    assert "説明コメント" in plain(result["reason"])


def test_最後のuser_messageより前の編集しか無ければ何も出さない(tmp_path, monkeypatch, capsys):
    put(tmp_path, "a.py", "# 説明コメント\nx = 1\n")
    patch = add_file_patch("a.py", "# 説明コメント", "x = 1")
    entries = [user_message("first"), custom_tool_call(patch), user_message("second")]
    assert stop(monkeypatch, capsys, tmp_path, entries) is None


def test_最後のuser_messageより後の編集だけを見る(tmp_path, monkeypatch, capsys):
    put(tmp_path, "old.py", "# 前の turn\nx = 1\n")
    put(tmp_path, "new.py", "# 今の turn\nx = 1\n")
    entries = [
        user_message("first"),
        custom_tool_call(add_file_patch("old.py", "# 前の turn", "x = 1")),
        user_message("second"),
        custom_tool_call(add_file_patch("new.py", "# 今の turn", "x = 1")),
    ]
    result = stop(monkeypatch, capsys, tmp_path, entries)
    assert result is not None
    reason = plain(result["reason"])
    assert "new.py" in reason
    assert "old.py" not in reason


@pytest.mark.parametrize(
    "call",
    [custom_tool_call, function_call_apply_patch, function_call_shell],
    ids=["custom_tool_call", "function_call の apply_patch", "function_call の shell"],
)
def test_3つの形のどれでも編集が拾われる(call, tmp_path, monkeypatch, capsys):
    put(tmp_path, "a.py", "# 説明コメント\nx = 1\n")
    patch = add_file_patch("a.py", "# 説明コメント", "x = 1")
    result = stop(monkeypatch, capsys, tmp_path, [user_message(), call(patch)])
    assert result is not None
    assert result["decision"] == "block"
    assert "説明コメント" in plain(result["reason"])


def test_shellがapply_patchでなければ編集とみなさない(tmp_path, monkeypatch, capsys):
    put(tmp_path, "a.py", "# 説明コメント\nx = 1\n")
    arguments = json.dumps({"command": ["bash", "-lc", "cat > a.py <<'EOF'\n# 説明コメント\nEOF"]})
    call = {
        "type": "response_item",
        "payload": {"type": "function_call", "name": "shell", "arguments": arguments},
    }
    assert stop(monkeypatch, capsys, tmp_path, [user_message(), call]) is None


def test_更新で足した行だけを見直す(tmp_path, monkeypatch, capsys):
    put(tmp_path, "a.py", "# 前からあるコメント\n# 足したコメント\nx = 1\n")
    patch = (
        "*** Begin Patch\n*** Update File: a.py\n@@\n"
        " # 前からあるコメント\n+# 足したコメント\n x = 1\n*** End Patch"
    )
    result = stop(monkeypatch, capsys, tmp_path, [user_message(), custom_tool_call(patch)])
    assert result is not None
    reason = plain(result["reason"])
    assert "足したコメント" in reason
    assert "前からあるコメント" not in reason


def test_移動したファイルは移動先のパスで見直す(tmp_path, monkeypatch, capsys):
    put(tmp_path, "new.py", "# 移動で足したコメント\nx = 1\n")
    patch = (
        "*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n@@\n"
        "+# 移動で足したコメント\n x = 1\n*** End Patch"
    )
    result = stop(monkeypatch, capsys, tmp_path, [user_message(), custom_tool_call(patch)])
    assert result is not None
    reason = plain(result["reason"])
    assert "new.py" in reason
    assert "移動で足したコメント" in reason


def test_削除だけのパッチでは何も出さない(tmp_path, monkeypatch, capsys):
    patch = "*** Begin Patch\n*** Delete File: gone.py\n*** End Patch"
    assert stop(monkeypatch, capsys, tmp_path, [user_message(), custom_tool_call(patch)]) is None


def test_相対パスはcwdから引き_絶対パスはそのまま使う(tmp_path, monkeypatch, capsys):
    put(tmp_path, "src/rel.py", "# 相対のコメント\nx = 1\n")
    absolute = put(tmp_path, "abs.py", "# 絶対のコメント\nx = 1\n")
    entries = [
        user_message(),
        custom_tool_call(add_file_patch("src/rel.py", "# 相対のコメント", "x = 1")),
        custom_tool_call(add_file_patch(str(absolute), "# 絶対のコメント", "x = 1")),
    ]
    result = stop(monkeypatch, capsys, tmp_path, entries)
    assert result is not None
    reason = "".join(plain(result["reason"]).split())
    assert "src/rel.py" in reason
    assert "相対のコメント" in reason
    assert "絶対のコメント" in reason


def test_壊れた行が混ざっていても読めた編集を拾う(tmp_path, monkeypatch, capsys):
    put(tmp_path, "a.py", "# 説明コメント\nx = 1\n")
    patch = add_file_patch("a.py", "# 説明コメント", "x = 1")
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(user_message()) + "\n{broken\n" + json.dumps(custom_tool_call(patch)) + "\n",
        encoding="utf-8",
    )
    payload = {"hook_event_name": "Stop", "transcript_path": str(transcript), "cwd": str(tmp_path)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload | {"session_id": "s"})))
    assert hooks.main(["stop", "--agent", "codex"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "説明コメント" in plain(result["reason"])


def test_編集後に消えたファイルは飛ばす(tmp_path, monkeypatch, capsys):
    patch = add_file_patch("gone.py", "# 消えたコメント", "x = 1")
    assert stop(monkeypatch, capsys, tmp_path, [user_message(), custom_tool_call(patch)]) is None
