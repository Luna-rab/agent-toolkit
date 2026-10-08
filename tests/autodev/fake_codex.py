"""検査で `codex exec` の代わりに起動する偽物。本物は叩かない。

`FAKE_CODEX_SCRIPT` に台本の JSON ファイルを渡す。台本は次の形:

- `steps`: 上から順にする。`{"emit": <JSONL の 1 行>}`・`{"sleep": 秒}`・`{"hook": <フックの記録の 1 行>}`
  （`AUTODEV_HOOK_RECORD` のファイルへ書き足す）・`{"hang": true}`（SIGINT が来るまで待つ）
- `exit`: 台本を終えたときの終了コード（既定 0）
- `sigint`: `"exit"` なら SIGINT で終了コード 1 で終わる（実測の形）。`"ignore"` なら無視する
- `stderr`: 標準エラーに書く文
- `login`: `codex login status` の終了コード（既定 0）

`FAKE_CODEX_RECORD` に、起動のたびに受けた引数・環境変数・標準入力・`--output-schema` のファイルの
中身を JSON の 1 行で書き足す。SIGINT を受けたら `FAKE_CODEX_SIGNALS` に 1 行書き足す。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

KEPT_ENV = (
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "AUTODEV_HOOK_RECORD",
    "AUTODEV_GUARD",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_CONFIG_DIR",
    "GIT_TERMINAL_PROMPT",
)


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def record(argv: list[str], stdin: str) -> None:
    path = os.environ.get("FAKE_CODEX_RECORD")
    if not path:
        return
    schema_file = option(argv, "--output-schema")
    schema = None
    if schema_file and os.path.isfile(schema_file):
        with open(schema_file, encoding="utf-8") as fh:
            schema = json.load(fh)
    body = {
        "argv": argv,
        "cwd": os.getcwd(),
        "env": {k: os.environ[k] for k in KEPT_ENV if k in os.environ},
        "stdin": stdin,
        "schema": schema,
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(body, ensure_ascii=False) + "\n")


def on_sigint(script: dict):
    def handler(signum, frame):
        path = os.environ.get("FAKE_CODEX_SIGNALS")
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("SIGINT\n")
        if script.get("sigint", "exit") == "exit":
            sys.stdout.flush()
            os._exit(1)

    return handler


def main() -> int:
    argv = sys.argv[1:]
    script_path = os.environ.get("FAKE_CODEX_SCRIPT")
    script = {}
    if script_path:
        with open(script_path, encoding="utf-8") as fh:
            script = json.load(fh)
    if argv == ["--version"]:
        sys.stdout.write("codex-cli 0.0.0 (fake)\n")
        return 0
    if argv[:2] == ["login", "status"]:
        return int(script.get("login", 0))
    signal.signal(signal.SIGINT, on_sigint(script))
    # プロンプトは標準入力から。呼ぶ側が閉じるまで読む
    record(argv, sys.stdin.read())
    for step in script.get("steps", []):
        if "emit" in step:
            emit(step["emit"])
        elif "sleep" in step:
            time.sleep(step["sleep"])
        elif "hook" in step:
            with open(os.environ["AUTODEV_HOOK_RECORD"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps(step["hook"], ensure_ascii=False) + "\n")
        elif step.get("hang"):
            while True:
                time.sleep(0.05)
    if script.get("stderr"):
        sys.stderr.write(script["stderr"] + "\n")
    return int(script.get("exit", 0))


if __name__ == "__main__":
    raise SystemExit(main())
