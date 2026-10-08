"""ガードの規則（domain/guard.py）。I/O 無しで、Guard と宛先・操作だけで確かめる。"""

from __future__ import annotations

import pytest

from autodev.domain.guard import (
    AskVerdict,
    RefusalReason,
    WriteTarget,
    WriteZone,
)
from autodev.domain.value_objects.guard import Guard
from autodev.domain.value_objects.write_scope import WriteScope

SCOPES = list(WriteScope)


def tree(path: str, *, is_test: bool = False, listed: bool = False) -> WriteTarget:
    return WriteTarget(WriteZone.TREE, path, is_test=is_test, listed=listed)


@pytest.mark.parametrize(
    ("scope", "source", "test"),
    [
        (WriteScope.NONE, False, False),
        (WriteScope.NON_TESTS, True, False),
        (WriteScope.TESTS_AND_STUBS, True, True),
        (WriteScope.TESTS_ONLY, False, True),
        (WriteScope.LISTED, False, False),
    ],
)
def test_worktreeの中は書いてよい範囲で決める(scope: WriteScope, source: bool, test: bool):
    guard = Guard(scope)
    assert (guard.judge_write(tree("src/a.py")) is None) is source
    assert (guard.judge_write(tree("tests/test_a.py", is_test=True)) is None) is test


def test_渡されたファイルだけを書ける():
    guard = Guard(WriteScope.LISTED)
    assert guard.judge_write(tree("src/a.py", listed=True)) is None
    refusal = guard.judge_write(tree("src/b.py"))
    assert refusal is not None
    assert refusal.reason is RefusalReason.NOT_LISTED


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize(
    "zone",
    [
        WriteZone.RUN_DIR,
        WriteZone.HOME,
        WriteZone.TARGET_REPO,
        WriteZone.TEMP,
        WriteZone.ELSEWHERE,
    ],
)
def test_worktreeの外は範囲にかかわらず止めない(scope: WriteScope, zone: WriteZone):
    assert Guard(scope).judge_write(WriteTarget(zone, "/x")) is None


@pytest.mark.parametrize("scope", SCOPES)
def test_場所の決まらないものは範囲にかかわらず止める(scope: WriteScope):
    refusal = Guard(scope).judge_write(WriteTarget(WriteZone.UNKNOWN, "$D/a"))
    assert refusal is not None
    assert refusal.reason is RefusalReason.UNKNOWN_PLACE
    assert refusal.zone is WriteZone.UNKNOWN


def test_askは聞けるステージだけが回答を待てる():
    asking = Guard(WriteScope.NONE, can_ask=True)
    assert asking.judge_ask(answered=False) is AskVerdict.DEFER
    assert asking.judge_ask(answered=True) is AskVerdict.PASS
    assert Guard(WriteScope.NONE).judge_ask(answered=True) is AskVerdict.REFUSE
