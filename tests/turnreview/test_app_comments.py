"""コメントの hook（`turnreview.app.comments`）を payload から本文まで通しで確かめる。

hook は transcript（会話の JSONL）の Edit / Write / MultiEdit の入力を見るので、
一時ディレクトリに transcript と編集後のファイルを両方置いてから
`agent-toolkit hook stop --agent claude`（`toolkit.hooks.main`）を呼ぶ。
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

import pytest
from turnreview_screen import plain

from toolkit import hooks as toolkit_hooks
from turnreview.app import comments
from turnreview.app import turn as app_turn


@pytest.fixture
def hook():
    return comments


def user_says(text: str, *, meta: bool = False) -> dict:
    entry: dict = {"type": "user", "message": {"role": "user", "content": text}}
    if meta:
        entry["isMeta"] = True
    return entry


def tool_result() -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    }


def tool_use(name: str, **inp) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": inp}],
        },
    }


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def wrote(root: Path, name: str, text: str) -> tuple[Path, dict]:
    """ファイルを置き、それを Write で書いたことにする transcript のエントリを返す。"""
    path = write(root, name, text)
    return path, tool_use("Write", file_path=str(path), content=text)


def review(hook, monkeypatch, capsys, root: Path, entries: list[dict], **kw):
    """フックを呼び、Claude に返る本文（無ければ None）を返す。

    `subagent=True` なら SubagentStop として呼ぶ。entries はサブエージェントの transcript に
    置き、親の transcript には `parent` を置く。
    """
    transcript = root / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    # 重複抑制の記録をテストごとに分ける。/tmp に残すと他のテストの結果に影響する
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root / "state"))
    # 折り返しが起きない幅で描かせ、1 段落を 1 行で比べる
    monkeypatch.setattr(app_turn, "width", lambda: 1000)
    payload = {
        "transcript_path": str(transcript),
        "cwd": str(root),
        "session_id": kw.get("session", "s"),
        "stop_hook_active": kw.get("active", False),
        "hook_event_name": "Stop",
    }
    if kw.get("subagent"):
        parent = root / "parent.jsonl"
        parent.write_text(
            "\n".join(json.dumps(e) for e in kw.get("parent", [])) + "\n", encoding="utf-8"
        )
        payload |= {
            "hook_event_name": "SubagentStop",
            "transcript_path": str(parent),
            "agent_transcript_path": str(transcript),
        }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    # 片方の見直しだけを確かめるので、もう片方は止める
    monkeypatch.setenv("CLAUDE_SKIP_TEST_REVIEW", "1")
    event = "subagent-stop" if kw.get("subagent") else "stop"
    assert toolkit_hooks.main([event, "--agent", "claude"]) == 0
    out = capsys.readouterr().out
    if not out:
        return None
    body = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    return body if kw.get("raw") else plain(body)


def test_no_edits_says_nothing(hook, tmp_path, monkeypatch, capsys):
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("hello")]) is None


def test_stop_hook_active_says_nothing(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "a.py", "# 足したコメント\nx = 1\n")
    entries = [user_says("go"), edit]
    assert review(hook, monkeypatch, capsys, tmp_path, entries, active=True) is None


def test_bash_written_file_is_not_seen(hook, tmp_path, monkeypatch, capsys):
    """Bash 越しの書き換えは tool_input に現れないので取りこぼす。承知の上での穴。"""
    write(tmp_path, "a.py", "# heredoc で書いたコメント\nx = 1\n")
    entries = [user_says("go"), tool_use("Bash", command="cat > a.py <<'EOF'\n# ...\nEOF")]
    assert review(hook, monkeypatch, capsys, tmp_path, entries) is None


def test_file_edited_before_this_turn_is_not_seen(hook, tmp_path, monkeypatch, capsys):
    """前の turn の編集は対象外。直近のユーザー発言で区切る。"""
    path = write(tmp_path, "a.py", "# 前の turn のコメント\nx = 1\n")
    entries = [
        user_says("first"),
        tool_use("Write", file_path=str(path), content="# 前の turn のコメント\nx = 1\n"),
        user_says("second"),
    ]
    assert review(hook, monkeypatch, capsys, tmp_path, entries) is None


def test_tool_results_and_meta_do_not_end_the_turn(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "a.ts", "// この turn で足した\nconst x = 1;\n")
    entries = [user_says("go"), edit, tool_result(), user_says("差し込み", meta=True)]
    body = review(hook, monkeypatch, capsys, tmp_path, entries)
    assert body is not None
    assert "a.ts:1  この turn で足した" in body


def test_trailing_comment_is_reported(hook, tmp_path, monkeypatch, capsys):
    """コードの後ろに付いたコメントも拾う。行頭が記号の行だけを見ていると取りこぼす。"""
    _, edit = wrote(tmp_path, "a.py", "x = 1  # 1 を入れる\n")
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit])
    assert body is not None
    assert "a.py:1  1 を入れる" in body


def test_markers_inside_python_string_are_not_comments(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "a.py", 'QUERY = """\n# これは文字列の中\nSELECT 1\n"""\n')
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit]) is None


