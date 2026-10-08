---
name: editing-rules
description: .claude/rules/ 配下のルールや CLAUDE.md を作る・直すときに従う手順。公式仕様（置き場所とスコープ、paths frontmatter で特定ファイルに絞る書き方、指示の書き方、サイズの目安、読み込まれる順序）を毎回 code.claude.com から取り直して本文に差し込むので、記憶や推測で書いて古い仕様のまま書くことがなくなる。CLAUDE.md にルールを足すとき、.claude/rules/ にファイルを作るとき、常時ロードを path-scoped に変えるとき、CLAUDE.md が大きくなって分割するときに必ず読む。
paths:
  - ".claude/rules/**"
  - "**/.claude/rules/**"
  # このリポジトリは全プロジェクト向けの指示を config/shared/AGENTS.md に置く。~/.claude/CLAUDE.md と ~/.codex/AGENTS.md へ配られる実体
  - "config/shared/AGENTS.md"
  - ".claude/CLAUDE.md"
  - "**/CLAUDE.md"
  - "**/AGENTS.md"
allowed-tools:
  - Bash(curl -sS https://code.claude.com/docs/*)
---

# rules / CLAUDE.md を編集するときの手順

**記憶や推測で書かない。仕様は変わる。** 下に差し込まれた公式ドキュメント（`https://code.claude.com/docs/ja/memory`）の該当節を読んでから手を動かす。

## 1. 公式仕様（毎回ここで取り直している）

```!
curl -sS --max-time 20 https://code.claude.com/docs/ja/memory.md 2>&1 | awk '/<h3 id="write-effective-instructions">/{f=1} /<h3 id="manage-claude-md-for-large-teams">/{f=0} f'
```

上が空、または見出しだけで中身が無い場合は、ページの構成が変わっている。`WebFetch` で
`https://code.claude.com/docs/ja/memory` を取り、CLAUDE.md の書き方・読み込み順・
`.claude/rules/` の節を読む。

## 2. 書き終えたら検査する

| 検査 | 落ちていたら |
|---|---|
| **常時ロードが必要か** | 常に効かせたい振る舞い規則以外は `paths` frontmatter で対象ファイルに絞る。絞れば一致するファイルを触るときだけ読み込まれ、毎ターン再送されるノイズが減る |
| **置き場所とスコープが合っているか** | プロジェクト共通なら `.claude/rules/`、全プロジェクトなら `~/.claude/rules/`。1 で取った表と突き合わせる |
| **CLAUDE.md が 200 行以下か** | 手順に育った節はスキル（`.claude/skills/`）へ移す。スキルの本体は呼ばれたときだけ読み込まれるので、長い資料は必要になるまでコストがかからない |
| **指示が検証できる形か** | 「適切に」「しっかり」ではなく、何がどうなっていれば守れているのかを書く |
| **見出しと箇条書きで区切ってあるか** | 密な段落は読み飛ばされる |

## 3. このリポジトリでの決めごと

- 日本語で書く。文章の作法は [../../../shared/AGENTS.md](../../../shared/AGENTS.md) の「日本語を書くときのルール」に従う。
- **ルールに置くのは「常に守る規則」だけ。** 手順や作業のやり方はスキルにする。
- 全プロジェクトに効かせる指示は `config/shared/AGENTS.md` に見出しを足して書く。`agent-toolkit install` が
  `~/.claude/CLAUDE.md` と `~/.codex/AGENTS.md` へ symlink する。Claude Code と Codex の両方に読まれるので、
  どちらかのツール名を書くときは両方の名前を並べる。
- このリポジトリでだけ効かせたいルールは `.claude/rules/` に置く。ここは配らない。
