"""`toolkit.merge` を関数で直接確かめる。フックのグループは toolkit 管理下だけが入れ替わり、ユーザーのものは残る。"""

from __future__ import annotations

import copy

import pytest

from toolkit.merge import (
    deep_merge,
    is_managed_hook_group,
    merge_hook_groups,
    merge_settings,
)


def group(command: str, **extra) -> dict:
    return {**extra, "hooks": [{"type": "command", "command": command}]}


STOP_OURS = group("agent-toolkit hook stop --agent claude")
USER_STOP = group("my-stop.sh")


def test_user_group_stays_and_ours_is_appended() -> None:
    merged = merge_settings({"hooks": {"Stop": [USER_STOP]}}, {"hooks": {"Stop": [STOP_OURS]}})
    assert merged["hooks"]["Stop"] == [USER_STOP, STOP_OURS]


def test_merging_twice_gives_the_same_result() -> None:
    once = merge_settings({"hooks": {"Stop": [USER_STOP]}}, {"hooks": {"Stop": [STOP_OURS]}})
    twice = merge_settings(once, {"hooks": {"Stop": [STOP_OURS]}})
    assert twice == once
    commands = [g["hooks"][0]["command"] for g in twice["hooks"]["Stop"]]
    assert commands.count("agent-toolkit hook stop --agent claude") == 1


def test_existing_managed_group_is_replaced_not_duplicated() -> None:
    old = group("agent-toolkit hook stop --agent claude", matcher="old")
    merged = merge_settings({"hooks": {"Stop": [old]}}, {"hooks": {"Stop": [STOP_OURS]}})
    assert merged["hooks"]["Stop"] == [STOP_OURS]


@pytest.mark.parametrize(
    "command",
    [
        "/home/u/.claude/hooks/check-foo.sh",
        "/x/.claude/hooks/review-new-comments.py",
        "python3 /x/dotfiles/dist/dot-claude/hooks/a.py",
        "agent-hud statusline",
    ],
)
def test_old_layout_and_toolkit_groups_are_dropped_and_user_group_stays(command: str) -> None:
    echo = group("echo hi")
    merged = merge_hook_groups([group(command), echo], [STOP_OURS])
    assert merged == [echo, STOP_OURS]


def test_is_managed_hook_group_true_when_any_command_matches() -> None:
    mixed = {"hooks": [{"command": "echo"}, {"command": "agent-toolkit hook stop --agent codex"}]}
    assert is_managed_hook_group(mixed) is True


def test_is_managed_hook_group_false_for_user_group_and_non_dict() -> None:
    assert is_managed_hook_group({"hooks": [{"command": "echo"}]}) is False
    assert is_managed_hook_group("text") is False


def test_non_dict_group_is_kept_as_user_group() -> None:
    merged = merge_hook_groups(["text", group("agent-hud statusline")], [STOP_OURS])
    assert merged == ["text", STOP_OURS]


def test_events_missing_from_over_are_left_alone() -> None:
    pre = [group("echo pre")]
    merged = merge_settings(
        {"hooks": {"PreToolUse": pre, "Stop": [USER_STOP]}},
        {"hooks": {"Stop": [STOP_OURS]}},
    )
    assert merged["hooks"]["PreToolUse"] == pre
    assert merged["hooks"]["Stop"] == [USER_STOP, STOP_OURS]


def test_event_only_in_over_is_added() -> None:
    merged = merge_settings({"hooks": {}}, {"hooks": {"Stop": [STOP_OURS]}})
    assert merged["hooks"]["Stop"] == [STOP_OURS]


def test_other_arrays_are_replaced() -> None:
    assert merge_settings({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}


def test_dicts_merge_recursively() -> None:
    merged = merge_settings({"env": {"X": "1"}}, {"env": {"Y": "2"}})
    assert merged == {"env": {"X": "1", "Y": "2"}}


def test_deep_merge_recurses_and_replaces_non_dicts() -> None:
    merged = deep_merge({"a": {"x": 1, "l": [1]}, "b": 1}, {"a": {"y": 2, "l": [2]}, "b": {"c": 3}})
    assert merged == {"a": {"x": 1, "y": 2, "l": [2]}, "b": {"c": 3}}


def test_inputs_are_not_mutated() -> None:
    base = {"hooks": {"Stop": [USER_STOP, group("agent-hud statusline")]}, "env": {"X": "1"}}
    over = {"hooks": {"Stop": [STOP_OURS]}, "env": {"Y": "2"}}
    base_before, over_before = copy.deepcopy(base), copy.deepcopy(over)
    merge_settings(base, over)
    assert base == base_before
    assert over == over_before

    current = [USER_STOP, group("agent-hud statusline")]
    ours = [STOP_OURS]
    current_before, ours_before = copy.deepcopy(current), copy.deepcopy(ours)
    merge_hook_groups(current, ours)
    assert current == current_before
    assert ours == ours_before


CODEX_GUARD_COMMANDS = [
    '[ -z "$AUTODEV_GUARD" ] || autodev hook deny-writes || exit 2',
    '[ -z "$AUTODEV_GUARD" ] || autodev hook park-on-ask || exit 2',
]


@pytest.mark.parametrize("command", CODEX_GUARD_COMMANDS)
def test_codex_guard_hook_command_is_managed(command: str) -> None:
    assert is_managed_hook_group(group(command))


def test_user_hook_with_a_similar_guard_prefix_is_not_managed() -> None:
    assert not is_managed_hook_group(group('[ -z "$MY_FLAG" ] || my-hook || exit 2'))
    assert not is_managed_hook_group(group("autodev hook deny-writes"))


def test_codex_guard_hooks_are_replaced_not_duplicated() -> None:
    ours = [group(command) for command in CODEX_GUARD_COMMANDS]
    mine = group("my-pre-tool.sh")
    current = {"hooks": {"PreToolUse": [mine, *ours]}}
    merged = merge_settings(current, {"hooks": {"PreToolUse": ours}})
    assert merged["hooks"]["PreToolUse"] == [mine, *ours]
    again = merge_settings(merged, {"hooks": {"PreToolUse": ours}})
    assert again == merged
