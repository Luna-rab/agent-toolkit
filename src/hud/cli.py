"""`agent-hud` の入口。サブコマンドは `statusline`（Claude Code の statusline）と `watch`（autodev のランを見る画面）。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-hud", description="エージェントの状態表示")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("statusline", help="標準入力の JSON を読み、statusline を標準出力に描く")
    watch = commands.add_parser("watch", help="autodev のランを見る画面を開く")
    watch.add_argument("name", nargs="?", help="開くラン名。省けばラン一覧から始める")
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if args.command == "statusline":
        from .app import statusline  # noqa: PLC0415

        return statusline.main()
    from .app import watch as watch_app  # noqa: PLC0415

    return watch_app.main(args.name)
