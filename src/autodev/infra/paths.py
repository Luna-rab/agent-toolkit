"""ランディレクトリの置き場。**パスを呼び出し側で連結しない。**

置き場は対象リポジトリの外で、`$XDG_STATE_HOME/autodev/<ラン名>/`（既定 `~/.local/state`）。
`AUTODEV_STATE_DIR` で `autodev/` までの根を差し替えられる（検査で `~/.local/state` を汚さない）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..domain.value_objects.design_version import DesignVersion
from ..domain.value_objects.execution_id import ExecutionId
from ..domain.value_objects.question_id import QuestionId
from ..domain.value_objects.run_name import RunName
from ..domain.value_objects.task_id import TaskId
from ..domain.value_objects.task_kind import TaskKind

#: `RunPaths.design` が作るファイル名
_DESIGN_FILE = re.compile(r"v([1-9][0-9]*)\.md")


def state_root(env: Mapping[str, str] | None = None) -> Path:
    """ランディレクトリを並べる根。"""
    env = os.environ if env is None else env
    if override := env.get("AUTODEV_STATE_DIR"):
        return Path(override).absolute()
    # XDG の仕様では、相対パスの XDG_STATE_HOME は無効として既定に戻す
    xdg = env.get("XDG_STATE_HOME", "")
    base = Path(xdg) if os.path.isabs(xdg) else Path.home() / ".local" / "state"
    return base / "autodev"


@dataclass(frozen=True)
class RunPaths:
    """1 つのラン（＝1 つのラン名）のパス。"""

    name: RunName
    root: Path

    @classmethod
    def of(cls, name: RunName, env: Mapping[str, str] | None = None) -> RunPaths:
        return cls(name, state_root(env) / name.value)

    @property
    def events_db(self) -> Path:
        return self.root / "events.db"

    @property
    def questions(self) -> Path:
        return self.root / "questions"

    @property
    def answers(self) -> Path:
        """計画ステージの ask への回答。questions/ と分ける（同じ所だと聞いた瞬間に再開する）。"""
        return self.root / "answers"

    def answer(self, tool_use_id: str) -> Path:
        return self.answers / f"{tool_use_id}.json"

    @property
    def progress(self) -> Path:
        return self.root / "progress"

    def progress_of(self, execution: ExecutionId) -> Path:
        return self.progress / f"{execution}.json"

    @property
    def codemap(self) -> Path:
        return self.root / "codemap.md"

    @property
    def designs(self) -> Path:
        return self.root / "design"

    def design(self, version: DesignVersion) -> Path:
        return self.root / version.path

    def design_versions(self) -> list[DesignVersion]:
        """`design/` に書いた版（`design` の逆）。"""
        folder = self.designs
        return [
            DesignVersion(int(found.group(1)))
            for path in (folder.iterdir() if folder.is_dir() else ())
            if (found := _DESIGN_FILE.fullmatch(path.name))
        ]

    def question(self, question: QuestionId) -> Path:
        return self.questions / f"{question}.json"

    @property
    def sessions(self) -> Path:
        """LLM の統括のセッションの控えを並べる所。"""
        return self.root / "sessions"

    def supervisor_session(self, name: str) -> Path:
        """LLM の統括（`Supervisor.name`）のセッションの控え。`--resume` で続きから起こすのに使う。"""
        return self.sessions / f"{name}.json"

    def supervisor_log(self, name: str) -> Path:
        return self.logs / f"supervisor-{name}.jsonl"

    def stage_log(self, execution: ExecutionId) -> Path:
        """LLM のステージの実行 1 回の stream-json のログ。在れば、その実行の claude はもう起こした。"""
        return self.logs / f"{execution}.jsonl"

    def task_results(self, task: TaskId) -> Path:
        return self.root / "tasks" / task.value / "results"

    def baseline(self, execution: ExecutionId) -> Path:
        """ステージを始める前の軽い検査の結果（同じ位置の最初の試みだけが書く）。"""
        return self.task_results(execution.task) / f"{execution}-baseline.json"

    def task_dir(self, task: TaskId) -> Path:
        return self.root / "tasks" / task.value

    @property
    def brief(self) -> Path:
        """Prepare が書くブリーフ（起動時の指示とリポジトリごとの設定）。"""
        return self.root / "brief.md"

    @property
    def overview_body(self) -> Path:
        """WriteOverview が返した概要 PR の本文。マーカーを入れたまま置き、RefreshOverview が毎回ここから
        埋め直す（埋めた本文で上書きすると、後から積んでも古いまま残る）。"""
        return self.root / "overview.md"

    @property
    def overview_title(self) -> Path:
        """WriteOverview が返した概要 PR のタイトル（印の `[autodev] ` を付ける前）。"""
        return self.root / "overview-title.txt"

    def pr_body(self, task: TaskId) -> Path:
        """WritePrBody が返したタスク PR の本文（成果物 pr-body の実物）。"""
        return self.task_dir(task) / "pr-body.md"

    def awaiting_expectations(self, task: TaskId) -> Path:
        """期待値を空けたテストの一覧（成果物 awaiting-expectations の実物）。"""
        return self.task_dir(task) / "awaiting-expectations.json"

    def tree_of(self, task: TaskId) -> Path:
        """タスクのステージの cwd。計画タスクは、再計画も含めて trees/overview。
        git 管理タスクは自分の worktree を持たず、概要ブランチの worktree で gh を叩く。"""
        if task.kind is TaskKind.IMPLEMENTATION:
            return self.task_tree(task)
        return self.overview_tree

    def relative(self, path: Path) -> str:
        """ランディレクトリからのパス（Pointers・成果物の在りか）。"""
        return path.relative_to(self.root).as_posix()

    @property
    def overview_tree(self) -> Path:
        return self.root / "trees" / "overview"

    @property
    def stack_top_tree(self) -> Path:
        return self.root / "trees" / "stack-top"

    def task_tree(self, task: TaskId) -> Path:
        return self.root / "trees" / task.value

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def rejections(self) -> Path:
        """拒んだコマンドの記録（`autodev status` に出す）。"""
        return self.logs / "rejected.jsonl"

    @property
    def guard(self) -> Path:
        return self.root / "guard.json"

    @property
    def driver_lock(self) -> Path:
        """driver が走っている間握る錠（`infra/lock.py`）。"""
        return self.root / "driver.lock"

    @property
    def children(self) -> Path:
        """driver が起こした子プロセスの控え（`adapters/process/children.py`）。"""
        return self.root / "children"

    @property
    def trees(self) -> Path:
        """worktree を並べる所（`clean`・`purge` が外す）。"""
        return self.root / "trees"
