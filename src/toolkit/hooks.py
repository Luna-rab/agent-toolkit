"""`agent-toolkit hook <event> --agent <agent>` の本体。

**どんな入力・例外でも終了コード 0 で終わる**（エージェントの停止を妨げない）。例外は標準エラーに 1 行。
"""

from __future__ import annotations

import argparse
import sys

from turnreview.app import turn
from turnreview.ports import hookio

AGENTS = ("claude", "codex")
#: イベント名と、stdin に `hook_event_name` が無いときに補う値
EVENTS = {"stop": "Stop", "subagent-stop": "SubagentStop"}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("event", choices=sorted(EVENTS), help="フックのイベント")
    parser.add_argument("--agent", required=True, choices=AGENTS, help="呼び出したエージェント")


def stop(event: str, agent: str) -> None:
    payload = hookio.read_payload()
    if payload is None:
        return
    payload.setdefault("hook_event_name", EVENTS[event])
    turn.run(agent, payload, hookio.write)


def run(args: argparse.Namespace) -> int:
    try:
        stop(args.event, args.agent)
    except Exception as exc:
        print(f"agent-toolkit hook {args.event}: skipped ({exc!r})", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-toolkit hook")
    add_arguments(parser)
    return run(parser.parse_args(argv))