def test_markers_inside_template_literal_are_not_comments(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "a.js", "const s = `\n/* これは文字列の中 */\n`;\n")
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit]) is None


def test_consecutive_line_comments_become_one_row(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "a.py", "# 1 行目\n# 2 行目\n# 3 行目\nx = 1\n")
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit])
    assert body is not None
    assert "コードのコメント（1 件）" in body
    assert "a.py:1  1 行目  3 行" in body


def test_docstring_and_rust_doc_comment_go_to_block_section(hook, tmp_path, monkeypatch, capsys):
    _, py = wrote(tmp_path, "a.py", '"""モジュールの説明"""\nx = 1\n')
    _, rs = wrote(tmp_path, "b.rs", "/// doc 1 行目\n/// doc 2 行目\nfn main() {}\n")
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), py, rs])
    assert body is not None
    assert "ブロックコメントと docstring（2 件）" in body
    assert "a.py:1  モジュールの説明  docstring・1 行" in body
    assert "b.rs:1  doc 1 行目  doc コメント・2 行" in body


def test_multiline_block_is_reported_once(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "a.rs", "fn main() {\n    /* 1 行目\n       2 行目 */\n}\n")
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit])
    assert body is not None
    assert "a.rs:2  1 行目  ブロック・2 行" in body


def test_only_lines_added_by_edit_are_reported(hook, tmp_path, monkeypatch, capsys):
    path = write(tmp_path, "a.sh", "#!/bin/sh\n# 前からあるコメント\n# 足したコメント\necho 1\n")
    entries = [
        user_says("go"),
        tool_use(
            "Edit",
            file_path=str(path),
            old_string="# 前からあるコメント\necho 1\n",
            new_string="# 前からあるコメント\n# 足したコメント\necho 1\n",
        ),
    ]
    body = review(hook, monkeypatch, capsys, tmp_path, entries)
    assert body is not None
    assert "a.sh:3  足したコメント" in body
    assert "前からあるコメント" not in body


def test_multiedit_inputs_are_collected(hook, tmp_path, monkeypatch, capsys):
    path = write(tmp_path, "a.py", "# 1 つ目\nx = 1\n# 2 つ目\ny = 2\n")
    entries = [
        user_says("go"),
        tool_use(
            "MultiEdit",
            file_path=str(path),
            edits=[
                {"old_string": "x = 1\n", "new_string": "# 1 つ目\nx = 1\n"},
                {"old_string": "y = 2\n", "new_string": "# 2 つ目\ny = 2\n"},
            ],
        ),
    ]
    body = review(hook, monkeypatch, capsys, tmp_path, entries)
    assert body is not None
    assert "コードのコメント（2 件）" in body


def test_closing_quotes_alone_do_not_pull_in_old_docstrings(hook, tmp_path, monkeypatch, capsys):
    added = '    """足した。\n\n    2 行目。\n    """\n'
    path = write(tmp_path, "a.py", '"""前からある説明。\n\n2 段落目。\n"""\n\n\ndef f():\n' + added)
    entries = [
        user_says("go"),
        tool_use(
            "Edit", file_path=str(path), old_string="def f():\n", new_string="def f():\n" + added
        ),
    ]
    body = review(hook, monkeypatch, capsys, tmp_path, entries)
    assert body is not None
    assert "足した。" in body
    assert "前からある説明" not in body


