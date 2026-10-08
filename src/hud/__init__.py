"""`agent-hud`（statusline と watch）の中身。

層は 4 つで、import の向きは一方通行である（`tests/hud/test_hud_layers.py` が守らせる）。

    app → ports, render, core
    render → core

- `core`: 決めること。dict と文字列を受けてデータを返す。I/O も rich も使わない
- `ports`: 外とのやり取り（`autodev status --json`・git・利用状況）。読んだものを解釈しない
- `render`: `core` のデータを rich の `Text` にする
- `app`: `ports` で読み、`core` で決め、`render` で描く
"""
