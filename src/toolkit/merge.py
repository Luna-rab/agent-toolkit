"""設定 JSON のマージ。フックの配列だけ、toolkit が管理するグループとユーザーのグループを分けて合わせる。"""

from __future__ import annotations

_MANAGED_PREFIXES = ("agent-toolkit hook", "agent-hud", '[ -z "$AUTODEV_GUARD" ] || autodev hook ')
_MANAGED_PARTS = ("/.claude/hooks/check-", "/.claude/hooks/review-new-", "/dist/dot-claude/")


def deep_merge(base: dict, over: dict) -> dict:
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _is_managed_command(command: object) -> bool:
    if not isinstance(command, str):
        return False
    return command.startswith(_MANAGED_PREFIXES) or any(part in command for part in _MANAGED_PARTS)


def is_managed_hook_group(group: object) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(hook, dict) and _is_managed_command(hook.get("command")) for hook in hooks
    )


def merge_hook_groups(current: list, ours: list) -> list:
    return [group for group in current if not is_managed_hook_group(group)] + list(ours)


def merge_settings(base: dict, over: dict) -> dict:
    merged = dict(base)
    for key, value in over.items():
        current = merged.get(key)
        if key == "hooks" and isinstance(value, dict) and isinstance(current, dict):
            events = dict(current)
            for event, groups in value.items():
                if isinstance(groups, list) and isinstance(events.get(event), list):
                    events[event] = merge_hook_groups(events[event], groups)
                elif isinstance(groups, dict) and isinstance(events.get(event), dict):
                    events[event] = deep_merge(events[event], groups)
                else:
                    events[event] = groups
            merged[key] = events
        elif isinstance(value, dict) and isinstance(current, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged
