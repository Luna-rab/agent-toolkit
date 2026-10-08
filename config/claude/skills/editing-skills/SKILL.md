---
name: editing-skills
description: .claude/skills/ 配下の SKILL.md と補助ファイルを作る・直すときに従う手順。公式仕様（frontmatter のフィールド、description と when_to_use の書き方、本体の行数、補助ファイルへの分け方）を毎回 code.claude.com から取り直して本文に差し込むので、記憶や推測で書いて古い仕様のまま書くことがなくなる。スキルや slash command を新規作成するとき、既存の SKILL.md を編集するとき、description や frontmatter を直すとき、スキルの構成を分割するときに必ず読む。
paths:
  - ".claude/skills/**"
  - "**/.claude/skills/**"
  - ".claude/commands/**"
  - "**/.claude/commands/**"
  # このリポジトリはスキルを config/ に置く。ここが ~/.claude/skills と ~/.agents/skills へ配られる実体
  - "config/shared/skills/**"
  - "config/claude/skills/**"
allowed-tools:
  - Bash(curl -sS https://code.claude.com/docs/*)
---

# skills を編集するときの手順

**記憶や推測で書かない。仕様は変わる。** 下に差し込まれた公式ドキュメント（`https://code.claude.com/docs/ja/skills`）の該当節を読んでから手を動かす。

## 1. 公式仕様（毎回ここで取り直している）

```!
curl -sS --max-time 20 https://code.claude.com/docs/ja/skills.md 2>&1 | awk '/<h3 id="types-of-skill-content">/{f=1} /<h3 id="control-who-invokes-a-skill">/{f=0} f'
```

上が空、または見出しだけで中身が無い場合は、ページの構成が変わっている。`WebFetch` で
`https://code.claude.com/docs/ja/skills` を取り、frontmatter リファレンスの節を読む。

## 2. 書き終えたら検査する

| 検査 | 落ちていたら |
|---|---|
| **本体が簡潔か** | 実行内容（何をするか）を述べ、方法や理由の説明は削る。読み込まれた `SKILL.md` はターン全体でコンテキストに留まり、毎ターン再送されてトークンになる |
| **`SKILL.md` が 500 行以下か** | 詳細なリファレンス資料を補助ファイルへ移し、`SKILL.md` からリンクして「何が書いてあり、いつ読むか」を示す |
| **`description` が自動起動の判断材料になっているか** | ユーザーが自然に言う語を入れる。主要なユースケースを前に置く（`description` と `when_to_use` の合計は 1,536 文字で切られる） |
| **frontmatter のフィールドが上の仕様どおりか** | 存在しないフィールドを書いていないか、`allowed-tools` / `disable-model-invocation` / `paths` / `context: fork` の意味を取り違えていないかを 1 で取った表と突き合わせる |
| **相対リンクと `scripts/` 参照が実在するか** | agent-toolkit リポジトリで作業しているなら `uv run pytest tests/config` を流す（ほかのリポジトリには無い）。frontmatter の YAML・行数・リンク・実行権限を検査する |

## 3. このリポジトリでの決めごと

- 日本語で書く。文章の作法は [../../../shared/AGENTS.md](../../../shared/AGENTS.md) の「日本語を書くときのルール」に従う。
- **公式ガイドの指定がこのリポジトリのルールより優先する。** `SKILL.md` は公式が「本体は簡潔に。
  実行内容を述べ、方法や理由を説明しない」と定めているので、日本語のルールの「理由（なぜ）を
  書く」より公式に従う。行数が増える直し方は見送る。
- コミットする前に `uv run pytest tests/config` を（リポジトリのルートから）流す。
