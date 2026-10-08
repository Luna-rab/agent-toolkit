"""`agent-toolkit` コマンドの入口。サブコマンドは `COMMANDS` に足す。"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from toolkit import doctor, hooks, install

#: サブコマンド名 → (説明, 引数を足す関数, 実行する関数)
COMMANDS: dict[str, tuple[str, Callable[[argparse.ArgumentParser], None], Callable]] = {
    "doctor": ("依存の CLI と認証を確かめる", doctor.add_arguments, doctor.run),
    "hook": ("エージェントのフックから呼ばれる", hooks.add_arguments, hooks.run),
    "install": ("config/ の中身をホームへ配る", install.add_arguments, install.run),
    "list": ("部品・プリセット・依存を一覧する", doctor.list_arguments, doctor.run_list),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-toolkit")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    for name, (help_text, add_arguments, run) in COMMANDS.items():
        sub = commands.add_parser(name, help=help_text, description=help_text)
        add_arguments(sub)
        sub.set_defaults(run=run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.run(args)