def test_moved_line_does_not_fire(hook, tmp_path, monkeypatch, capsys):
    """old にも同じ行があるときは発火しない。再インデントやコードの移動で毎回鳴らないため。"""
    path = write(tmp_path, "a.py", "x = 1\n# 動かすコメント\ny = 2\n")
    entries = [
        user_says("go"),
        tool_use(
            "Edit",
            file_path=str(path),
            old_string="# 動かすコメント\nx = 1\n",
            new_string="x = 1\n# 動かすコメント\n",
        ),
    ]
    assert review(hook, monkeypatch, capsys, tmp_path, entries) is None


def test_pragmas_shebang_pep723_and_separators_are_excluded(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(
        tmp_path,
        "a.py",
        "#!/usr/bin/env -S uv run --script\n"
        "# /// script\n"
        '# dependencies = ["pyyaml"]\n'
        "# ///\n"
        "# SPDX-License-Identifier: MIT\n"
        "# ----------------------------\n"
        "# TODO: あとでやる\n"
        "import os  # noqa: F401\n"
        "x: int = 1  # type: ignore[assignment]\n",
    )
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit]) is None


def test_annotation_only_docblocks_are_excluded(hook, tmp_path, monkeypatch, capsys):
    """PHPUnit の `@test` は消すとテストが走らなくなる。説明の文を持つ docblock は残して見る。"""
    _, edit = wrote(
        tmp_path,
        "ATest.php",
        "<?php\nclass ATest {\n"
        "    /** @test */\n    public function a(): void {}\n"
        "    /**\n     * @dataProvider cases\n     */\n    public function b(): void {}\n"
        "    /**\n     * 境界の値を確かめる。\n     * @test\n     */\n    public function c(): void {}\n"
        "}\n",
    )
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit])
    assert body is not None
    assert "ブロックコメントと docstring（1 件）" in body
    assert "ATest.php:9  境界の値を確かめる。" in body


def test_config_files_go_to_config_section(hook, tmp_path, monkeypatch, capsys):
    _, toml = wrote(tmp_path, "c.toml", "# key を有効にする\nkey = true\n")
    _, docker = wrote(tmp_path, "Dockerfile", "# ベースイメージ\nFROM alpine\n")
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), toml, docker])
    assert body is not None
    assert "設定ファイルのコメント（2 件）" in body
    assert "コードのコメント（" not in body


def test_markdown_and_unknown_suffix_are_ignored(hook, tmp_path, monkeypatch, capsys):
    _, md = wrote(tmp_path, "README.md", "# 見出し\n// コードではない\n")
    _, unknown = wrote(tmp_path, "data.xyz", "# なにか\n")
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), md, unknown]) is None


def test_skipped_directories_are_ignored(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "node_modules/a.js", "// 依存パッケージのコメント\n")
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit]) is None


def test_missing_file_is_skipped(hook, tmp_path, monkeypatch, capsys):
    entries = [
        user_says("go"),
        tool_use("Write", file_path=str(tmp_path / "gone.py"), content="# x\n"),
    ]
    assert review(hook, monkeypatch, capsys, tmp_path, entries) is None


def test_same_comment_is_reported_once_per_session(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "a.py", "# 足したコメント\nx = 1\n")
    entries = [user_says("go"), edit]
    assert review(hook, monkeypatch, capsys, tmp_path, entries, session="same") is not None
    assert review(hook, monkeypatch, capsys, tmp_path, entries, session="same") is None
    assert review(hook, monkeypatch, capsys, tmp_path, entries, session="other") is not None


def test_every_comment_is_listed(hook, tmp_path, monkeypatch, capsys):
    """一覧を途中で切ると、切った分を見ないまま終える。切った分は報告済みにもなって二度と出ない。"""
    # コメントの間にコードの行を挟む。隣り合う行のコメントは 1 つの連なりにまとまる
    source = "".join(f"# {i} 番目\nx{i} = {i}\n" for i in range(12))
    _, edit = wrote(tmp_path, "a.py", source)
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit])
    assert body is not None
    assert "コードのコメント（12 件）" in body
    assert "a.py:23  11 番目" in body
    assert "ほか 4 件" not in body


