"""ガードの規則。LLM のステージに何を書かせ、何をさせないか。

ここに書くのは「してよいか・いけないか」だけである。実パスをどの場所（`WriteZone`）に振り分けるか、
テストのパスかどうか、コマンド行から宛先と操作を取り出すことは、アダプタ（`adapters/claude/guard.py`）が
翻訳して `WriteTarget` にして渡す。止めた理由（`Refusal`）を文面にするのもアダプタである。

止めるのは、worktree の中のステージごとの書き込み範囲（`WriteScope`）を外れた書き込みと、場所が決まらない
書き込み（`UNKNOWN_PLACE`）だけである。worktree の外（ホーム・ランディレクトリ・対象リポジトリの checkout など）への
書き込みと、`gh`・`git push` は止めない。それらは最初の指示（`system_rules`）で禁じる。

`value_objects/guard.py` の `Guard` の `judge_write`・`judge_ask` が、ここの関数を呼ぶ。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .value_objects.guard import Guard
from .value_objects.write_scope import WriteScope


class WriteZone(Enum):
    """書き込みの宛先がある場所。入れ子の場所は、一番深いものに振り分ける。"""

    #: ステージの worktree。WriteScope が掛かる
    TREE = "tree"
    #: ランディレクトリ（`events.db` がある所。自分の worktree を除く）
    RUN_DIR = "run-dir"
    #: ホームディレクトリ（設定を含む）
    HOME = "home"
    #: 対象リポジトリの手元の checkout。手元のブランチと作業中のファイルに触らない
    TARGET_REPO = "target-repo"
    #: OS の一時ディレクトリ（`/tmp`・`$TMPDIR`）。テストやツールが一時ファイルを置く
    TEMP = "temp"
    #: どれにも入らない場所（OS の権限が守る）
    ELSEWHERE = "elsewhere"
    #: 場所が決まらない（展開できない変数や置換で始まるパス・分からない cwd からの相対パス）
    UNKNOWN = "unknown"


#: ステージ 1 回でフックに拒まれてよい数。これを超えたら打ち切る。何度も止められるステージは指示書を
#: 読み違えていて、ターンの上限まで使っても直らない
HOOK_DENIALS_BEFORE_CUTOFF = 10


def cut_off_by_denials(denials: int) -> bool:
    """フックに拒まれた数から、走っているステージを打ち切るか。"""
    return denials > HOOK_DENIALS_BEFORE_CUTOFF


@dataclass(frozen=True)
class WriteTarget:
    """書き込みの宛先 1 つ。`is_test`・`listed` は TREE のときだけ意味を持つ。"""

    zone: WriteZone
    #: 文面に出すパス（TREE なら worktree の根から）
    path: str
    #: テストのパスに当たるか（ディレクトリなら、中にテストのパスが入りうるか）
    is_test: bool = False
    #: 実行のときに渡された「書いてよいファイル」に入っているか（WriteScope.LISTED）
    listed: bool = False


class RefusalReason(Enum):
    READ_ONLY_TREE = "read-only-tree"
    TESTS_PROTECTED = "tests-protected"
    NON_TESTS_PROTECTED = "non-tests-protected"
    NOT_LISTED = "not-listed"
    UNKNOWN_PLACE = "unknown-place"
    ASK_NOT_ALLOWED = "ask-not-allowed"


@dataclass(frozen=True)
class Refusal:
    reason: RefusalReason
    #: 何を止めたか（パス・操作）
    subject: str = ""
    #: 止めた場所
    zone: WriteZone | None = None


class AskVerdict(Enum):
    """ask の呼び出しへの判断。"""

    #: 回答が無いので、PreToolUse の defer で止めて待つ
    DEFER = "defer"
    #: 回答があるので通す
    PASS = "pass"
    #: このステージは ask で聞けない
    REFUSE = "refuse"


_SCOPE_REFUSAL = {
    WriteScope.NONE: RefusalReason.READ_ONLY_TREE,
    WriteScope.NON_TESTS: RefusalReason.TESTS_PROTECTED,
    WriteScope.TESTS_ONLY: RefusalReason.NON_TESTS_PROTECTED,
    WriteScope.LISTED: RefusalReason.NOT_LISTED,
}


def _scope_allows(scope: WriteScope, target: WriteTarget) -> bool:
    if scope is WriteScope.NONE:
        return False
    if scope is WriteScope.NON_TESTS:
        return not target.is_test
    if scope is WriteScope.TESTS_AND_STUBS:
        # テストとスタブを書く。スタブか実装かはフックで見分けられない
        return True
    if scope is WriteScope.TESTS_ONLY:
        return target.is_test
    return target.listed


def judge_write(guard: Guard, target: WriteTarget) -> Refusal | None:
    zone = target.zone
    if zone is WriteZone.TREE:
        if _scope_allows(guard.writes, target):
            return None
        return Refusal(_SCOPE_REFUSAL[guard.writes], target.path, zone)
    if zone is WriteZone.UNKNOWN:
        # 場所が決まらないものは、書いてよい範囲にかかわらず止める
        return Refusal(RefusalReason.UNKNOWN_PLACE, target.path, zone)
    return None


def judge_ask(guard: Guard, answered: bool) -> AskVerdict:
    """回答のファイルの実在だけで決め、中身を解釈しない。何度再開しても同じ判断になる。"""
    if not guard.can_ask:
        return AskVerdict.REFUSE
    return AskVerdict.PASS if answered else AskVerdict.DEFER
