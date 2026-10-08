# dotfiles への移行候補

agent-toolkit が今配っているもののうち、本来は dotfiles が持つものと、その移し方。責任分担は [README.md](../README.md) の「責任分担」。

**dotfiles 側に配置の仕組みができるまで、agent-toolkit の既存の配置処理は消さない。** 先に消すと、dotfiles が引き継ぐ前に配置が止まる。

## 移行候補

| 候補 | 今の置き場 | 移し先 | 理由 |
| --- | --- | --- | --- |
| VS Code の端末プロファイル「autodev watch」（`zsh -lic "exec agent-hud watch"`） | `config/vscode/settings.json`（部品 `vscode`） | dotfiles のエディタ設定 | エディタとシェルの設定は dotfiles の担当。`zsh -lic` はログインシェルで PATH を整えてから `agent-hud` を呼ぶ前提で、シェル設定と一緒に管理したほうが崩れにくい |
| モデル・権限・環境変数・プラグインの個人選択 | `config/presets/personal/claude/settings.json`（`--preset personal`） | dotfiles が個人設定として持ち、`~/.claude/settings.json` へマージする | 個人の好みで、agent-toolkit の機能ではない。移し終えたら dotfiles は `--preset none` で呼ぶ |
| gh-stack 拡張 | 部品 `extras`（`gh extension install github/gh-stack`） | dotfiles の依存導入 | gh の拡張はツールの導入で、dotfiles の担当。archify の取得は agent-toolkit に残す。今は gh-stack と archify が同じ部品 `extras` に入っているので、gh-stack だけを移すには手順 3 のとおり agent-toolkit を直す |
| mise・PATH・シェルなど環境構築の説明 | `docs/claude.md` の LSP の節（mise の `npm:` パッケージ）ほか | dotfiles の README | agent-toolkit は PATH にコマンドがあることだけを前提にする |

## 移行手順

1. dotfiles に、候補ごとの配置処理を作る（`~/.vscode-server/data/Machine/settings.json` へのマージ、個人設定の `~/.claude/settings.json` へのマージ。gh-stack は dotfiles の `install.sh` の `install_gh_extensions` がすでに入れているので作らない）
2. dotfiles から `./install.sh --skip vscode --preset none` で呼び、重複して配置されないことを確かめる。`extras` は skip しない（skip すると archify も入らない。gh-stack は dotfiles の `install_gh_extensions` と agent-toolkit の両方が入れるが、agent-toolkit は gh-stack が入っていれば取得を飛ばすので、重複しても害はない）。確かめには `agent-toolkit doctor --skip vscode --preset none --json` と、`HOME` を一時ディレクトリにした `agent-toolkit install` を使う
3. 動くことを確かめたあとで、agent-toolkit から `vscode` 部品とプリセット `personal`、その `config/` の中身を消す。gh-stack の取得は `extras` から外し、archify の取得だけを残す。消すときは `src/toolkit/catalog.py` の表とテスト（`tests/toolkit/`）を直す

3 は dotfiles 側の配置が実機で動いてから行う。
