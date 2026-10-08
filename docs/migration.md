# dotfiles からの抽出

元リポジトリ: https://github.com/Luna-rab/dotfiles

別 clone に git-filter-repo を実行し、次のパスの履歴を保持した。

- `dist/dot-claude/`
- `dist/dot-vscode-server/`（autodev watch の設定）
- `.claude/agents/`、`.claude/scripts/`
- `test/`、`pyproject.toml`、`uv.lock`
- `README.md`、`install.sh`、`.gitignore`（混在部分は抽出後に整理）

元の dotfiles の履歴は書き換えていない。
未追跡の `.claude/settings.json` や、ホームの資格情報・実行データは取り込んでいない。
