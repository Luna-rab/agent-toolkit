"""テストの hook（`turnreview.app.testcases`）を payload から本文まで通しで確かめる。

transcript の集め方と報告済みの記録はコメントの hook と共有しているので
（`turnreview.core.turn`・`turnreview.core.report`）、ここではテストの見分け方と本文を見る。
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
from turnreview.app import testcases
from turnreview.app import turn as app_turn


@pytest.fixture
def hook():
    return testcases


def user_says(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


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
    path = write(root, name, text)
    return path, tool_use("Write", file_path=str(path), content=text)


def review(hook, monkeypatch, capsys, root: Path, entries: list[dict], **kw):
    transcript = root / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root / "state"))
    monkeypatch.setattr(app_turn, "width", lambda: 1000)
    payload = {
        "transcript_path": str(transcript),
        "cwd": str(root),
        "session_id": kw.get("session", "s"),
        "stop_hook_active": kw.get("active", False),
        "hook_event_name": "Stop",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    # 片方の見直しだけを確かめるので、もう片方は止める
    monkeypatch.setenv("CLAUDE_SKIP_COMMENT_REVIEW", "1")
    event = "subagent-stop" if kw.get("subagent") else "stop"
    assert toolkit_hooks.main([event, "--agent", "claude"]) == 0
    out = capsys.readouterr().out
    return plain(json.loads(out)["hookSpecificOutput"]["additionalContext"]) if out else None


PHPUNIT = """<?php
class GuideTest extends TestCase
{
    /** @test */
    public function 開発ガイドにローカルログイン口が書かれている(): void
    {
        $this->assertStringContainsString('404', file_get_contents(base_path('docs/guide.md')));
    }

    #[Test]
    public function 属性で印を付けたテスト(): void {}

    public function test_prefix(): void {}

    public function helper(): void {}

    private function test_private(): void {}
}
"""


def test_phpunit_tests_are_listed(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "tests/Feature/GuideTest.php", PHPUNIT)
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit])
    assert body is not None
    assert "足したテスト（3 本）" in body
    assert "tests/Feature/GuideTest.php:5  開発ガイドにローカルログイン口が書かれている" in body
    assert "GuideTest.php:11  属性で印を付けたテスト" in body
    assert "GuideTest.php:13  test_prefix" in body
    assert "helper" not in body
    assert "test_private" not in body


def test_pest_calls_are_listed(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "tests/Unit/login.php", "<?php\nit('returns 404', function () {});\n")
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit])
    assert body is not None
    assert "tests/Unit/login.php:2  returns 404" in body


def test_js_and_ts_calls_are_listed(hook, tmp_path, monkeypatch, capsys):
    source = (
        "describe('login', () => {\n"
        "  it('redirects', async () => {});\n"
        "  test.only(`only this`, () => {});\n"
        "  it.each([1, 2])('each %s', () => {});\n"
        "});\n"
    )
    _, ts = wrote(tmp_path, "src/login.spec.ts", source)
    _, js = wrote(tmp_path, "__tests__/a.js", "test('plain js', () => {});\n")
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), ts, js])
    assert body is not None
    assert "足したテスト（4 本）" in body
    assert "src/login.spec.ts:2  redirects" in body
    assert "src/login.spec.ts:3  only this" in body
    assert "src/login.spec.ts:4  each %s" in body
    assert "__tests__/a.js:1  plain js" in body
    # describe はテストではなく、まとめる枠
    assert "spec.ts:1  login" not in body


def test_python_tests_are_listed(hook, tmp_path, monkeypatch, capsys):
    source = (
        "def helper():\n    pass\n\n"
        "def test_top():\n    assert helper() is None\n\n"
        "class TestGroup:\n    async def test_method(self):\n        pass\n"
    )
    _, edit = wrote(tmp_path, "test_a.py", source)
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit])
    assert body is not None
    assert "test_a.py:4  test_top" in body
    assert "test_a.py:8  test_method" in body
    assert "helper" not in body


def test_non_test_files_are_ignored(hook, tmp_path, monkeypatch, capsys):
    """テストの形をした関数でも、テストファイルの外なら並べない。"""
    _, py = wrote(tmp_path, "app/checks.py", "def test_connection():\n    pass\n")
    _, ts = wrote(tmp_path, "src/router.ts", "test('not a test file', () => {});\n")
    _, vendor = wrote(tmp_path, "vendor/x/tests/ATest.php", PHPUNIT)
    entries = [user_says("go"), py, ts, vendor]
    assert review(hook, monkeypatch, capsys, tmp_path, entries) is None


def test_body_only_edit_is_not_listed(hook, tmp_path, monkeypatch, capsys):
    """宣言の行を足していないテストは並べない。既存のテストの手直しで毎回上がらないため。"""
    path = write(tmp_path, "test_a.py", "def test_x():\n    assert f() == 2\n")
    entries = [
        user_says("go"),
        tool_use(
            "Edit", file_path=str(path), old_string="assert f() == 1", new_string="assert f() == 2"
        ),
    ]
    assert review(hook, monkeypatch, capsys, tmp_path, entries) is None


def test_renamed_test_is_listed(hook, tmp_path, monkeypatch, capsys):
    path = write(tmp_path, "test_a.py", "def test_new_name():\n    assert True\n")
    entries = [
        user_says("go"),
        tool_use(
            "Edit",
            file_path=str(path),
            old_string="def test_old_name():",
            new_string="def test_new_name():",
        ),
    ]
    body = review(hook, monkeypatch, capsys, tmp_path, entries)
    assert body is not None
    assert "test_a.py:1  test_new_name" in body


def test_reply_asks_both_questions_per_test(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "test_a.py", "def test_x():\n    pass\n")
    body = review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit])
    assert body is not None
    assert "本番コードをどう変えたら、このテストは落ちるか" in body
    assert "その変更で落ちるテストが、ほかにないか" in body
    # 書いてある値を実装と突き合わせる形でも、ドキュメントを読むテストは消させる
    assert "ファイルとして読むテストは、答えにかかわらず消してください" in body
    assert "`path:line` — 残す／消す／書き直す：落ちるのは〜したとき。" in body


def test_same_test_is_reported_once_until_it_changes(hook, tmp_path, monkeypatch, capsys):
    path, edit = wrote(tmp_path, "test_a.py", "def test_x():\n    assert f() == 1\n")
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit]) is not None
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit]) is None

    rewritten = "def test_x():\n    assert f() == 2\n"
    write(tmp_path, "test_a.py", rewritten)
    again = tool_use("Write", file_path=str(path), content=rewritten)
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("again"), again]) is not None


def test_stop_hook_active_says_nothing(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "test_a.py", "def test_x():\n    pass\n")
    entries = [user_says("go"), edit]
    assert review(hook, monkeypatch, capsys, tmp_path, entries, active=True) is None


def test_env_var_disables_the_hook(hook, tmp_path, monkeypatch, capsys):
    _, edit = wrote(tmp_path, "test_a.py", "def test_x():\n    pass\n")
    monkeypatch.setenv("CLAUDE_SKIP_TEST_REVIEW", "1")
    assert review(hook, monkeypatch, capsys, tmp_path, [user_says("go"), edit]) is None
