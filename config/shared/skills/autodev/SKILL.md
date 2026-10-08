---
name: autodev
description: >-
  1 つの指示を、計画・実装・テスト・レビューを経て stacked PR まで無人で進める autodev の driver を
  起動し、driver が出す質問をユーザーに渡して回答を届け、終わったら結果を報告する。ステージの進行と
  合否は driver の中の統括とドメインが決めるので、このスキルはステージを務めず、質問にも自分で答えない。
when_to_use: >-
  ユーザーが `/autodev` と打ったとき、または「この作業を実装から PR まで自律で進めてほしい」
  「人が見ていなくても進むようにしてほしい」と頼んだとき、走っている autodev のランの質問に
  答えたい・状態を知りたい・ランを片付けたいと言ったときに使う。受入条件が書けるだけの大きさがある
  作業に使う。1 ファイルの小さな修正には重すぎる。
---

# autodev

あなたは driver の外にいて、役目は 3 つだけである: driver を起動する・質問をユーザーに渡す・回答を driver に届ける。

入口は PATH の `autodev` コマンド。
結果の JSON は標準出力に、知らせと落ちた理由は標準エラーに出る。

## 1. 起動する

次を確定する。足りないものはユーザーに聞き、推測で埋めない。

| 引数 | 決め方 |
| --- | --- |
| `--name` | ラン名。英小文字・数字・`-` で 1〜49 字。`-` で始めず、`--` と末尾の `-` を含めない。指示の主題から付ける |
| `--repo` | 対象リポジトリ。新しいランでは必須 |
| 指示 | 受入条件が一意に定まる文 |
| `--base` | ユーザーが指定したときだけ付ける。省くと origin の既定ブランチ |

指示と回答は、引用符で囲んだヒアドキュメント（`<<'EOF'`）で `-`（標準入力）に渡す。`"..."` で囲むと、
文の中の `"`・`$`・バッククォートで壊れ、`$(...)` は実行される。

```bash
autodev run --name <ラン名> --repo <リポジトリ> --instruction-file - <<'EOF'
<指示>
EOF
```

**Bash の `run_in_background` で走らせる**（1 タスクで 30 分以上かかる）。終わると終了コードが届く。

## 2. 待っている間

ランディレクトリは `autodev status --json --name <ラン名>` の `run.directory` で取る。起動の直後は、events.db がまだ無いので 5 が返ることがある。`run` がまだ終えていなければ 5 秒おいて取り直す（5 回まで。それでも 5 か、1 が返ったら標準エラーの理由をユーザーに伝える）。
その `questions/` を Monitor で見る（`timeout_ms` は上限にし、切れたら張り直す）。Monitor が出した
パス（渡し済みの質問）は会話の中で覚えておき、張り直すときは改行で区切って `seen` の初めの値にする。

```bash
dir=<ランディレクトリ>/questions; seen="<渡し済みの質問のパス>"
while true; do
  cur=$(grep -rlE --include='*.json' '"status"[[:space:]]*:[[:space:]]*"open"' "$dir" 2>/dev/null | sort)
  [ -n "$cur" ] && comm -13 <(echo "$seen" | sort) <(echo "$cur"); seen=$cur; sleep 2
done
```

質問のファイル `questions/<質問 ID>.json` の欄は `question`（ID）・`body`・`status`（`open` / `answered` / `withdrawn`）・
`answer`・`reason`（取り下げた理由）。新しく `open` になったら、すぐ `body` をユーザーに渡す。
**質問にはあなたが答えず、ユーザーに聞く。** 回答は次で置く。

```bash
autodev answer --name <ラン名> --question <質問 ID> --answer-file - <<'EOF'
<回答>
EOF
```

driver が走っていれば、置いた回答をそのまま受け取る。回答を置いた後に `run` を呼び直すのは、`run` が 4 で終わったときだけ。

取り下げた質問に答えると、`answer` は 1 で落ち、標準エラーに取り下げた理由が出る。ユーザーに
「その質問は取り下げられた」と理由を添えて伝え、次の質問を待つ。答え済み・無い質問・空の回答も 1 で落ちる。

状態を知りたいときは `autodev status --json --name <ラン名>` を読む（`format` は 2。形は autodev パッケージの `infra/status/status_sections.py`）。driver が止まっているかは `run.driver_stopped`、タスクの実行は `tasks[].flow.steps[].executions`（書き直す前のフローでまだ走っている実行は `tasks[].earlier_executions`）、終わった理由は実行の `end_reason`、指摘は `tasks[].findings` と `plan.findings` にある。

## 3. 終わったとき

| コード | すること |
| --- | --- |
| 0 | `status --json` の `tasks[]` で、`kind` が `implementation` のものを `status` で分けて報告する: `stacked`（積んだ）・`dropped`（止めた）・`discarded`（破棄した）・`superseded`（引き継がれた。引き継いだ先は `superseded_by`）。積んだ数と積む数は `run.stacked_tasks` と `run.stack_target_tasks`。概要 PR は `stack.overview.pr`。0 は「全部積んだ」ではない。マージしない |
| 4 | 回答待ちで、進められるタスクが無い。`status --json` の `questions[]` をユーザーに渡し、答えを `answer` で置いてから `run --name <ラン名>` を呼び直す |
| 3 | パニック（利用枠の上限など。SIGTERM・SIGINT で止めたときも 3）。`status --json` の `run.panic_cause` を読む。null か足りなければ、`run` の標準エラーとランディレクトリの `logs/` を見る。原因をユーザーに伝え、原因が消えたら `run --name <ラン名>` を呼び直す |
| 1 | 起動できなかった。標準エラーの理由をユーザーに伝える。直せるもの（認証・gh stack・リポジトリの設定・ラン名）はユーザーと直してから呼び直す |

