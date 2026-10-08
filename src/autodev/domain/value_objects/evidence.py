from __future__ import annotations

from dataclasses import dataclass

from .artifact_ref import ArtifactRef
from .commit_sha import CommitSha
from .deferred_call import DeferredCall
from .gate_report import GateReport
from .stage_exit import StageExit
from .union_verdict import UnionVerdict
from .verify_result import VerifyResult


@dataclass(frozen=True)
class Evidence:
    """実行器が外から集めた証拠。判断は入れない（完了・失敗・エスカレーションを決めるのは Task）。"""

    exit: StageExit
    #: 結果の JSON がスキーマの形をしているか（空なら False）
    result_valid: bool
    #: `produces` の実物を確かめられた成果物
    products: tuple[ArtifactRef, ...] = ()
    #: タスクのブランチの根元（`Task.base_commit`）から HEAD までのコミットの数。根元が無く数えられ
    #: なければ None（0 件と取り違えない）
    commits: int | None = None
    verify: tuple[VerifyResult, ...] = ()
    #: ステージを始める前の軽い検査の結果。取れていなければ None
    baseline: tuple[VerifyResult, ...] | None = None
    deferred: DeferredCall | None = None
    #: Gate のときだけ在る
    gate: GateReport | None = None
    #: CheckUnion のときだけ在る
    union: UnionVerdict | None = None
    #: Rebase が衝突したファイル
    conflicts: tuple[str, ...] = ()
    #: エラーで終わったときの理由（result が無ければ標準エラー）
    error: str | None = None
    #: ガードのフックに拒まれた呼び出しの数。LLM のステージだけが持つ
    hook_denials: int = 0
    #: claude を `--resume` で起こした（LLM のステージだけ）
    resumed: bool = False
    #: claude が `system/init` を出した（セッションを開いてターンを始めた）
    initialized: bool = False
    #: こちらが claude を止めた（interrupt を送った、または result が来ないので kill した）
    stopped_by_us: bool = False
    #: result の `num_turns`。result が無ければ None
    num_turns: int | None = None
    #: 変えないと返せるステージ（`can_keep`）のときの作業ツリーの HEAD。集めていなければ None
    head: CommitSha | None = None
    #: 同じステージのコミットしていない変更のあるファイル。集めていなければ None（空と取り違えない）
    dirty: tuple[str, ...] | None = None
