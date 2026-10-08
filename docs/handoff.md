# agent toolkit の引継ぎ

dotfiles から AI 関連の履歴とファイルを抽出した private リポジトリ
[Luna-rab/agent-toolkit](https://github.com/Luna-rab/agent-toolkit)。
通常の環境から clone／pull して使う。構成とインストール手順は [README.md](../README.md)。

## 配布の仕組み

- コードは `src/`、ホームへ配る設定と指示は `config/`。`install.sh` が `uv tool install --editable` で
  `autodev`・`agent-toolkit`・`agent-hud` を入れ、`agent-toolkit install` が `config/` をホームへ配る
- 部品・プリセット・配置・依存の表は `src/toolkit/catalog.py`、実行は `src/toolkit/install.py`、マージの規則は `src/toolkit/merge.py`
- `agent-toolkit list` が部品と依存を出し、`agent-toolkit doctor` が依存の CLI と認証を確かめる。入口と終了コードは [README.md](../README.md)
- dotfiles へ移す候補は [dotfiles-migration.md](dotfiles-migration.md)
- `agent-toolkit install --offline` は gh-stack と archify の取得を飛ばす
- 共通 Skills は `~/.agents/skills/` と `~/.claude/skills/` にスキル 1 つずつ symlink する

## 動作の範囲

- autodev の実行アダプタは Claude Code・Codex CLI・Copilot CLI。クラスごとに選び、既定は Claude Code。Codex から入口の CLI を操作しても、選ばない限り driver の実行先は Claude Code
- Copilot を選ぶには `copilot login` が要る。認証は OS の資格情報ストアを使う環境では未検証で、フックの時間切れでは操作が通りうる。利用枠の上限のイベントの形は同梱の型定義から取ったもので、実物では未確認
- フックは Claude Code と Codex の両方で動く（Stop の turnreview）
- Codex のフックは、初回に `/hooks` で信頼済みとして登録する

## 旧配置からの切り替え

元の dotfiles の `dist/dot-claude/` を指していた `~/.claude/{rules,hooks,scripts}` の symlink は、
`./install.sh` が消す。`~/.local/bin/autodev` の旧 symlink も `install.sh` が消す。
置き換えた実体は `~/.dotbackup/` に残る。

資格情報、history、projects、autodev のラン記録をリポジトリへ取り込まない。

## その後の改善候補

- autodev に他エージェント用の実行・ガードアダプタを追加する
- 新しいホーム環境で `./install.sh` を通しで検証する
