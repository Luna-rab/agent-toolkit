# Claude Code configuration

## Claude Code (`~/.claude/`)

`config/` 配下を全環境で共通利用する。`agent-toolkit install`（`./install.sh` が呼ぶ）が次を行う:

- `config/shared/AGENTS.md` を `~/.claude/CLAUDE.md`（と Codex の `~/.codex/AGENTS.md`）に symlink（編集が即反映）
- `config/shared/skills/` と `config/claude/skills/` の中のスキルを 1 つずつ `~/.claude/skills/` に symlink
  （`~/.claude/skills` 自体は実ディレクトリ。理由は下の「スキル」）
- `settings.json` は symlink せず、`config/claude/hud.json`・個人プリセット
  （`config/presets/personal/claude/settings.json`）を `~/.claude/settings.json` にマージ（理由は下の「設定ファイルのマージ」）

既存の `projects/` `history.jsonl` 等ランタイムデータは温存される。

### スキル（`~/.claude/skills/`）

`~/.claude/skills` はディレクトリごと symlink にしてはいけない。ここは第三者スキルの
インストーラ（`npx skills add <owner>/<repo> -g`）が実体をコピーする先でもあり、
symlink にしているとコピーがこのリポジトリの中に落ちる。

そこで `agent-toolkit install` は `~/.claude/skills` を実ディレクトリのまま置き、
`config/shared/skills/` と `config/claude/skills/` の中のスキルだけを 1 つずつ symlink する。
Claude Code はスキル 1 つ単位の symlink も辿る（v2.1.273 で確認）。リポジトリから消したスキルの
symlink は次のインストールが片付ける。手で入れたスキルは symlink ではないので残る。

### archify（図を作るスキル）