呼び直すときは `--instruction` を付けない（1 で止まる）。`--repo`・`--base` は省くか、最初と同じ値にする。
同じランの driver が走っている・前の driver が起こしたプロセスが生きている、で 1 になったら、終わるのを待つかユーザーに聞く。

## 4. 片付ける

どちらも消したものは戻せない。**使う前に、何が消えるかをユーザーに示して確かめる。**

| コマンド | 使う場面 | 消すもの |
| --- | --- | --- |
| `autodev clean --name <ラン名>` | 終了コード 0 で終えたランの worktree を外す | worktree だけ。ランディレクトリの記録は残す |
| `autodev purge --name <ラン名>` | やめたラン・もう見ないランを消す | 手元の worktree・ブランチ・ランディレクトリ。PR とリモートのブランチは残る |

止まったら、標準エラーに出た理由をユーザーに示す。`--force` はその理由を越えて消すので、ユーザーが承知したときだけ付ける。
`--force` でも消さないのは、driver か、前の driver が起こしたプロセスが走っているとき。
`clean` も `purge` も、worktree の未コミットの変更や、切り離した HEAD にしか無いコミットがあると止まる。
squash マージや rebase マージを済ませたランの `purge` は `--force` が要る。PR がマージ済みなら `--force` で消してよい。

## 5. エージェント・モデル・effort を変える

ユーザーにエージェントやモデルや effort の設定を頼まれたら、`~/.config/autodev/models.json` を手で書かずに次で設定する。
クラスは `lead`（統括・計画・ジャッジ）・`review`（レビュー）・`implement`（テスト・実装・修正）・
`write`（PR 本文）。今の値は `autodev config show` で見る。

```bash
autodev config set --class <クラス> [--agent claude|codex|copilot] [--model <モデル>] [--effort <effort>]
```

- `--agent`: クラスごとにステージを務めるエージェント。既定は `claude`
- effort: claude は `low`・`medium`・`high`・`xhigh`・`max`。codex は `minimal`・`low`・`medium`・`high`・`xhigh`。copilot は `none`・`minimal`・`low`・`medium`・`high`・`xhigh`・`max`
- codex・copilot を選ぶクラスには `--model`（そのエージェントのモデル名）が要る。claude に戻すときも claude のモデル名を `--model` で渡す。copilot に `auto` は使えない
- copilot を選ぶには、`copilot login` 済みであること。個人の MCP・プラグインは引き継がれない。利用枠の上限（`rate_limit`・`quota`）に当たると、
  別モデルへ切り替えずにランが止まる（終了コード 3）。認証は OS の資格情報ストアを使う環境では未検証で、ランの始めの確かめは
  `config.json` の認証の項目を見るだけ（トークンの失効は最初のステージで分かる）
- codex を選ぶには、`codex login` 済みであること。`agent-toolkit install` の後に、Codex の `/hooks` で autodev のフックを
  信頼する（初回だけ。信頼しないと、ステージが「フックの記録が 0 件」のエラーで落ちる）

設定は次に始めるランから効く。走っているランと呼び直したランは、始めたときの値を使い続けるので、そうユーザーに伝える。

## 6. 検証コマンドの種類

ランが流す検証コマンドは 3 種類で、流す時点が違う。リポジトリの設定（`~/.config/autodev/repos/<スラッグ>.json`）には
`quickChecks` と `regressionTests` を書く。`taskTests` は計画がタスクごとに選ぶ。古い鍵 `verify` は読まれず、起動が失敗する（終了コード 1）。

| 種類 | 鍵 | 中身 | 流す時点 |
| --- | --- | --- | --- |
| タスクのテスト | `taskTests` | そのタスクのテスト | `ConfirmRed`・`Gate` |
| 軽い検査 | `quickChecks` | lint・型検査のように速く、どのタスクを積んだ時点でも通るもの | `Gate`・`IntegrationCheck` |
| 回帰テスト | `regressionTests` | テスト全体のように遅く、ほかの場所を壊していないかを確かめるもの | `IntegrationCheck` |

統合検査（`IntegrationCheck`）は、積む前にスタックの一番上で軽い検査と回帰テストを流す。

## 7. しないこと

- ステージ（計画・テスト作成・実装・レビュー・ジャッジなど）を自分で務める
- 合否を判断する・積まれていないタスクの PR を自分で作る
- ランディレクトリ（`events.db`・`questions/` など）を書き換える。回答は `answer` でだけ届ける
- 統括の代わりに質問に答える
- `gh pr merge` / `gh stack merge` を呼ぶ
- `ask` を呼ぶ（計画ステージが使う）
