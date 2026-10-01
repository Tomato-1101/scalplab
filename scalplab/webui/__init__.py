"""バックテストのエントリーポイントをチャートで確認する Web UI。

`python -m scalplab.webui` で stdlib http.server を 127.0.0.1:8025 に起動する。
指標（RCI/SAR/SMA/エントリー）はサーバ側で既存の scalplab.indicators / strategy /
engine を使って計算し、フロント（lightweight-charts）には描画用の座標だけを渡す。
これにより「表示」と「バックテスト」が同一コードを共有し、両者のズレを構造的に防ぐ。
"""
