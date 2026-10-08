"""この turn に Claude が編集ツールで書いたものを、応答を終える前に見直させる hook の中身。

層は 5 つで、import の向きは一方通行である（`tests/turnreview/test_turnreview_layers.py`
が守らせる）。

    app → ports, syntax, render, core
    syntax → core
    render → core

- `core`: 決めること。dict・文字列・`Path` を受けてデータを返す。I/O も tree-sitter も rich も使わない
- `syntax`: ソースの文字列から、コメントとテストの位置と本文を取り出す。tree-sitter と `ast` はここだけ
- `render`: `core` の `Review` を rich で色と罫線の付いた文字列にする。rich はここだけ
- `ports`: 外とのやり取り（stdin・stdout・transcript・ファイル・一時ディレクトリ）。読んだものを解釈しない
- `app`: `ports` で読み、`syntax` で取り出し、`core` で決め、`render` で描いて返す
"""
