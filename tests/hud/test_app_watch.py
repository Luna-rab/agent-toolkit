"""autodev-watch の画面を、Textual の `run_test()` で端末なしに動かす。autodev の入口は偽物に替える。"""

from __future__ import annotations

import asyncio
import time

from hud_samples import calls, finding, status, write_fake_entry
from textual.pilot import Pilot

from hud.app.watch import Level, Watch

#: `--name` を付けたときだけ終了コード `code` で落ち、一覧は `FAKE_STATUS` を返す入口。
#: 呼ばれた引数を `FAKE_CALLS` に足す
NAME_FAILS = """\
import os, sys
with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
if "--name" in sys.argv:
    sys.stderr.write("{message}\\n")
    sys.exit({code})
with open(os.environ["FAKE_STATUS"], encoding="utf-8") as fh:
    print(fh.read())
"""


def name_fails(tmp_path, monkeypatch, code: int, message: str) -> None:
    """入口を、`--name` だけが終了コード `code` で落ちるものに替える。"""
    entry = tmp_path / f"name-fails-{code}.py"
    entry.write_text(NAME_FAILS.format(code=code, message=message), encoding="utf-8")
    monkeypatch.setenv("AUTODEV_ENTRY", str(entry))


def screen_text(app: Watch) -> str:
    return app.export_screenshot().replace("&#160;", " ")


def fake(tmp_path, monkeypatch, statuses: list[dict]) -> None:
    for key, value in write_fake_entry(tmp_path, statuses).items():
        monkeypatch.setenv(key, value)


async def settle(pilot: Pilot) -> None:
    """status を呼ぶスレッドが終わり、結果で描き直すまで待つ。"""
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def test_ランから段までEnterで入りEscで戻る(tmp_path, monkeypatch):
    fake(tmp_path, monkeypatch, [status()])

    async def drive() -> None:
        app = Watch()
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            assert (app.level, app.cursor[Level.RUNS]) == (Level.RUNS, "add-cache")
            # ランの詳細: 進み具合と回答を待っている質問
            text = screen_text(app)
            assert "1/4 スタック済み" in text
            assert "空の入力は弾くか" in text
            assert "深く" in text

            await pilot.press("enter")
            await settle(pilot)
            # 実行中のタスクを選んで入る。詳細は段の並びと走っている実行
            assert (app.level, app.cursor[Level.TASKS]) == (Level.TASKS, "task1")
            text = screen_text(app)
            # スクリーンショットは色の変わり目で文字列が切れるので、色の揃った部分だけを探す
            assert "ジャッジ r2 ◼" in text and "完了チェック" in text
            assert "ジャッジ r2 a1" in text and "7ターン Read" in text

            await pilot.press("enter")
            await settle(pilot)
            # 今の段を選ぶ。その段の実行を出す
            assert (app.level, app.cursor[Level.STAGES]) == (Level.STAGES, "step:1")
            assert "実行中" in screen_text(app)

            await pilot.press("escape")
            await settle(pilot)
            assert (app.level, app.cursor[Level.TASKS]) == (Level.TASKS, "task1")
            await pilot.press("escape")
            await settle(pilot)
            assert app.level is Level.RUNS

    asyncio.run(drive())


def test_指摘や判断の履歴があっても層はランとタスクと段のまま(tmp_path, monkeypatch):
    st = status()
    st["tasks"][1]["findings"] = [finding(), finding(id="R2", status="closed")]
    st["tasks"][1]["notes"] = [{"text": "空の入力は弾く", "origin": "user", "question": "q1"}]
    st["plan"]["findings"] = [finding(id="D1", design=1)]
    fake(tmp_path, monkeypatch, [st])

    async def drive() -> None:
        app = Watch()
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            for level in (Level.TASKS, Level.STAGES, Level.STAGES):
                await pilot.press("enter")
                await settle(pilot)
                assert app.level is level
            # 指摘は段の下の層にならず、段の項目のまま
            assert app.cursor[Level.STAGES] == "step:1"

    asyncio.run(drive())


def test_深い層では選んだランのstatusだけを呼ぶ(tmp_path, monkeypatch):
    fake(tmp_path, monkeypatch, [status()])

    async def drive() -> None:
        app = Watch("add-cache")
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            app.reload()
            await settle(pilot)
            assert calls(tmp_path) == ["status --json --name add-cache"] * 2

    asyncio.run(drive())


def test_statusを待つ間も画面を止めない(tmp_path, monkeypatch):
    fake(tmp_path, monkeypatch, [status()])

    async def drive() -> None:
        app = Watch()
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            slow = tmp_path / "slow-autodev.py"
            slow.write_text("import time; time.sleep(1)", encoding="utf-8")
            monkeypatch.setenv("AUTODEV_ENTRY", str(slow))
            started = time.monotonic()
            app.reload()
            await pilot.press("down")
            assert time.monotonic() - started < 0.5
            await settle(pilot)

    asyncio.run(drive())


