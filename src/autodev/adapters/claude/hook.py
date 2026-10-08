"""ガードのフック（deny-writes.py・park-on-ask.py）の入口。`python -I -m autodev.adapters.claude.hook <名前>`。

標準入力と環境変数を `guard.run_hook` に渡して、返事を書き出す。
読み込めないときは止める（終了コード 2）。フックが黙って落ちると、ガードが消えたまま走る。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

DENY_EXIT = 2


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) != 1:
            raise ValueError(f"ハンドラ名を 1 つ渡す（{args!r}）")
        from . import guard  # noqa: PLC0415

        handler = args[0]
        reply = guard.run_hook(handler, sys.stdin.read(), os.environ)
    except Exception as error:
        sys.stderr.write(f"ガードのフックを読み込めないので、呼び出しを止めました: {error}\n")
        return DENY_EXIT
    if reply.stdout:
        sys.stdout.write(reply.stdout + "\n")
    if reply.stderr:
        sys.stderr.write(reply.stderr + "\n")
    return reply.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
