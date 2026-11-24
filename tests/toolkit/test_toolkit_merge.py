"""`toolkit.merge` を関数で直接確かめる。以前の agent-toolkit が入れたフックだけが消え、ユーザーのものは残る。"""

from __future__ import annotations

import copy

import pytest

from toolkit.merge import deep_merge, drop_managed_hooks, is_managed_hook_group


def group(command: str, **extra) -> dict:
    return {**extra, "hooks": [{"type": "command", "command": command}]}


USER_STOP = group("my-stop.sh")

OLD_COMMANDS = [
    "agent-toolkit hook stop --agent claude",
    "agent-toolkit hook subagent-stop --agent claude",
    '[ -z "$AUTODEV_GUARD" ] || autodev hook deny-writes || exit 2',
    '[ -z "$AUTODEV_GUARD" ] || autodev hook park-on-ask || exit 2',
    "/home/u/.claude/hooks/check-foo.sh",
    "/x/.claude/hooks/review-new-comments.py",
    "python3 /x/dotfiles/dist/dot-claude/hooks/a.py",
    "agent-hud statusline",
]


@pytest.mark.parametrize("command", OLD_COMMANDS)
def test_old_hook_groups_are_managed(command: str) -> None:
    assert is_managed_hook_group(group(command))


def test_is_managed_hook_group_true_when_any_command_matches() -> None:
    mixed = {"hooks": [{"command": "echo"}, {"command": "agent-toolkit hook stop --agent codex"}]}
    assert is_managed_hook_group(mixed) is True


def test_user_hooks_are_not_managed() -> None:
    assert is_managed_hook_group({"hooks": [{"command": "echo"}]}) is False
    assert is_managed_hook_group("text") is False
    assert not is_managed_hook_group(group('[ -z "$MY_FLAG" ] || my-hook || exit 2'))
    assert not is_managed_hook_group(group("autodev hook deny-writes"))


def test_drop_keeps_user_groups_and_removes_emptied_events() -> None:
    settings = {
        "model": "x",
        "hooks": {
            "Stop": [USER_STOP, group("agent-toolkit hook stop --agent claude")],
            "SubagentStop": [group("agent-toolkit hook subagent-stop --agent claude")],
            "Notification": [],
        },
    }
    assert drop_managed_hooks(settings) == {
        "model": "x",
        "hooks": {"Stop": [USER_STOP], "Notification": []},
    }


def test_drop_removes_hooks_key_when_nothing_is_left() -> None:
    settings = {"hooks": {"Stop": [group("agent-toolkit hook stop --agent codex")]}}
    assert drop_managed_hooks(settings) == {}


def test_drop_leaves_settings_without_our_hooks_as_is() -> None:
    settings = {"hooks": {"Stop": [USER_STOP], "Odd": "text"}, "env": {"X": "1"}}
    assert drop_managed_hooks(settings) is settings
    assert drop_managed_hooks({"env": {}}) == {"env": {}}


def test_drop_does_not_mutate_input() -> None:
    settings = {"hooks": {"Stop": [USER_STOP, group("agent-toolkit hook stop --agent claude")]}}
    before = copy.deepcopy(settings)
    drop_managed_hooks(settings)
    assert settings == before


def test_deep_merge_recurses_and_replaces_non_dicts() -> None:
    merged = deep_merge({"a": {"x": 1, "l": [1]}, "b": 1}, {"a": {"y": 2, "l": [2]}, "b": {"c": 3}})
    assert merged == {"a": {"x": 1, "y": 2, "l": [2]}, "b": {"c": 3}}
