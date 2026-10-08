"""`autodev` のサブコマンド。引数を読んでコマンドを組み、requests に置くか driver を組んで回し、
終了コードを返すだけにする。進め方の判断は持たない。

機械が読む出力は標準出力に JSON で、知らせと落ちた理由は標準エラーに出す。

終了コード:

| コード | 意味 |
| --- | --- |
| 0 | ランを終えた（`run`）・頼んだことを済ませた（ほかのサブコマンド） |
| 1 | 起動できなかった・頼んだことをしなかった。引数の誤りも 1 にする |
| 3 | パニック（`run`）。原因を取り除いて同じラン名で呼び直す |
| 4 | 回答待ちで、進められるタスクが無い（`run`）。回答を置いて同じラン名で呼び直す |
| 5 | そのランが無い（`status --name`。events.db が無い）。ラン名の誤りは 1 |

2 は `hook` だけが使う（呼び出しを止める印）。ほかのサブコマンドは使わない。argparse は引数の誤りを 2 で返すので、`_Parser` で 1 に替える。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from .adapters.claude.guard import ANSWER_FILE_OPTION, DENY_EXIT, GUARD_ENV
from .adapters.codex.hook import DENY_WRITES, PARK_ON_ASK, run_codex_hook
from .adapters.github import git as git_adapter
from .adapters.github.git import Git
from .adapters.process.command import CommandFailed
from .app.driving import assembly, cleanup
from .app.driving.driver import ExitCode, StartRequest
from .domain.commands.questions import AnswerQuestion
from .domain.commands.run import StartRun
from .domain.events.run import RunStarted
from .domain.services.housekeeping import (
    Blocker,
    Leftovers,
    blocking,
    clean_blockers,
    purge_blockers,
    start_blockers,
)
from .domain.value_objects.base import InvalidValue
from .domain.value_objects.branch_name import BranchName
from .domain.value_objects.command_id import CommandId
from .domain.value_objects.instruction import Instruction
from .domain.value_objects.issuer import Issuer
from .domain.value_objects.model_class import (
    AgentKind,
    Effort,
    ModelClass,
    ModelClasses,
    ModelName,
)
from .domain.value_objects.question_id import QuestionId
from .domain.value_objects.repository import Repository
from .domain.value_objects.run_name import RunName
from .infra.lock import DriverBusy, DriverLock
from .infra.model_config import ModelConfigError, load_model_classes, models_path, set_model_class
from .infra.paths import RunPaths
from .infra.repo_config import RepoConfigError, config_path, load_repo_config
from .infra.store.db import EventsDbMissing
from .infra.store.eventstore import EventReader
from .infra.store.requests import RequestBox

OK = 0
FAILED = int(ExitCode.FAILED)


class Failed(Exception):
    """頼まれたことをせずに、終了コード 1 で終える。文面は標準エラーに出す。"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(FAILED, f"{self.prog}: {message}\n")


def _say(message: str) -> None:
    sys.stderr.write(f"autodev: {message}\n")


