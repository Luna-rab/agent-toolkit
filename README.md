# agent-toolkit

個人用の AI コーディングエージェント（Claude Code・Codex）の設定、Skills、フック、autodev、HUD を管理する。
通常のシェル・Git・開発ツールの設定は [dotfiles](https://github.com/Luna-rab/dotfiles) に置く。

## 責任分担

| 担当 | 持つもの |
| --- | --- |
| dotfiles | 依存 CLI（uv・gh・git・node など）の導入、PATH、シェルとエディタの設定、agent-toolkit の clone、`install.sh` の起動 |
| agent-toolkit | autodev・HUD・Skills・フック、Claude・Codex 別の配置（`agent-toolkit install`）、依存の確認（`doctor`） |
| 各環境 | 認証情報（gh・claude・codex）、履歴、autodev のラン記録。リポジトリに取り込まない |

agent-toolkit から dotfiles へ移す候補と手順は [docs/dotfiles-migration.md](docs/dotfiles-migration.md)。

## 構成

```text
install.sh      uv tool install --editable の後に agent-toolkit install を呼ぶ
pyproject.toml  1 プロジェクト。コマンド autodev・agent-toolkit・agent-hud を宣言する
src/            コード（autodev・turnreview・hud・toolkit）
config/         ホームへ配る設定と指示。コードは置かない
tests/          src/ と同じ形（autodev・turnreview・hud・toolkit・config）
docs/           詳細な説明
```

## 部品とプリセット

配るものは「部品」で選び、個人の設定は「プリセット」で選ぶ。部品とプリセットの定義は `src/toolkit/catalog.py`。

| 部品 | 配るもの | 置き先 | 方法 |
| --- | --- | --- | --- |
| `instructions` | `config/shared/AGENTS.md` | `~/.claude/CLAUDE.md`、`~/.codex/AGENTS.md` | symlink |
| `skills` | `config/shared/skills/<名前>` | `~/.agents/skills/<名前>`、`~/.claude/skills/<名前>` | スキル 1 つずつ symlink |
| | `config/claude/skills/<名前>` | `~/.claude/skills/<名前>` | スキル 1 つずつ symlink |
| `hooks` | `config/claude/hooks.json` | `~/.claude/settings.json` | JSON マージ |
| | `config/codex/hooks.json` | `~/.codex/hooks.json` | JSON マージ |
| `hud` | `config/claude/hud.json`（`statusLine`） | `~/.claude/settings.json` | JSON マージ |
| `vscode` | `config/vscode/settings.json` | `~/.vscode-server/data/Machine/settings.json` | JSON マージ（`~/.vscode-server` があるときだけ） |
| `extras` | gh-stack 拡張、archify スキルの取得 | `gh` の拡張、`~/.agents/skills/archify`（`~/.claude/skills/archify` からリンク） | ネットワーク取得（`--offline` で取得だけ飛ばし、展開済みの archify へのリンクは張る） |

| プリセット | 内容 |
| --- | --- |
| `personal`（既定） | `config/presets/personal/claude/settings.json`（`model`・`permissions`・`env`・`tui`・`extraKnownMarketplaces`・`enabledPlugins`）を `~/.claude/settings.json` にマージする |
| `none` | 何も足さない。`~/.claude/settings.json` に既にあるプリセットのキーも消さない |

`agent-toolkit list` は部品・プリセットと、それぞれが要る依存（必須・任意・認証の要否）を出す。`--json` で機械可読にもなる。

## 依存の準備

依存は部品ごとに違う。何が要るかは `agent-toolkit list` で見る。常に要るのは uv。`skills` は git・claude・gh・gh-stack が必須で、node・codex・copilot が任意。`hooks` は codex が任意。`hud` は git が任意。

dotfiles の mise 設定（`dist/dot-config/mise/config.toml`）で入れる例。uv・node は今の設定にある。gh は無いので足す:

```toml
[tools]
uv = "latest"
node = "lts"
gh = "latest"
```

dotfiles を使わないときは、uv を公式のインストーラ（`curl -LsSf https://astral.sh/uv/install.sh | sh`）などで入れ、gh・git・node は OS のパッケージマネージャで入れる。Claude Code・Codex・Copilot CLI は各公式の手順で入れる。gh-stack は、部品 `extras` を選び、`--offline` を付けず、gh が PATH にあるときに `agent-toolkit install` が `gh extension install github/gh-stack` で入れる。認証（`gh auth login`、`claude`、`codex login`、`copilot login`）は各環境で行う。

## インストール

```sh
git clone git@github.com:Luna-rab/agent-toolkit.git
cd agent-toolkit
./install.sh
```

`install.sh` は `uv tool install --editable --reinstall .` で `autodev`・`agent-toolkit`・`agent-hud` を
`~/.local/bin/` に入れてから、`agent-toolkit install` を実行する。`agent-toolkit install` がすること:

- 部品とプリセットの表のとおりに symlink を張り、JSON をマージする。マージでは配置先にだけあるキーを残し、配列は config 側で置き換える。ただしフックは、`agent-toolkit hook`・`agent-hud` などこのリポジトリが管理する定義だけを入れ替え、ユーザー自身のフックは残す
- 実体のある配置先は `~/.dotbackup/` に移してから置く
- `~/.claude/skills`・`~/.agents/skills` はディレクトリごと symlink にしない（第三者スキルのインストーラが実体をコピーするため）。管理下のスキルを指していた行き先の無い symlink は消す
- 旧配置の `~/.claude/{rules,hooks,scripts}` の symlink（行き先が `/dist/dot-claude/`）は、部品の選び方に関わらず毎回消す
- JSON として読めない配置先（コメント入りなど）は触らず、標準エラーに警告する

`~/.codex/config.toml` と `~/.codex/rules/` は Codex 自身が書くので扱わない。
履歴・資格情報・autodev のラン記録は取り込まない。

### 選び方

```sh
./install.sh --cli-only                                  # コマンドだけ入れ、配置はしない
./install.sh --only instructions skills                  # 部品を選ぶ
./install.sh --skip vscode extras --preset none          # 部品を除き、個人設定も足さない
agent-toolkit install --offline                          # ネットワーク取得（gh-stack・archify）を飛ばす
```

`--only` と `--skip` は同時に指定できない。`HOME` を差し替えれば、実際のホームに触れずに試せる。

### 入口・引数・終了コード（dotfiles が呼ぶ約束）

| 入口 | 終了コード |
| --- | --- |
| `./install.sh --cli-only` | 0 成功 / 2 `--cli-only` とほかの引数の併用 / 10 uv が見つからない / 11 `uv tool install` の失敗 |
| `./install.sh [install の引数…]` | 10、11（`uv tool install` か `uv tool dir --bin` の失敗）に加え、配置に進んだら `agent-toolkit install` の終了コードをそのまま返す。引数は `--cli-only` 以外解釈せず中継する |
| `agent-toolkit install [--offline] [--only C… \| --skip C…] [--preset personal\|none]` | 0 成功 / 2 引数の誤り / 20 手順の一部が失敗（失敗しても残りの手順は最後まで行う） |
| `agent-toolkit doctor [--only C… \| --skip C…] [--preset personal\|none] [--json]` | 0 必須の依存が揃い認証済み / 2 引数の誤り / 30 必須の CLI が無い / 31 必須の CLI はあるが未認証がある（30 と 31 が両方当たるときは 30） |
| `agent-toolkit list [--json]` | 0 成功 / 2 引数の誤り |

`doctor` は任意の依存が無い・未認証でも 0 を返す（一覧には出す）。選んだ部品とプリセットの依存だけを見る。`--json` は `status`・`components`・`preset`・`checks`（`name`・`required`・`installed`・`authenticated`・`needed_by`）を 1 つの JSON で標準出力に出す。

環境変数:

| 名前 | 使われ方 |
| --- | --- |
| `UV` | uv の実行パス。`install.sh` と `doctor` が最初に見る。指定されていて実行できないときは、PATH へ進まず「uv が無い」扱い（`install.sh` は 10、`doctor` は 30） |
| `HOME` | 配置先の基準。`install.sh` が見る `$HOME/.local/bin/mise`（PATH に uv が無いときの `mise which uv`）、`~/.dotbackup/`、`~/.claude` などもここから決まる |

uv の探し方（`install.sh`・`doctor` 共通）は `UV` → PATH の `uv` → `$HOME/.local/bin/mise which uv`。

dotfiles からの呼び方の例:

```sh
./install.sh --preset personal || exit $?   # UV を渡さなければ PATH、次に mise から uv を探す
agent-toolkit doctor --json
```

### 初回に必要な手順

- **Codex のフックは、`agent-toolkit install` の後の初回に、Codex の `/hooks` で autodev のフックを信頼済みとして登録する。** Codex はフック定義のハッシュで信頼を記録する。登録するまでフックは走らない。autodev で Codex を選んだランは、信頼がないと「フックの記録が 0 件」のエラーで落ちる
- **autodev のステージを Codex で走らせるには、クラスごとに選ぶ。** `autodev config set --class <クラス> --agent codex --model <codexのモデル名> [--effort minimal|low|medium|high|xhigh]`。codex には `--model` が要る。`codex login` も要る。詳細は [autodev の README](config/shared/skills/autodev/README.md)
- **Copilot CLI で走らせるときも、クラスごとに選ぶ。** `autodev config set --class <クラス> --agent copilot --model <copilotのモデル名> [--effort none|minimal|low|medium|high|xhigh|max]`。`--model` は必須で `auto` は使えない。`copilot login` が要る。呼び出し元の環境変数と `COPILOT_HOME`（無ければ `~/.copilot`）をそのまま使う。利用枠の上限（`rate_limit`・`quota`）でランは止まる。認証は OS の資格情報ストアを使う環境では未検証で、フックの時間切れでは操作が通りうる。ランの始めの認証の確かめは `config.json` の認証の項目を見るだけ。上限のイベントの形は同梱の型定義から取ったもので、実物では未確認。詳細は [autodev の README](config/shared/skills/autodev/README.md)
- **`uv tool install --editable` のため、依存（`pyproject.toml` の dependencies）を足したら `./install.sh` で再インストールする**

## 開発・検査

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

`.claude/agents/` と `.codex/agents/` はこのリポジトリでの開発用で、ホームへ配らない。
開発の進め方は [AGENTS.md](AGENTS.md)、既存機能の説明は [Claude Code と autodev](docs/claude.md) を参照。

## 更新

```sh
git pull --ff-only
./install.sh
```
