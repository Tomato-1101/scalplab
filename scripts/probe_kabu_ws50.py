"""kabuステーション WebSocket 上限 50 銘柄同時受信の実機プローブ（読み取りのみ・発注なし）。

なぜ: 将来のリアルタイムシグナル検知は register（50 銘柄上限）+ WebSocket PUSH が
前提。上限いっぱいで受信が回るかを確認する。

実測 2026-08-19（立会中）: register 50/50 成功・30 秒で 3,077 ティック・全 50 銘柄
から受信。終了時の WinError 10038 WARNING は既知の無害な競合（troubleshooting.md）。

実行: .venv\\Scripts\\python scripts\\probe_kabu_ws50.py
"""
import queue
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from scalplab.live import KabuStationTickSource, load_live_config  # noqa: E402

live_cfg = load_live_config()  # live.local.yaml があればマシン固有設定で上書きされる
pm_cfg = yaml.safe_load((ROOT / "config" / "premarket.yaml").read_text(encoding="utf-8"))
codes = list(pm_cfg["codes"])[:50]

client = KabuStationTickSource(live_cfg)
client.connect()
# 前のプローブの登録を掃除してから 50 銘柄で埋める
client._http_request(
    "PUT", f"{client._API_PREFIX}/unregister/all",
    {"Content-Type": "application/json", "X-API-KEY": client._token or ""}, None)

q: queue.Queue = queue.Queue()
ok, failed = client.subscribe(codes, q, {"ticks": 0})  # state は {"ticks": 0} が契約
print(f"register: OK {len(ok)} / 失敗 {len(failed)}")
if failed:
    print(f"失敗例: {failed[:3]}")

DURATION = 30
t0 = time.time()
counts: dict[str, int] = {}
while time.time() - t0 < DURATION:
    try:
        code, t_ns, price = q.get(timeout=0.5)
    except queue.Empty:
        continue
    counts[code] = counts.get(code, 0) + 1
client.close()

total = sum(counts.values())
print(f"{DURATION} 秒受信: 合計 {total} ティック / 受信銘柄 {len(counts)}/{len(ok)}")
top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
print("上位: " + ", ".join(f"{c}={n}" for c, n in top))