def test_many_comments_stay_under_the_context_limit(hook, tmp_path, monkeypatch, capsys):
    """10KB を超えると Claude には先頭の 2KB しか渡らない。色を落とし、それでも超えるなら一覧を
    `path:line` だけにして収める。切り捨てはしない。"""
    source = "".join(
        f"# {i} 番目のコメントで、見出しとしてはそこそこ長い文を置く\nx{i} = {i}\n"
        for i in range(300)
    )
    _, edit = wrote(tmp_path, "a.py", source)
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit], raw=True)
    assert body is not None
    assert len(body) <= 10_000
    assert "\x1b[" not in body
    assert all(f"a.py:{2 * i + 1}\n" in body for i in range(300))
    assert "番目のコメント" not in body


def test_reply_asks_for_one_line_per_comment(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "a.py", "# 足したコメント\nx = 1\n")
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit])
    assert body is not None
    assert "`path:line` — 残す／消す／縮める／書き換える：理由" in body
    assert "消したら読む人が何を間違えるか" in body


def test_rewritten_docstring_is_reported_again(hook, tmp_path, monkeypatch, capsys):
    """見出しの行が同じでも、2 段落目を書き換えたら再び上げる。"""
    first = '"""見出し。\n\n1 つ目の段落。\n"""\n'
    path, edit = wrote(tmp_path, "a.py", first)
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit]) is not None

    second = '"""見出し。\n\n書き換えた段落。\n"""\n'
    write(tmp_path, "a.py", second)
    rewrite = tool_use(
        "Edit", file_path=str(path), old_string="1 つ目の段落。", new_string="書き換えた段落。"
    )
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("again"), rewrite])
    assert body is not None
    assert "a.py:1  見出し。  docstring・4 行" in body


def test_shifted_comment_is_not_reported_again(hook, tmp_path, monkeypatch, capsys):
    """上に行を足してコメントがずれただけなら、再び上げない。鍵に行番号を入れていない。"""
    path, edit = wrote(tmp_path, "a.py", "# 残すコメント\nx = 1\n")
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit]) is not None

    write(tmp_path, "a.py", "import os\n# 残すコメント\nx = 1\n")
    # 同じ本文のコメントを書き直した Edit。old に同じ行があると added から落ちるので new だけに置く
    again = tool_use(
        "Edit", file_path=str(path), old_string="x = 1", new_string="# 残すコメント\nx = 1"
    )
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("again"), again]) is None


def test_scratchpad_is_ignored(hook, tmp_path, monkeypatch, capsys):
    # review() が gettempdir を tmp_path/state に差し替えるので、その下が一時ディレクトリになる
    _, edit = wrote(tmp_path, "state/claude-1000/s/scratchpad/a.py", "# 使い捨て\nx = 1\n")
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit]) is None


def test_subagent_reads_its_own_transcript(hook, tmp_path, monkeypatch, capsys):
    """SubagentStop では agent_transcript_path を読む。親の編集はサブエージェントに返さない。"""
    _, parent_edit = wrote(tmp_path, "parent.py", "# 親が足した\nx = 1\n")
    _, child_edit = wrote(tmp_path, "child.py", "# 子が足した\ny = 2\n")
    body = review(
        hook,
        monkeypatch,
        capsys,
        tmp_path,
        [user_says("サブエージェントへの指示"), child_edit],
        subagent=True,
        parent=[user_says("go"), parent_edit],
    )
    assert body is not None
    assert "child.py:1  子が足した" in body
    assert "parent.py" not in body
    # 親に渡るのは最後のメッセージだけなので、元の報告を書き直させる
    assert "最終報告をもう一度そのまま書いてください" in body


def test_main_session_does_not_ask_to_repeat_the_report(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "a.py", "# 足したコメント\nx = 1\n")
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit])
    assert body is not None
    assert "最終報告" not in body


def test_env_var_disables_the_hook(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "a.py", "# 足したコメント\nx = 1\n")
    monkeypatch.setenv("CLAUDE_SKIP_COMMENT_REVIEW", "1")
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit]) is None
