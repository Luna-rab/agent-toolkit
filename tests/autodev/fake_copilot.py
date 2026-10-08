"""検査で `copilot -p --output-format json` の代わりに起動する偽物。本物は叩かない。

`FAKE_COPILOT_SCRIPT` に台本の JSON ファイルを渡す。台本は次の形:

- `steps`: 上から順にする。`{"emit": <JSONL の 1 行>}`・`{"sleep": 秒}`・`{"hook": <フックの記録の 1 行>}`
  （`AUTODEV_HOOK_RECORD` のファイルへ書き足す）・`{"hang": true}`（SIGINT が来るまで待つ）
- `exit`: 台本を終えたときの終了コード（既定 0）
- `sigint`: `"abort"` なら `abort` イベントを流して終了コード 0 で終わる（実測の形）。`"ignore"` なら無視する
- `stderr`: 標準エラーに書く文
- `version`: `copilot --version` の終了コード（既定 0）

`FAKE_COPILOT_RECORD` に、起動のたびに受けた引数・環境変数・`COPILOT_HOME/config.json` の中身を
JSON の 1 行で書き足す。SIGINT を受けたら `FAKE_COPILOT_SIGNALS` に 1 行書き足す。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

KEPT_ENV = (
    "COPILOT_HOME",
    "COPILOT_PROVIDER_API_KEY",
    "COPILOT_PROVIDER_BASE_URL",
    "COPILOT_MODEL",
    "COPILOT_OFFLINE",
    "COPILOT_GITHUB_TOKEN",
    "AUTODEV_HOOK_RECORD",
    "AUTODEV_GUARD",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_CONFIG_DIR",
    "GH_ENTERPRISE_TOKEN",
    "GIT_CONFIG_COUNT",
    "GIT_TERMINAL_PROMPT",
)


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def home_config() -> object:
    home = os.environ.get("COPILOT_HOME")
    if not home:
        return None
    try:
        with open(os.path.join(home, "config.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def record(argv: list[str]) -> None:
    path = os.environ.get("FAKE_COPILOT_RECORD")
    if not path:
        return
    body = {
        "argv": argv,
        "cwd": os.getcwd(),
        "env": {k: os.environ[k] for k in KEPT_ENV if k in os.environ},
        "prompt": option(argv, "-p"),
        "home_config": home_config(),
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(body, ensure_ascii=False) + "\n")


def on_sigint(script: dict):
    def handler(signum, frame):
        path = os.environ.get("FAKE_COPILOT_SIGNALS")
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("SIGINT\n")
        if script.get("sigint", "abort") == "abort":
            emit({"type": "abort", "data": {"reason": "user_abort"}})
            os._exit(0)

    return handler


def main() -> int:
    argv = sys.argv[1:]
    script_path = os.environ.get("FAKE_COPILOT_SCRIPT")
    script = {}
    if script_path:
        with open(script_path, encoding="utf-8") as fh:
            script = json.load(fh)
    if argv == ["--version"]:
        sys.stdout.write("GitHub Copilot CLI 0.0.0 (fake)\n")
        return int(script.get("version", 0))
    signal.signal(signal.SIGINT, on_sigint(script))
    record(argv)
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