def _emit(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _value(kind: type[Any], text: str, what: str) -> Any:
    try:
        return kind(text)
    except InvalidValue as error:
        raise Failed(f"{what}が使えない: {error}") from error


def _paths(name: str) -> RunPaths:
    return RunPaths.of(_value(RunName, name, "ラン名"))


def _existing(name: str) -> RunPaths:
    paths = _paths(name)
    if not paths.root.is_dir():
        raise Failed(f"そのランが無い: {paths.root}")
    return paths


def _text(value: str | None, file: str | None) -> str | None:
    """`--x` か `--x-file`（`-` なら標準入力）の中身。どちらも無ければ None。"""
    if file is None:
        return value
    if file == "-":
        return sys.stdin.read()
    try:
        return Path(file).read_text(encoding="utf-8")
    except OSError as error:
        raise Failed(f"{file} を読めない: {error}") from error


# --- run ---


def _repository(given: str) -> Repository:
    top = git_adapter.toplevel(Path(given).absolute())
    if top is None:
        raise Failed(f"git のリポジトリが見つからない: {given}")
    return Repository(str(top))


def _resume_checks(args: argparse.Namespace, paths: RunPaths, started: RunStarted) -> None:
    """既にあるランに、変えられない値を違う値で渡したら止める。黙って捨てると、呼んだ側は変えたつもり
    になる。"""
    if args.instruction is not None or args.instruction_file is not None:
        raise Failed(
            f"{paths.name} は既にある。続きから始めるときは --instruction を付けない"
            "（質問への回答は `autodev answer` で渡す）"
        )
    if args.repo is not None and _repository(args.repo) != started.repository:
        raise Failed(f"{paths.name} の対象リポジトリは {started.repository} で、--repo と違う")
    if args.base is not None and args.base != started.base.value:
        raise Failed(f"{paths.name} の base は {started.base} で、--base と違う")


def _instruction(args: argparse.Namespace) -> Instruction:
    text = _text(args.instruction, args.instruction_file)
    if text is None:
        raise Failed("新しいランには --instruction か --instruction-file が要る")
    return _value(Instruction, text.strip(), "指示")


def _start_command(
    args: argparse.Namespace,
    name: RunName,
    instruction: Instruction,
    repository: Repository,
    models: ModelClasses,
) -> StartRun:
    repo = Git(repository.value)
    overview = BranchName.overview(name)
    # ランディレクトリを消してもブランチは残る。残ったブランチを CutBranch が作り済みと
    # して使うと、前のランのコミットの上から始まる
    repo.fetch()
    if repo.remote_branch_exists(overview) or repo.branch_exists(overview):
        raise Failed(
            f"ラン名 {name} は使われている（{overview} が手元か origin にある）。別のラン名にする"
        )
    base = _value(BranchName, args.base, "base") if args.base else repo.default_branch()
    return StartRun(
        command_id=CommandId(f"start/{name}"),
        issuer=Issuer.cli(),
        name=name,
        instruction=instruction,
        repository=repository,
        base=base,
        models=models,
    )


def cmd_run(args: argparse.Namespace) -> int:
    paths = _paths(args.name)
    started = assembly.run_started(paths)
    instruction: Instruction | None = None
    models: ModelClasses | None = None
    if started is None:
        instruction = _instruction(args)
        if args.repo is None:
            raise Failed("新しいランには --repo が要る")
        # 新しいランだけが読む。呼び直したランは RunStarted に記録した値を使う
        models = _models()
    # 対象リポジトリを git で探す前に確かめる。git が無いと、リポジトリが無いという違う理由で落ちる
    # models を記録する前に始めたラン（None）は既定値で動かす
    chosen = models or (started.models if started is not None else None) or ModelClasses.default()
    if missing := assembly.missing_tools(Path.cwd(), chosen):
        raise Failed("足りないものがあるので走らない: " + " / ".join(missing))
    if started is not None:
        _resume_checks(args, paths, started)
        repository = started.repository
    else:
        repository = _repository(args.repo)
    try:
        config = load_repo_config(repository)
    except RepoConfigError as error:
        raise Failed(str(error)) from error
    if config.source is None:
        _say(f"リポジトリの設定が無いので既定で走る（置くなら {config_path(repository)}）")
    start = (
        None
        if instruction is None or models is None
        else StartRequest(_start_command(args, paths.name, instruction, repository, models))
    )
    _say(f"{paths.name} を{'続きから始める' if start is None else '始める'}（記録: {paths.root}）")
    try:
        with DriverLock(paths.driver_lock):
            # 錠が取れたので前の driver はもういない。その driver が起こした子が残っていないかを見る
            _refuse(
                blocking(start_blockers(Leftovers(live=cleanup.live_children(paths))), force=False),
                forcible_here=False,
            )
            return int(assembly.build_driver(paths, config).drive(start))
    except DriverBusy as error:
        raise Failed(str(error)) from error


# --- config ---


def _models() -> ModelClasses:
    try:
        return load_model_classes()
    except ModelConfigError as error:
        raise Failed(str(error)) from error


def _emit_models(models: ModelClasses) -> None:
    path = models_path()
    _emit(
        {
            "path": str(path),
            "exists": path.is_file(),
            "classes": {
                cls.value: {
                    "model": models.of(cls).model.value,
                    "effort": models.of(cls).effort.value,
                    "agent": models.of(cls).agent.value,
                }
                for cls in ModelClass
            },
        }
    )


def cmd_config_show(args: argparse.Namespace) -> int:
    _emit_models(_models())
    return OK


def cmd_config_set(args: argparse.Namespace) -> int:
    if args.model is None and args.effort is None and args.agent is None:
        raise Failed("--model・--effort・--agent の少なくとも 1 つが要る")
    model = None if args.model is None else _value(ModelName, args.model, "モデル")
    try:
        models = set_model_class(
            ModelClass(args.cls),
            model=model,
            effort=None if args.effort is None else Effort(args.effort),
            agent=None if args.agent is None else AgentKind(args.agent),
        )
    except ModelConfigError as error:
        raise Failed(f"{error}（何も書いていない）") from error
    _say("次に始めるランから効く。走っているランは始めたときの値を使い続ける")
    _emit_models(models)
    return OK


# --- status・events ---


def cmd_status(args: argparse.Namespace) -> int:
    from .infra.status.status import all_statuses, run_status  # noqa: PLC0415

    if args.name is None:
        _emit(all_statuses())
        return OK
    paths = _paths(args.name)
    try:
        _emit(run_status(paths))
    except EventsDbMissing as error:
        # 開いた時点の事実で決める。後で見直すと、その間に driver が events.db を作ったときに取り違える。
        # HUD は 5 を見て、ランが消えたと見分ける
        _say(f"そのランが無い: {error}")
        return int(ExitCode.RUN_NOT_FOUND)
    except FileNotFoundError as error:
        raise Failed(f"ランを読めない: {error}") from error
    return OK


def cmd_events(args: argparse.Namespace) -> int:
    paths = _paths(args.name)
    try:
        with EventReader.open(paths.events_db) as reader:
            _emit([stored.to_json() for stored in reader.read_all()])
    except FileNotFoundError as error:
        raise Failed(f"そのランが無い: {error}") from error
    return OK


# --- answer・ask ---


def cmd_answer(args: argparse.Namespace) -> int:
    paths = _paths(args.name)
    if not paths.events_db.is_file():
        raise Failed(f"そのランが無い: {paths.events_db}")
    question = _value(QuestionId, args.question, "質問の id")
    text = _text(args.answer, args.answer_file) or ""
    refusal = assembly.answer_refusal(
        paths,
        AnswerQuestion(
            command_id=CommandId("answer/check"),
            issuer=Issuer.cli(),
            question=question,
            answer=text,
        ),
    )
    if refusal is not None:
        raise Failed(f"回答を置かなかった: {refusal}")
    with RequestBox.open(paths.events_db) as box:
        request = box.put(AnswerQuestion, {"question": question, "answer": text})
    # driver を起こし直すのは answer の役目ではない
    _say(f"回答を置いた。`autodev run --name {paths.name}` を呼び直すと driver が受け取る")
    _emit({"request": request, "question": question.value})
    return OK


def cmd_ask(args: argparse.Namespace) -> int:
    """計画ステージが Bash から呼ぶ。普通はガードのフック（park-on-ask）が呼び出しを defer で止め、
    回答のファイルが置かれてから `--answer-file <answers/<tool_use_id>.json>` を足して通す。

    `--answer-file` が無いのは、フックが止められなかった（ほかのツールと同じターンで呼んだ）ときで、
    回答を待てないので単独で呼び直させる。
    """
    if args.answer_file is None:
        raise Failed(
            "回答を待って止まれなかった。このコマンドは、ほかのツールを呼ばないターンで単独に呼び直す"
        )
    try:
        loaded = json.loads(Path(args.answer_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Failed(f"回答のファイル {args.answer_file} を読めない: {error}") from error
    answer = loaded.get("answer") if isinstance(loaded, dict) else None
    if not isinstance(answer, str):
        raise Failed(f"回答のファイル {args.answer_file} に answer が無い")
    sys.stdout.write(answer if answer.endswith("\n") else answer + "\n")
    return OK


def cmd_hook(args: argparse.Namespace) -> int:
    """Codex の PreToolUse から呼ばれる。ガード（AUTODEV_GUARD）の下では、落ちたときも 2 で止める。"""
    try:
        reply = run_codex_hook(args.handler, sys.stdin.read(), os.environ)
    except Exception as error:
        if not os.environ.get(GUARD_ENV):
            return OK
        sys.stderr.write(f"ガードのフックが落ちたので、呼び出しを止めました: {error}\n")
        return DENY_EXIT
    if reply.stdout:
        sys.stdout.write(reply.stdout + "\n")
    if reply.stderr:
        sys.stderr.write(reply.stderr + "\n")
    return reply.exit_code


# --- clean・purge ---


def _refuse(blockers: Sequence[Blocker], *, forcible_here: bool = True) -> None:
    """ドメインが挙げた、越えられない理由があれば止める。理由の文に、進め方が書いてある。

    `forcible_here` は、そのサブコマンドに `--force` があるか（run には無いので、出さない）。
    """
    if not blockers:
        return
    for blocker in blockers:
        _say(blocker.reason)
    if forcible_here and all(blocker.forcible for blocker in blockers):
        raise Failed("何もしていない。失うものを承知で進めるなら --force を付ける")
    if forcible_here:
        raise Failed("何もしていない（--force でも進めない）")
    raise Failed("何もしていない")


def cmd_clean(args: argparse.Namespace) -> int:
    paths = _existing(args.name)
    try:
        with DriverLock(paths.driver_lock):
            started = assembly.run_started(paths)
            repository = started.repository if started else None
            found = cleanup.leftovers(paths, repository, count_unpushed=False)
            finished = assembly.run_aggregate(paths).complete
            _refuse(blocking(clean_blockers(found, finished=finished), force=args.force))
            removed = cleanup.remove_worktrees(paths, repository)
    except DriverBusy as error:
        raise Failed(str(error)) from error
    _say(f"記録は残してある: {paths.root}")
    _emit({"removed": [str(tree) for tree in removed], "kept": str(paths.root)})
    return OK


def cmd_purge(args: argparse.Namespace) -> int:
    paths = _existing(args.name)
    try:
        with DriverLock(paths.driver_lock):
            started = assembly.run_started(paths)
            repository = started.repository if started else None
            found = cleanup.leftovers(paths, repository, count_unpushed=True)
            _refuse(blocking(purge_blockers(found), force=args.force))
            branches = cleanup.purge(paths, repository)
    except DriverBusy as error:
        # driver が走っている間は --force でも消さない。消すと、走っている driver の記録が消える
        raise Failed(str(error)) from error
    _say("GitHub の PR とリモートのブランチは残してある")
    _emit({"deleted": str(paths.root), "branches": [branch.value for branch in branches]})
    return OK


# --- 引数 ---


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="autodev", description="autodev の driver と、/autodev・ステージの入口")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="ランを始める・続きから進める")
    run.add_argument("--name", required=True, help="ラン名（英小文字・数字・-）")
    given = run.add_mutually_exclusive_group()
    given.add_argument("--instruction", help="指示の本文（新しいランだけ）")
    given.add_argument("--instruction-file", help="指示の本文のファイル（- なら標準入力）")
    run.add_argument("--repo", help="対象リポジトリ（新しいランでは要る）")
    run.add_argument("--base", help="ランの base（省けば origin の既定ブランチ）")
    run.set_defaults(func=cmd_run)

    config = sub.add_parser("config", help="クラスごとのモデルと effort の設定")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    show = config_sub.add_parser("show", help="今の設定と設定ファイルのパスを JSON で出す")
    show.set_defaults(func=cmd_config_show)
    put = config_sub.add_parser("set", help="設定ファイルの、1 つのクラスの欄を書き換える")
    put.add_argument(
        "--class", dest="cls", required=True, choices=[cls.value for cls in ModelClass]
    )
    put.add_argument("--model", help="エージェントの --model に渡す名前")
    put.add_argument("--effort", choices=[effort.value for effort in Effort])
    put.add_argument("--agent", choices=[agent.value for agent in AgentKind])
    put.set_defaults(func=cmd_config_set)

    status = sub.add_parser("status", help="ランの状態を JSON で出す")
    status.add_argument("--json", action="store_true", required=True)
    status.add_argument("--name", help="省けば、すべてのラン")
    status.set_defaults(func=cmd_status)

    events = sub.add_parser("events", help="ランのイベントを JSON で書き出す")
    events.add_argument("--name", required=True)
    events.set_defaults(func=cmd_events)

    answer = sub.add_parser("answer", help="質問に答える（requests に置く）")
    answer.add_argument("--name", required=True)
    answer.add_argument("--question", required=True, help="questions/<id>.json の id")
    body = answer.add_mutually_exclusive_group(required=True)
    body.add_argument("--answer", help="回答の本文")
    body.add_argument("--answer-file", help="回答の本文のファイル（- なら標準入力）")
    answer.set_defaults(func=cmd_answer)

    ask = sub.add_parser("ask", help="計画ステージが質問する（単独で呼ぶ）")
    ask.add_argument("--question", required=True)
    # ガードのフックが、回答のファイルを置いた後に足す
    ask.add_argument(ANSWER_FILE_OPTION, dest="answer_file", help=argparse.SUPPRESS)
    ask.set_defaults(func=cmd_ask)

    hook = sub.add_parser("hook", help="Codex のガードのフック（PreToolUse）。標準入力を読む")
    hook.add_argument("handler", choices=[DENY_WRITES, PARK_ON_ASK])
    hook.set_defaults(func=cmd_hook)

    clean = sub.add_parser("clean", help="終えたランの worktree を外す（記録は残す）")
    clean.add_argument("--name", required=True)
    clean.add_argument(
        "--force", action="store_true", help="終えていない・コミットしていない変更があっても外す"
    )
    clean.set_defaults(func=cmd_clean)

    purge = sub.add_parser("purge", help="worktree・手元のブランチ・ランディレクトリを消す")
    purge.add_argument("--name", required=True)
    purge.add_argument(
        "--force",
        action="store_true",
        help="走っている記録・push していないコミット・コミットしていない変更があっても消す",
    )
    purge.set_defaults(func=cmd_purge)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="autodev: %(message)s")
    try:
        return args.func(args)
    except (Failed, CommandFailed) as error:
        # CommandFailed は、走り出す前と片付けの git・gh が落ちたとき（ラン名を確かめる fetch など）
        _say(str(error))
        return FAILED
