#!/bin/bash
# 使い方: ./install.sh --cli-only | ./install.sh [agent-toolkit install の引数...]
# 終了コード: 0 成功 / 2 引数の誤り / 10 uv が見つからない / 11 uv tool の失敗
#             配置に進んだら agent-toolkit install の終了コードをそのまま返す
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# --cli-only だけを見る。ほかの引数は agent-toolkit install へそのまま渡す
cli_only=0
for arg in "$@"; do
  [[ "$arg" == "--cli-only" ]] && cli_only=1
done
if [[ "$cli_only" -eq 1 && "$#" -gt 1 ]]; then
  echo "ERROR: --cli-only はほかの引数と一緒に使えない" >&2
  exit 2
fi

# UV（指定されていれば実行できること）→ PATH → mise の順に探す。
# mise 経由で入れた uv は、この時点ではまだ PATH に無い
uv_bin=""
if [[ -n "${UV:-}" ]]; then
  if [[ -x "$UV" && -f "$UV" ]]; then
    uv_bin="$UV"
  fi
else
  uv_bin=$(command -v uv || true)
  if [[ -z "$uv_bin" && -x "$HOME/.local/bin/mise" ]]; then
    uv_bin=$("$HOME/.local/bin/mise" which uv 2>/dev/null) || uv_bin=""
    if [[ ! (-x "$uv_bin" && -f "$uv_bin") ]]; then
      uv_bin=""
    fi
  fi
fi
if [[ -z "$uv_bin" ]]; then
  echo "ERROR: uv not found" >&2
  exit 10
fi

# 旧配置の autodev は dotfiles 側の autodev.py への symlink だった
old="$HOME/.local/bin/autodev"
if [[ -L "$old" && "$(readlink "$old")" == */dist/dot-claude/skills/autodev/scripts/autodev.py ]]; then
  rm -f "$old"
fi

"$uv_bin" tool install --editable --reinstall "$repo" || exit 11

if [[ "$cli_only" -eq 1 ]]; then
  exit 0
fi

tool_bin=$("$uv_bin" tool dir --bin) || exit 11

# set -e のもとで agent-toolkit の終了コードがそのまま返る
"$tool_bin/agent-toolkit" install "$@"