def test_読み直してもカーソルを保つ(tmp_path, monkeypatch):
    fake(tmp_path, monkeypatch, [status()])

    async def drive() -> None:
        app = Watch("add-cache")
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            assert (app.level, app.cursor[Level.TASKS]) == (Level.TASKS, "task1")
            await pilot.press("down")
            await pilot.pause()
            assert app.cursor[Level.TASKS] == "task2"
            app.reload()
            await settle(pilot)
            assert app.cursor[Level.TASKS] == "task2"

    asyncio.run(drive())


def test_読み損じたら前の表示とカーソルを残して理由を出す(tmp_path, monkeypatch):
    fake(tmp_path, monkeypatch, [status()])

    async def drive() -> None:
        app = Watch("add-cache")
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            await pilot.press("down")
            await pilot.pause()
            broken = tmp_path / "broken-autodev.py"
            broken.write_text("import sys; sys.stderr.write('autodev: 落ちた\\n'); sys.exit(1)")
            monkeypatch.setenv("AUTODEV_ENTRY", str(broken))
            app.reload()
            await settle(pilot)
            assert (app.level, app.cursor[Level.TASKS]) == (Level.TASKS, "task2")
            text = screen_text(app)
            assert "autodev: 落ちた" in text and "キャッシュの土台" in text

    asyncio.run(drive())


def test_nameが終了コード1なら一覧を呼ばずに前の表示を残して理由を出す(tmp_path, monkeypatch):
    """一覧にそのランが無くても、1 は「ランが無い」ではないので、ランのリストに戻らない。"""
    fake(tmp_path, monkeypatch, [status()])

    async def drive() -> None:
        app = Watch("add-cache")
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            await pilot.press("down")
            await pilot.pause()
            write_fake_entry(tmp_path, [])
            before = len(calls(tmp_path))
            name_fails(tmp_path, monkeypatch, 1, "Traceback: boom")
            app.reload()
            await settle(pilot)
            assert (app.level, app.run_name) == (Level.TASKS, "add-cache")
            assert app.cursor[Level.TASKS] == "task2"
            text = screen_text(app)
            assert "Traceback: boom" in text and "キャッシュの土台" in text
            assert calls(tmp_path)[before:] == ["status --json --name add-cache"]

    asyncio.run(drive())


def test_nameが終了コード5ならランのリストに戻る(tmp_path, monkeypatch):
    """一覧にまだそのラン名があっても、5 はランが無いという答えなので戻る。"""
    fake(tmp_path, monkeypatch, [status()])

    async def drive() -> None:
        app = Watch("add-cache")
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            assert app.level is Level.STAGES
            name_fails(tmp_path, monkeypatch, 5, "autodev: そのランが無い")
            app.reload()
            await settle(pilot)
            assert (app.level, app.run_name) == (Level.RUNS, None)
            assert app.cursor[Level.RUNS] == "add-cache"

    asyncio.run(drive())


def test_形の版が2でないランの一覧なら右ペインに形の版が違うと出す(tmp_path, monkeypatch):
    fake(tmp_path, monkeypatch, [status(format=1)])

    async def drive() -> None:
        app = Watch()
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            assert "形の版が違う" in screen_text(app)

    asyncio.run(drive())


def test_形の版が2でないランの中を見ると右ペインに形の版が違うと出す(tmp_path, monkeypatch):
    fake(tmp_path, monkeypatch, [status(format=1)])

    async def drive() -> None:
        app = Watch("add-cache")
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            assert "形の版が違う" in screen_text(app)

    asyncio.run(drive())


def test_これからの段はまだ走っていないと出す(tmp_path, monkeypatch):
    fake(tmp_path, monkeypatch, [status()])

    async def drive() -> None:
        app = Watch("add-cache")
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            await pilot.press("down")
            await pilot.pause()
            assert app.cursor[Level.STAGES] == "step:2"
            assert "まだ走っていない" in screen_text(app)

    asyncio.run(drive())


def test_読めないランは理由を出し中には入らない(tmp_path, monkeypatch):
    fake(tmp_path, monkeypatch, [{"name": "old", "error": "ValueError: 版が違う"}, status()])

    async def drive() -> None:
        app = Watch()
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            await pilot.press("down")
            await pilot.pause()
            assert app.cursor[Level.RUNS] == "old"
            assert "ValueError: 版が違う" in screen_text(app)
            await pilot.press("enter")
            await settle(pilot)
            assert app.level is Level.RUNS

    asyncio.run(drive())


def test_一覧にも無いランならランのリストに戻る(tmp_path, monkeypatch):
    fake(tmp_path, monkeypatch, [status()])

    async def drive() -> None:
        app = Watch("nope")
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            assert (app.level, app.cursor[Level.RUNS]) == (Level.RUNS, "add-cache")

    asyncio.run(drive())


def test_statusが返らなければそう出す(tmp_path, monkeypatch):
    entry = tmp_path / "broken-autodev.py"
    entry.write_text("import sys; sys.stderr.write('autodev: まだ動かない\\n'); sys.exit(1)")
    monkeypatch.setenv("AUTODEV_ENTRY", str(entry))

    async def drive() -> None:
        app = Watch()
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            text = screen_text(app)
            assert "autodev status を読めない" in text and "まだ動かない" in text

    asyncio.run(drive())
