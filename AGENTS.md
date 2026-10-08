# このリポジトリでの開発ガイド

Claude Code と Codex 向けの設定・Skills・フック・autodev・HUD を管理する。構成とインストール手順は
[README.md](README.md)。

## 置き場所の決まり

- コードは `src/`、ホームへ配る設定と指示は `config/`。`config/` に Python を置かず、`src/` に SKILL.md・settings.json・hooks.json・AGENTS.md を置かない
- テストは `tests/` に `src/` と同じ形で置く（`tests/{autodev,turnreview,hud,toolkit,config}/`）。`tests/conftest.py` で `sys.path` を足さない（パッケージはインストール済み）
- 依存は `pyproject.toml` に宣言する。PEP 723 の `# /// script` は使わない
- フックと statusline は、ファイルパスではなくコマンド（`agent-toolkit hook <イベント> --agent <claude|codex>`、`agent-hud statusline`）で登録する。Codex はフック定義のハッシュで信頼を記録するので、実装を変えても定義が変わらない形にする
- スキルにコードを置かない。スキルは PATH のコマンド（`autodev` など）を呼ぶ
- 配置の表は `src/toolkit/catalog.py`、実行は `src/toolkit/install.py`。配置先を足したら両方とテスト（`tests/toolkit/test_toolkit_install.py`）を直す
- 個人の設定（モデル・権限・プラグインなど）は `config/presets/<名前>/` に置く。機能の設定（`config/claude/hooks.json`・`hud.json`）に混ぜない
- 資格情報・history・autodev のラン記録をコミットしない

## 検査

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

`config/shared/skills/` と `config/claude/skills/` の SKILL.md は `tests/config/` が検査する。

## 動作確認

`agent-toolkit install` は実際のホームを書き換える。試すときは `HOME` を一時ディレクトリにして `--offline` を付ける:

```sh
HOME=$(mktemp -d) uv run agent-toolkit install --offline
```

依存を足したら `./install.sh` で再インストールする（`uv tool install --editable` のため）。

## 開発用 subagent

`.claude/agents/medium-{worker,reviewer}.md` と `.codex/agents/medium-{worker,reviewer}.toml` は同じ指示を持つ。片方を直したらもう片方も直す。