[archify](https://github.com/tt-a1i/archify) は構成図・フロー図・シーケンス図・データフロー図・
状態遷移図を、ブラウザで開いて操作できる 1 ファイルの HTML にする。`agent-toolkit install` が release に
添付された配布用 zip を取り、sha256 を照合してから `~/.agents/skills/archify` に展開し、
`~/.claude/skills/archify` からリンクする。入れるバージョンと sha256 は `src/toolkit/install.py` の
`ARCHIFY_VERSION`・`ARCHIFY_SHA256` に書いてある。

- **上げ方**: <https://tt-a1i.github.io/archify/skill-updates/archify/stable.json> の `version` と
  `artifact.sha256` を `src/toolkit/install.py` の `ARCHIFY_VERSION`・`ARCHIFY_SHA256` に写して `./install.sh` を実行する。
- **`npx skills add tt-a1i/archify -g` は使わない**。default ブランチの HEAD を丸ごとコピーする
  ので、devcontainer を作り直すたびに別のバージョン（実測では開発版 2.17.0-dev.1）が入り、
  テスト一式まで付いてくる。
- 動かすのに要るのは `node` だけ（dotfiles の mise 設定の `node = "lts"` が入れる）。
  npm パッケージは要らない。
- **PNG / WebM の書き出しと `archify visual-check` は Chrome か Chromium を PATH から探す**。
  devcontainer にはどちらも無いのでその 2 つは動かない。HTML を作る `validate` / `deliver` は動く。
  ブラウザの場所を渡すなら `ARCHIFY_CHROME`、コンテナで sandbox を切るなら
  `ARCHIFY_CHROME_NO_SANDBOX=1`。
- 使うたびに上の URL へ更新の有無を問い合わせる。止めるなら `ARCHIFY_UPDATE_CHECK_DISABLED=1`。

### statusline（`agent-hud statusline`、中身は `src/hud/`）

[rich](https://github.com/Textualize/rich) で 24bit カラーの行を組み立てて標準出力に出す。

```
Opus 5.5 · high   ctx ━━━━━───── 47%   $3.21
dotfiles   feature/x +2 ~1 ?3
5h ━━━━━━━━━━━━━━━━━━━━━━━━┃━━━━╾─────────── 72% ▲12 1h59m
7d ━━━━━━━━━━━━╾───────────────────┃─────── 31% ▼4 5d14h
```

- 1 行目は使っている量（モデル・コンテキスト・費用）、2 行目はリポジトリとブランチ、
  3・4 行目は利用枠。PR 番号は Claude Code 自身が出すので出さない。
- **利用枠の棒は 40 マスで、0.5 マスまで刻む**（境目のマスは左半分だけ太い `╾`）。
  棒は罫線で描く。ブロック要素（`█` `▌`）はもっと細かく刻めるが、マスの高さいっぱいを
  塗るので、5h と 7d の棒が上下でくっついて見える。
  `┃` は窓の時間が過ぎた位置で、棒がこれを越えていれば使いすぎている。
- **端末の幅を変えても描き直されない**（Claude Code の描き直すきっかけに入っていない）。
  次に描き直すまで前の幅の出力が残るので、右寄せや行末の空白で幅を埋めず、大事なものを
  左から並べる。`settings.json` の `refreshInterval: 2` で 2 秒ごとに描き直させ、幅の変化に
  追いつかせる。
- **`▲12` は、利用枠の窓の時間が過ぎた割合より 12 ポイント多く使っているという意味。**
  このままでは窓の途中で尽きる。`▼` は余裕がある側。後ろはリセットまでの残り時間。
- **Ink のような常駐する描画はできない。** Claude Code はコマンドを起動し直して標準出力を
  受け取るだけで、端末につながない。描き方は「1 回出して終わる」ものに限られる。
- **端末の幅は `COLUMNS` から読む。** 収まらない行は優先度の低い部品（費用・worktree・
  コンテキスト・ブランチ）から落とす。
- **Nerd Font を前提にしない。** 既定のフォントにもある文字（`│` `━` `╾` `─` `┃`）で描く。
- **`rich` は `pyproject.toml` の dependencies に宣言し、`./install.sh` が入れる。** 依存を足したら再インストールする。

#### 中身の置き場（`src/hud/`）

入口は `hud/cli.py` の `agent-hud`（サブコマンド `statusline`）。

```mermaid
flowchart LR
    entry["hud.cli<br/>statusline"] --> app
    app --> ports
    app --> render
    app --> core
    render --> core
```

| 層 | 受け持つこと | 使ってはいけないもの |
| --- | --- | --- |
| `core` | 決めること（セッションの読み取り・git の状態・利用枠のペース）。dict と文字列を受けてデータを返す | ファイル・`subprocess`・`os`・rich |
| `ports` | git・利用状況を読む。読んだものを解釈しない | rich・hud のほかの層 |
| `render` | `core` のデータを rich の `Text` にする | ファイル・`subprocess`・`os` |
| `app` | `ports` で読み、`core` で決め、`render` で描く | — |

この向きは `tests/hud/test_hud_layers.py` が import を読んで守らせる。`core` が 1 行
`subprocess` を import すると、利用枠のペースを git とファイル無しでは試せなくなり、しかも
ほかの検査は全部通るので誰も気づけない。

### 設定ファイルのマージ（`~/.claude/settings.json`）

`~/.claude/settings.json` は symlink にしない。Claude Code 自身がここへ書き込むためで
（`/plugin` が `extraKnownMarketplaces` と `enabledPlugins` を足し、`/config` が
`effortLevel` などを書く）、symlink にするとその書き込みがこのリポジトリへ漏れる。

`agent-toolkit install` は `config/claude/hud.json`・個人プリセットを素材として実体へマージする
（辞書は再帰で合流し、配列は config 側で置き換える）。リポジトリが持つキーはリポジトリ側で上書きし、実体にしか
無いキーはそのまま残る。だからそのマシンだけで使う設定は `~/.claude/settings.json` に
直接書けばよく、次のインストールでも消えない。マージで中身が変わるときは、変わる前の実体を
`~/.dotbackup/settings.json.<日時>` に取る。

### タスクリストのツール（`CLAUDE_CODE_ENABLE_TODO_TOOLS`）

`config/presets/personal/claude/settings.json` の `env` にこのキーを入れてある。**入れないと、新しいモデルでは
`TaskCreate` / `TaskUpdate` / `TaskGet` / `TaskList` の 4 ツールが Claude に渡らず、作業中の
タスクリストに何も載らない**（画面のパネルにも出ない）。

これは公式に文書化された opt-in である。[Tools reference の「Task tool
availability」](https://code.claude.com/docs/en/tools-reference#task-tool-availability)（Claude
Code v2.1.233 以降）が、次の 2 点を述べている。

- 対象は **Opus 4.8 / Sonnet 5 / Fable 5 / Mythos 5 と、それぞれの系列のそれ以降**。
  このリポジトリの `model` は `opus[1m]`（= `claude-opus-5`）なので当たる。
- 既定で外している理由は「これらのモデルは書かれたチェックリスト無しでも複数手順の作業を追え、
  ツールの定義とリマインダーがコンテキストを食う」から。**廃止ではない**（廃止されたのは
  `TodoWrite` の方で、`TaskCreate` などの 4 ツールに置き換わった）。

opt-in の方法は 4 つ挙げられている。ここでは 1 つ目を使っている。

| 方法 | 効く範囲 |
| --- | --- |
| `env` に `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`（ここで採用） | 全セッション・全モデル・全プロバイダ |
| `claude --allowedTools TaskCreate` | その起動だけ |
| `claude --tools …`（並べたものだけに絞る） | その起動だけ |
| Agent SDK の `allowedTools` / `tools` / `env` | その呼び出しだけ |

受け付ける値は `1` / `true` / `yes` / `on`（大文字小文字とも）。プロジェクトの設定
（実測したときは分割前の `config/claude/settings.json`。今の `config/presets/personal/claude/settings.json` の `env` に当たる）に足したときは、**走っているセッションでもその場で 4 ツールが増えた**
（Claude Code は設定ファイルの変更を監視している。2.1.234 で実測）。増えなければ再起動する。
同じモデルでキーの有無だけを変えた実測:

| 実行 | `TaskCreate` があるか |
| --- | --- |
| `claude -p "…"` | ない |
| `CLAUDE_CODE_ENABLE_TODO_TOOLS=1 claude -p "…"` | ある |

**このリポジトリで opt-in する理由は、進捗を人が見るためである。** モデルの側は無くても困らない
（上の公式の記述）。**払っているのは 4 ツールの定義とリマインダーのぶんのコンテキストである。**

サブエージェントには、**セッションがツールを持っているときだけ**同じものが渡る（モデルが違っても
同じ。上の公式ページ）。

### Opus の 1M コンテキスト（`"model": "opus[1m]"`）

`config/presets/personal/claude/settings.json` の `"model": "opus[1m]"` は、Opus のコンテキストウィンドウ（1 回の
やり取りでモデルが読める最大トークン数）を 100 万トークンにする指定。末尾の `[1m]` が
Claude Code に long context のベータ機能（`context-1m-2025-08-07`）を要求させる印で、
これが無いと 20 万トークンで打ち切られる。

`ANTHROPIC_DEFAULT_OPUS_MODEL` ではなく `model` キーに書く。Claude Code 2.1.226 で
実測した結果:

| 指定 | 実際のウィンドウ |
| --- | --- |
| `config/presets/personal/claude/settings.json` の `"model": "opus[1m]"` | 1,000,000 |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` に `[1m]` を付けるだけ | 200,000（`[1m]` が落ちる） |

環境変数に `[1m]` を付けても、`claude --model opus` のように別名を明示しない限り
Claude Code が `[1m]` を落とし、20 万トークンのままになる。

トークン単価は `[1m]` の有無で変わらない（long context の割増は無い）。ただしウィンドウが
5 倍になると自動 compact（会話履歴の自動要約）が起きるまでが長くなり、1 リクエストあたりの
入力トークンが増えるので支払総額は増える。手前で compact させたいときは
`CLAUDE_CODE_AUTO_COMPACT_WINDOW`（または `autoCompactWindow` 設定）でトークン数を指定する。

Sonnet 5 は既定で 100 万トークンなので、`[1m]` を付ける必要はない。

### 言語サーバ（LSP）

Claude Code は言語サーバから定義・参照・シンボル検索・型エラーを引く。使えるようにするには
2 つ揃える必要があり、置き場所が分かれている。

| 何を決めるか | どこに書くか |
| --- | --- |
| どの拡張子をどのサーバに渡すか | `config/presets/personal/claude/settings.json` の `enabledPlugins`（公式 plugin の `typescript-lsp` / `pyright-lsp` / `php-lsp`） |
| サーバの実行ファイル | dotfiles の mise 設定（`dist/dot-config/mise/config.toml`）（`npm:typescript-language-server` / `npm:pyright` / `npm:intelephense`） |

公式 plugin が持つのは起動コマンドと拡張子の対応だけで、実行ファイルは PATH から探す。
plugin を有効にしても実行ファイルが無ければ、その言語では何も引けない。

**TypeScript は、開いているリポジトリの `node_modules/typescript` が要る。**
`typescript-language-server` は解析を tsserver に任せ、それをワークスペース直下から探す。
dotfiles の mise 設定（`dist/dot-config/mise/config.toml`） に `npm:typescript` を足しても効かない。mise は npm パッケージごとに別の
node_modules を作るので、サーバからは見えないため。`npm ci` を通していないリポジトリでは
initialize が `Could not find a valid TypeScript installation` で失敗する。

### Dev Containers

VS Code のユーザ設定に以下を追加すると、コンテナ作成時に自動適用される:

```json
"dotfiles.repository": "<owner>/dotfiles",
"dotfiles.targetPath": "~/dotfiles",
"dotfiles.installCommand": "install.sh"
```
