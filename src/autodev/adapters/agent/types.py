"""エージェント（`claude -p`・`codex exec`・`copilot -p`）を問わない、起動条件と結果の形。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ...domain.value_objects.model_class import AgentKind
from ...domain.value_objects.session_id import SessionId

#: interrupt を送ってから result を待つ時間。過ぎたら kill する
INTERRUPT_GRACE = 60.0
#: 進み具合を知らせる間隔。ステージ 1 回で数百のイベントが流れるので、毎回は知らせない
PROGRESS_INTERVAL = 5.0
#: 利用枠の上限の文言。上限に当てた形は実物で確かめていない
_RATE_LIMIT_TEXT = re.compile(r"usage limit|hit your limit|rate limit|rate_limit", re.IGNORECASE)
#: 上限ではない一時的な失敗の文言。claude 2.1.288 の実行ファイルの中にある、529 の過負荷と、
#: 「usage limit」の語を含むが上限ではないと言う 429 の文言。待てば通るので、パニックにしない
_NOT_RATE_LIMIT_TEXT = re.compile(r"is experiencing high load|not your usage limit", re.IGNORECASE)


@dataclass(frozen=True)
class AgentCall:
    """エージェント 1 回の起動条件。"""

    #: None なら何も送らない（defer で止まったセッションの再開）
    prompt: str | None
    cwd: str
    session: SessionId
    #: True なら `--resume <session>`、False なら `--session-id <session>`
    resume: bool
    #: JSONL を書き足すファイル。標準エラーは `<log>.err` に書き足す
    log_path: str
    #: 結果の形（JSON Schema draft-07 の本文。パスではない）
    json_schema: str | None = None
    model: str | None = None
    effort: str | None = None
    max_turns: int | None = None
    #: フックを書いた設定（`guard.json`）のパス
    settings: str | None = None
    system_append: str | None = None
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    #: 足す環境変数（ガードの `AUTODEV_GUARD` など）
    env: Mapping[str, str] = field(default_factory=dict)
    #: これを過ぎたら interrupt を送る（秒）。None なら待ち続ける
    timeout: float | None = None
    #: どのエージェントで起動するか。`AgentRouter` が見て実装を選ぶ
    agent: AgentKind = AgentKind.CLAUDE
    #: ランディレクトリ。Codex が `sessions/codex/` と `answers/` を引く。Claude は使わない
    run_dir: str | None = None


class Ending(Enum):
    """プロセスの終わり方。"""

    #: result が来て、プロセスが終わった
    RESULT = "result"
    #: result が来ないまま、プロセスが自分で終わった（引数の誤りなど）
    NO_RESULT = "no-result"
    #: result が来ないので、こちらが kill した（interrupt が効かなかった・result の後も終わらない）
    KILLED = "killed"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(frozen=True)
class DeferredToolUse:
    """PreToolUse のフックの defer で止まった呼び出し。"""

    tool_use_id: str
    name: str
    input: Mapping[str, Any]


@dataclass(frozen=True)
class Progress:
    """走っている間の進み具合。"""

    #: 届いた assistant のメッセージの数（result の `num_turns` とは数え方が違いうる）
    turns: int
    #: 直前に呼んだツールの名前
    last_tool: str | None
    #: フックに拒まれた呼び出しの数
    hook_denials: int
    events: int


@dataclass(frozen=True)
class AgentOutcome:
    """起きた事実。"""

    ending: Ending
    exit_code: int | None
    session: SessionId
    #: result の `subtype`（`success`・`error_max_turns`・`error_during_execution` など）
    subtype: str | None = None
    is_error: bool = False
    stop_reason: str | None = None
    terminal_reason: str | None = None
    num_turns: int = 0
    #: result の `result`（最後の応答の本文）。無ければ `errors` をつないだもの
    text: str = ""
    #: `structured_output`。返らなかった・object でなければ None
    structured: Mapping[str, Any] | None = None
    usage: Usage = Usage()
    cost_usd: float = 0.0
    api_error_status: int | None = None
    permission_denials: tuple[Mapping[str, Any], ...] = ()
    hook_denials: int = 0
    deferred: DeferredToolUse | None = None
    rate_limited: bool = False
    #: こちらから interrupt を送ったか（送った理由）
    interrupted: str | None = None
    #: 標準エラーの末尾
    stderr: str = ""
    capabilities: tuple[str, ...] = ()
    log_path: str = ""
    #: `system/init` を 1 回でも受けた（claude がセッションを開いてターンを始めた）。`--resume` で
    #: 続けるセッションが見つからないと、claude 2.1.288 は init を出さず、`error_during_execution`・
    #: `num_turns: 0` の result を返して終了コード 1 で終わる（プロンプトの有無で変わらない。段 6 の実測）
    initialized: bool = False


def limit_text(text: str) -> bool:
    return bool(_RATE_LIMIT_TEXT.search(text)) and not _NOT_RATE_LIMIT_TEXT.search(text)
