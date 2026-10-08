"""`agent-toolkit doctor`（依存の確認）と `agent-toolkit list`（部品・プリセット・依存の一覧）。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from toolkit.catalog import (
    CLI_DEPENDENCIES,
    COMPONENTS,
    PRESETS,
    Dependency,
    Selection,
    add_selection_arguments,
    selection,
)

#: 外部コマンドを待つ上限（秒）
TIMEOUT = 15


@dataclass(frozen=True)
class Check:
    name: str
    required: bool
    installed: bool
    authenticated: bool | None  # 認証を確かめない依存と、入っていない依存は None
    needed_by: tuple[str, ...]  # 部品名・"preset:personal"・"cli"。カタログの順


def _run(command: list[str]) -> tuple[int, str] | None:
    """(終了コード, stdout)。実行できない・時間切れなら None。対話を始めないよう stdin を閉じる。"""
    env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode, done.stdout


def _succeeds(command: list[str]) -> bool:
    result = _run(command)
    return result is not None and result[0] == 0


def _stdout(command: list[str]) -> str | None:
    result = _run(command)
    return None if result is None else result[1]


def _uv_installed(home: Path) -> bool:
    uv = os.environ.get("UV")
    if uv:
        return os.path.isfile(uv) and os.access(uv, os.X_OK)
    if shutil.which("uv"):
        return True
    mise = home / ".local" / "bin" / "mise"
    if not (mise.is_file() and os.access(mise, os.X_OK)):
        return False
    found = _stdout([str(mise), "which", "uv"])
    return bool(found and found.strip())


def _installed(name: str, home: Path) -> bool:
    if name == "uv":
        return _uv_installed(home)
    if name == "gh-stack":
        if shutil.which("gh") is None:
            return False
        return "github/gh-stack" in (_stdout(["gh", "extension", "list"]) or "")
    return shutil.which(name) is not None


def _authenticated(name: str) -> bool:
    commands = {
        "gh": ["gh", "auth", "status"],
        "claude": ["claude", "auth", "status"],
        "codex": ["codex", "login", "status"],
    }
    return name in commands and _succeeds(commands[name])


def _selected_dependencies(chosen: Selection) -> list[tuple[str, tuple[Dependency, ...]]]:
    sources: list[tuple[str, tuple[Dependency, ...]]] = [("cli", CLI_DEPENDENCIES)]
    sources += [(c.name, c.dependencies) for c in COMPONENTS if c.name in chosen.components]
    sources += [(f"preset:{p.name}", p.dependencies) for p in PRESETS if p.name == chosen.preset]
    return sources


def check(chosen: Selection, home: Path) -> list[Check]:
    merged: dict[str, tuple[Dependency, bool, list[str]]] = {}
    for source, dependencies in _selected_dependencies(chosen):
        for dep in dependencies:
            first, required, needed_by = merged.get(dep.name, (dep, False, []))
            merged[dep.name] = (first, required or dep.required, [*needed_by, source])
    checks = []
    for name, (dep, required, needed_by) in merged.items():
        installed = _installed(name, home)
        authenticated = _authenticated(name) if installed and dep.auth else None
        checks.append(Check(name, required, installed, authenticated, tuple(needed_by)))
    return checks


def status(checks: list[Check]) -> int:
    if any(c.required and not c.installed for c in checks):
        return 30
    if any(c.required and c.authenticated is False for c in checks):
        return 31
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    add_selection_arguments(parser)
    parser.add_argument("--json", action="store_true", help="JSON で出す")


def _mark(item: Check) -> str:
    if not item.installed:
        return "未インストール"
    if item.authenticated is None:
        return "ok"
    return "ok（認証済み）" if item.authenticated else "未認証"


def run(args: argparse.Namespace) -> int:
    chosen = selection(args)
    checks = check(chosen, Path.home())
    code = status(checks)
    if args.json:
        payload = {
            "status": code,
            "components": [c.name for c in COMPONENTS if c.name in chosen.components],
            "preset": chosen.preset,
            "checks": [asdict(c) for c in checks],
        }
        print(json.dumps(payload, ensure_ascii=False))
        return code
    for item in checks:
        kind = "必須" if item.required else "任意"
        print(f"{item.name:<10} {kind}  {_mark(item):<14} {', '.join(item.needed_by)}")
    print(f"status: {code}")
    return code


def list_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="JSON で出す")


def _dependency_line(dep: Dependency) -> str:
    kind = "必須" if dep.required else "任意"
    return f"    {dep.name} ({kind}{'・認証' if dep.auth else ''}): {dep.purpose}"


def run_list(args: argparse.Namespace) -> int:
    if args.json:
        payload = {
            "components": [asdict(c) for c in COMPONENTS],
            "presets": [asdict(p) for p in PRESETS],
            "cli": [asdict(d) for d in CLI_DEPENDENCIES],
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    print("部品:")
    for c in COMPONENTS:
        print(f"  {c.name}: {c.description}")
        for dep in c.dependencies:
            print(_dependency_line(dep))
    print("プリセット:")
    for p in PRESETS:
        print(f"  {p.name}: {p.description}")
        for dep in p.dependencies:
            print(_dependency_line(dep))
    print("常に要るもの:")
    for dep in CLI_DEPENDENCIES:
        print(_dependency_line(dep))
    return 0
