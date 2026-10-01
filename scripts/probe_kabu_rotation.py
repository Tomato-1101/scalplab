"""kabuステーション /board ローテーション方式の実機プローブ（読み取りのみ・発注なし）。

なぜ: /board は内部で銘柄を自動登録して 50 枠を消費する（2026-08-19 実測。
troubleshooting.md）。premarket の 291 銘柄巡回には「PUT /unregister/all で枠を
空にしてから次の 50 銘柄を /board する」ローテーションが必要で、その成立性と
所要時間・レイテンシを測るのがこのスクリプト。premarket.py 改修時の参照実装を兼ねる。

実測 2026-08-19（立会中）: 291 銘柄 6 バッチ 1 周 5.9 分・OK 286/291（失敗は全て
タイムアウト・レジスト数エラーはゼロ）・/board レイテンシ中央値 863ms / p95 2.1s。

実行: .venv\\Scripts\\python scripts\\probe_kabu_rotation.py
"""
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from scalplab.live import KabuStationTickSource, load_live_config  # noqa: E402

live_cfg = load_live_config()  # live.local.yaml があればマシン固有設定で上書きされる
pm_cfg = yaml.safe_load((ROOT / "config" / "premarket.yaml").read_text(encoding="utf-8"))
codes = list(pm_cfg["codes"])

client = KabuStationTickSource(live_cfg, auto_stream=False)
client.connect()


def unregister_all():
    return client._http_request(
        "PUT", f"{client._API_PREFIX}/unregister/all",
        {"Content-Type": "application/json", "X-API-KEY": client._token or ""}, None)


THROTTLE = 0.15
batches = [codes[i:i + 50] for i in range(0, len(codes), 50)]
print(f"universe {len(codes)} 銘柄 → {len(batches)} バッチ")

lat_all = []
total_ok = total_ng = 0
err_summary = {}
t_sweep = time.time()
for i, batch in enumerate(batches, 1):
    st, resp = unregister_all()
    if st != 200:
        print(f"[batch {i}] unregister/all 失敗 status={st} resp={resp}")
        break
    ok = ng = 0
    t0 = time.time()
    for c in batch:
        t1 = time.time()
        try:
            status, data = client.board_snapshot(c)
        except Exception as e:  # noqa: BLE001 - 1銘柄の失敗で止めない
            ng += 1
            err_summary[type(e).__name__] = err_summary.get(type(e).__name__, 0) + 1
            continue
        lat_all.append(time.time() - t1)
        if status == 200:
            ok += 1
        else:
            ng += 1
            err = data.get("Code") if isinstance(data, dict) else status
            err_summary[err] = err_summary.get(err, 0) + 1
        time.sleep(THROTTLE)
    total_ok += ok
    total_ng += ng
    print(f"[batch {i}] OK={ok} NG={ng} 所要 {time.time() - t0:.1f} 秒")

sweep_sec = time.time() - t_sweep
print()
print(f"全 {len(codes)} 銘柄 1 周: OK={total_ok} NG={total_ng} 所要 {sweep_sec/60:.1f} 分")
if err_summary:
    print(f"エラー内訳: {err_summary}")
if lat_all:
    lat_sorted = sorted(lat_all)
    p95 = lat_sorted[int(len(lat_sorted) * 0.95)]
    print(f"/board レイテンシ: 中央値 {statistics.median(lat_all)*1000:.0f}ms / "
          f"平均 {statistics.mean(lat_all)*1000:.0f}ms / p95 {p95*1000:.0f}ms / "
          f"最大 {max(lat_all)*1000:.0f}ms（throttle {THROTTLE}s は含まず）")
