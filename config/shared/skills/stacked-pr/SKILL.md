---
name: stacked-pr
description: GitHub の stacked pull request を gh stack 拡張で作る・積み替える・同期する・マージする・レビュー順を説明する。1 つの作業を依存し合う複数の PR に分けて積み上げる機能で、新しいため記憶や推測でコマンドを書くと間違う。必ずこのスキルを読んでから手を動かす。「stacked PR」「スタックした PR」「PR を積む」「PR を分割して連鎖させる」「gh stack」「積み替え」「restack」「下から順にマージ」が出てきたとき、および積んだ PR のレビュー順・マージ順・コンフリクトの直し方を尋ねられたときに使う。
allowed-tools:
  - Bash(gh stack view *)
  - Bash(gh stack --help)
  - Bash(gh stack help *)
  - Bash(gh extension list)
---

# stacked PR（gh stack）

**stacked PR** は、1 つの大きな変更を**依存し合う複数の PR の連なり**に分けたものである。下から
順に積み、下から順にレビューしてマージする。組み立てるのは GitHub 公式の CLI 拡張 `gh stack` で、
GitHub 側もスタックを PR の merge box に描き、まとめてマージする API を持つ。

```
main（trunk）
 ← branch-1（PR #101）      ← 最初にマージする
   ← branch-2（PR #102）    ← #101 が入ってから
     ← branch-3（PR #103）
```

**通常の PR との違いは base が trunk ではないこと。** `branch-2` の PR は `main` ではなく
`branch-1` に向く。だから **`branch-1` がマージされるまで `branch-2` はマージできない**し、
`branch-2` の差分表示には `branch-1` の変更が含まれない（レビュワーはその PR の分だけを読める）。

## 取り掛かる前に公式ドキュメントを読む

**記憶で書かない。** 該当するページを `WebFetch` で読んでから手を動かす。

| やること | 読むページ |
|---|---|
| 概念を掴む | https://docs.github.com/ja/pull-requests/get-started/about-stacked-prs |
| とりあえず 1 本組む | https://docs.github.com/ja/pull-requests/get-started/stacked-prs-quickstart |
| 作る | https://docs.github.com/ja/pull-requests/how-tos/create-pull-requests/creating-stacked-pull-requests |
| 積み替える・並べ替える・同期する | https://docs.github.com/ja/pull-requests/how-tos/create-pull-requests/managing-stacked-pull-requests |
| マージする | https://docs.github.com/ja/pull-requests/how-tos/merge-and-close-pull-requests/merging-stacked-pull-requests |
| CI の走り方を絞る | https://docs.github.com/ja/pull-requests/how-tos/merge-and-close-pull-requests/optimizing-ci-for-stacked-pull-requests |
| 詰まった | https://docs.github.com/ja/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-stacked-pull-requests |

## この環境に入っている gh stack

```!
gh stack --help 2>&1 | head -60
```

上が空か `unknown command` なら拡張が入っていない。`gh extension install github/gh-stack` で
入れる。**下に書いたフラグも、使う前に `gh stack <サブコマンド> --help` で確かめる**（バージョンが
上がると変わりうる）。

## よく使う流れ

```bash
gh stack init <ブランチ名>...   # スタックを作る。既存ブランチは採用され、無いものは作られる
                               # 1 本目は既定ブランチの上。別の trunk にするなら --base
gh stack add <ブランチ名>       # いまのスタックの一番上に 1 段足す
gh stack submit                # 全ブランチを push し、PR を作成・更新してスタックを GitHub に作る
gh stack sync                  # remote と合わせる（fetch → trunk を進める → 連鎖 rebase → push）
gh stack view                  # いまの並びと各 PR の状態を見る
gh stack merge                 # 下から順に、指定した PR までを一括でマージする
```

- **`submit` は対話エディタを開く。** PR のタイトル・本文・draft かどうかをその場で決める。
  非対話（スクリプトや CI）では `--auto` になり、**自動生成のタイトルで draft として作られる**
  （draft にしたくなければ `--open`）。
- **`sync` は push を `--force-with-lease --atomic` で行う。** 積み替えると履歴が書き換わるので
  force push が避けられない。他人がそのブランチに commit している場合は失敗する。
- **`rebase` は連鎖 rebase。** `--no-trunk` を付けると trunk を fetch せず、スタック内の
  段どうしの rebase だけを行う。`--downstack` / `--upstack` で範囲を絞れる。
- **`merge` は all-or-nothing。** 選んだ PR とその下の全部が 1 回の操作で base に入り、
  1 本でもマージできなければ 1 本も入らない。マージ要件（branch protection・repository rules）の
  バイパスはスタックでは使えない。base が merge queue を使っているならキューに入る。
- **`checkout <番号>`** の番号は、まずスタック番号（GitHub のスタック UI に出る識別子）として
  解釈され、次に PR 番号として扱われる。PR の URL やブランチ名でも指定できる。

## 気をつけること

- **レビュー順とマージ順は下から。** 上の PR だけ先にマージすることはできない。レビュワーに
  伝えるときは「どの PR から読むか」を明示する。
- **土台が draft のあいだは上の PR もマージできない。** 途中で誤ってマージされるのを防ぐ用途に
  使える一方、マージを始めるには土台を `gh pr ready` に上げる必要がある。
- **1 段目を直したら上の全段を積み替える。** `gh stack view` が `⚠ Needs rebase` を出す。
  `gh stack sync`（または `rebase`）を流してから push する。
- **コンフリクトは rebase が止まった場所で直す。** 直したら `--continue`、やめるなら `--abort`。
  正確な指定は `gh stack rebase --help` とトラブルシューティングのページで確かめる。
- **並べ替えと 1 本だけ外す操作（`gh stack modify`）は対話 TUI である。** 自動化できないので、
  その判断が要るときは人に渡す。
- **スタックの追跡情報はローカルの git ディレクトリに置かれる。** 別の worktree や別のマシンから
  同じスタックを操作したいときは `gh stack checkout` で引き直す。**1 つのスタックを触る場所は
  1 か所に決める**——別の場所で `sync` を通すと元の場所の記録が取り残され、`gh stack view` が
  古い SHA を返しながら `Needs rebase` も出さない。
- **`sync` を通す前に、その trunk を checkout している worktree がいないかを見る。** trunk の
  ローカル ref を fast-forward するので、握っている worktree の HEAD だけが動くことがある。

