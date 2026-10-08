"""`agent-toolkit install`：配置の表を実行する。"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

from toolkit.catalog import (
    Selection,
    add_selection_arguments,
    placements,
    selection,
)
from toolkit.merge import merge_settings

#: 手順の失敗を表す終了コード
EXIT_STEP_FAILED = 20

# 上げ方: https://tt-a1i.github.io/archify/skill-updates/archify/stable.json の
# version と artifact.sha256 を写す。
ARCHIFY_VERSION = "2.16.0"
ARCHIFY_SHA256 = "4c59fa6557a2385beaaef8c7219cc414573acc9f0c30a932d5053b0b20689a46"
ARCHIFY_URL = f"https://github.com/tt-a1i/archify/releases/download/v{ARCHIFY_VERSION}/archify.zip"

OLD_DIST_SKILLS = "/dist/dot-claude/skills/"
OLD_DIST = "/dist/dot-claude/"


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise FileNotFoundError("pyproject.toml のあるディレクトリが見つからない")


def warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def backup(path: Path, home: Path, *, copy: bool = False) -> Path:
    """実体を ~/.dotbackup/ へ移す（copy=True なら写す）。"""
    root = home / ".dotbackup"
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    dest = root / f"{path.name}.{stamp}"
    n = 1
    while dest.exists() or dest.is_symlink():
        dest = root / f"{path.name}.{stamp}.{n}"
        n += 1
    if copy:
        shutil.copy2(path, dest)
    else:
        shutil.move(str(path), dest)
    print(f"backup {path} -> {dest}")
    return dest


def link(source: Path, target: Path, home: Path) -> None:
    if target.is_symlink():
        if os.readlink(target) == str(source):
            return
        target.unlink()
    elif target.exists():
        backup(target, home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source)
    print(f"link {target} -> {source}")


def merge_json(sources: list[Path], target: Path, home: Path) -> None:
    """同じ配置先へ向かう断片を 1 つにまとめ、配置先へ 1 回だけマージする。"""
    fragment: dict = {}
    for source in sources:
        try:
            part = json.loads(source.read_text(encoding="utf-8"))
        except ValueError as exc:
            warn(f"{source} を読めないので {target} は触らない: {exc}")
            return
        if not isinstance(part, dict):
            warn(f"{source} が JSON のオブジェクトではない。{target} は触らない")
            return
        fragment = merge_settings(fragment, part)
    if target.is_symlink() and not target.exists():
        target.unlink()
    if target.exists():
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except ValueError:
            warn(f"{target} は JSON として読めない（コメント入りなど）。触らない")
            return
        if not isinstance(current, dict):
            warn(f"{target} が JSON のオブジェクトではない。触らない")
            return
        merged = merge_settings(current, fragment)
        if merged == current and not target.is_symlink():
            return
        backup(target, home, copy=True)
    else:
        merged = fragment
        target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if target.exists():
            shutil.copymode(target, tmp)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    print(f"merge {', '.join(str(s) for s in sources)} into {target}")


def ensure_real_dir(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists() and not path.is_dir():
        raise OSError(f"{path} はディレクトリではない")
    path.mkdir(parents=True, exist_ok=True)


def points_to_managed(link_path: Path, repo: Path) -> bool:
    raw = os.readlink(link_path)
    resolved = os.path.normpath(os.path.join(link_path.parent, raw))
    for config in {str(repo / "config"), str(repo.resolve() / "config")}:
        if resolved == config or resolved.startswith(config + os.sep):
            return True
    return OLD_DIST_SKILLS in resolved


def remove_dangling_skill_links(skills_dir: Path, repo: Path) -> None:
    if not skills_dir.is_dir():
        return
    for entry in sorted(skills_dir.iterdir()):
        if entry.is_symlink() and not entry.exists() and points_to_managed(entry, repo):
            entry.unlink()
            print(f"remove dangling link {entry}")


def remove_old_links(home: Path) -> None:
    for name in ("rules", "hooks", "scripts"):
        path = home / ".claude" / name
        if path.is_symlink() and OLD_DIST in os.readlink(path):
            path.unlink()
            print(f"remove old link {path}")


def install_gh_stack() -> None:
    if shutil.which("gh") is None:
        warn("gh not found. skip installing the gh-stack extension")
        return
    listed = subprocess.run(
        ["gh", "extension", "list"], capture_output=True, text=True, check=False
    )
    if "github/gh-stack" in listed.stdout:
        return
    print("install gh extension github/gh-stack ...")
    done = subprocess.run(["gh", "extension", "install", "github/gh-stack"], check=False)
    if done.returncode != 0:
        warn("failed to install gh-stack. run 'gh extension install github/gh-stack' later")


def install_archify(home: Path) -> None:
    dest = home / ".agents" / "skills" / "archify"
    release = dest / "skill-release.json"
    try:
        installed = json.loads(release.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        installed = None
    if installed == ARCHIFY_VERSION:
        return
    print(f"install the archify skill {ARCHIFY_VERSION} ...")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "archify.zip"
        try:
            with urllib.request.urlopen(ARCHIFY_URL, timeout=60) as response:
                archive.write_bytes(response.read())
        except (OSError, http.client.HTTPException) as exc:
            warn(f"failed to download {ARCHIFY_URL}: {exc}. skip installing the archify skill")
            return
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != ARCHIFY_SHA256:
            warn(f"sha256 mismatch for archify {ARCHIFY_VERSION} (actual {actual}). skip")
            return
        out = Path(tmp) / "out"
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            dest.unlink()
        elif dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(out / "archify"), dest)
    print(f"archify skill {ARCHIFY_VERSION} installed to {dest}")


def run_install(repo: Path, home: Path, offline: bool, chosen: Selection) -> int:
    """手順を順に行う。OSError が出た手順は標準エラーに 1 行出して次へ進み、失敗の数を返す。"""
    failures = 0

    def step(label: str, action: Callable[[], None]) -> None:
        nonlocal failures
        try:
            action()
        except OSError as exc:
            failures += 1
            print(f"agent-toolkit install: {label} に失敗: {exc}", file=sys.stderr)

    components = chosen.components
    skills_dirs = (home / ".claude" / "skills", home / ".agents" / "skills")
    if components & {"skills", "extras"}:
        for skills in skills_dirs:
            step(f"{skills} の用意", lambda skills=skills: ensure_real_dir(skills))
    step("旧リンクの移行", lambda: remove_old_links(home))
    merges: dict[Path, list[Path]] = {}
    for placement in placements(repo, home, chosen):
        if placement.method == "link":
            step(
                f"{placement.target} への配置",
                lambda p=placement: link(p.source, p.target, home),
            )
        else:
            merges.setdefault(placement.target, []).append(placement.source)
    for target, sources in merges.items():
        step(f"{target} への配置", lambda t=target, s=sources: merge_json(s, t, home))
    if "extras" in components:
        if not offline:
            step("gh-stack の取得", install_gh_stack)
            step("archify の取得", lambda: install_archify(home))
        archify = home / ".agents" / "skills" / "archify"
        if archify.is_dir():
            step(
                "archify のリンク",
                lambda: link(archify, home / ".claude" / "skills" / "archify", home),
            )
    if "skills" in components:
        for skills in skills_dirs:
            step(
                f"{skills} の片付け",
                lambda skills=skills: remove_dangling_skill_links(skills, repo),
            )
    return failures


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--offline", action="store_true", help="gh-stack と archify のネットワーク取得を飛ばす"
    )
    add_selection_arguments(parser)


def run(args: argparse.Namespace) -> int:
    failures = run_install(repo_root(), Path.home(), args.offline, selection(args))
    if failures:
        print(f"agent-toolkit install: {failures} 件の手順が失敗した", file=sys.stderr)
        return EXIT_STEP_FAILED
    print("agent-toolkit installation completed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-toolkit install")
    add_arguments(parser)
    return run(parser.parse_args(argv))
