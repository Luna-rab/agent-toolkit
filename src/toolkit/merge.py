"""設定 JSON のマージと、以前の agent-toolkit が入れたフックの片付け。"""

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


def drop_managed_hooks(settings: dict) -> dict:
    """agent-toolkit が以前入れたフックのグループを除く。

    ユーザーのグループは残す。除いて空になったイベントは消し、`hooks` が空になれば `hooks` も消す。
    """
    events = settings.get("hooks")
    if not isinstance(events, dict):
        return settings
    kept: dict = {}
    for event, groups in events.items():
        if not isinstance(groups, list):
            kept[event] = groups
            continue
        rest = [group for group in groups if not is_managed_hook_group(group)]
        if rest or not groups:
            kept[event] = rest
    if kept == events:
        return settings
    cleaned = dict(settings)
    if kept:
        cleaned["hooks"] = kept
    else:
        del cleaned["hooks"]
    return cleaned
