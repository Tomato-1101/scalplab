"""premarket parquet の日次分析: 寄り前フィールドの生存状況と「気配 vs 実寄り値」の精度。

なぜ: ギャップ戦略の実行は「寄り前気配から寄り値（ギャップ）を予測して下位 5% を
選ぶ」が核心。録った日ごとに (1) どのフィールドが寄り前に生きているか
(2) 最終気配がどれだけ寄り値を当てたか、を出して蓄積する。

実測 2026-08-19（50 銘柄・08:51〜）: 寄り前に生きるのは Bid/Ask（Sign 0107 寄前気配）・
板 10 段・OverSellQty/UnderBuyQty/MarketOrder*Qty。CalcPrice は前日終値の写しで不動
（板寄せ予想価格ではない）。最終気配 mid vs 寄り値の乖離 中央値 56bp・最大 306bp、
気配ベース予測ギャップと実ギャップの相関 +0.939。

実行: .venv\\Scripts\\python scripts\\analyze_premarket_day.py [YYYY-MM-DD]
      （日付省略時は今日。--csv で data/exchange/ に銘柄別サマリを書き出す）
"""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

args = [a for a in sys.argv[1:] if not a.startswith("-")]
day = args[0] if args else datetime.now().strftime("%Y-%m-%d")
write_csv = "--csv" in sys.argv

path = ROOT / "data" / "premarket" / f"{day}.parquet"
if not path.exists():
    raise SystemExit(f"見つからない: {path}")
df = pd.read_parquet(path)
df["fetched_at"] = pd.to_datetime(df["fetched_at"])
pre = df[df["fetched_at"] < f"{day} 09:00:00"]
post = df[df["fetched_at"] >= f"{day} 09:00:00"]
print(f"{day}: 全 {len(df)} 行 / 寄り前 {len(pre)} 行 / 銘柄 {df['code'].nunique()} / "
      f"収集 {df['fetched_at'].min().time()} - {df['fetched_at'].max().time()}")

# (1) 寄り前に生きているフィールド（非ゼロ率と、寄り前に値が動いた銘柄数）
if len(pre):
    skip = {"fetched_at", "code", "phase", "symbol", "symbolname", "raw"}
    num_cols = [c for c in df.columns if c not in skip and not c.endswith("time")
                and pd.api.types.is_numeric_dtype(df[c])]
    g = pre.groupby("code")
    print("\n寄り前フィールド（非ゼロ率 / 値が動いた銘柄数）:")
    for c in num_cols:
        nz = (pre[c].fillna(0) != 0).mean() * 100
        moved = (g[c].nunique() > 1).sum()
        if nz > 0:
            print(f"  {c:24s} {nz:4.0f}%  動いた銘柄 {moved}/{g.ngroups}")

# (2) 最終気配 vs 実寄り値
rows = []
for code, grp in pre.groupby("code"):
    last = grp.sort_values("fetched_at").iloc[-1]
    opens = post[(post["code"] == code) & post["openingprice"].notna()]
    if not len(opens) or not last["bidprice"] or not last["askprice"]:
        continue
    op = opens["openingprice"].iloc[0]
    mid = (last["bidprice"] + last["askprice"]) / 2
    prev = last["previousclose"]
    rows.append({
        "code": code, "last_quote_time": str(last["fetched_at"].time()),
        "quote_mid": mid, "open": op, "prev_close": prev,
        "quote_vs_open_bp": (mid - op) / op * 1e4,
        "gap_pred_bp": (mid - prev) / prev * 1e4,
        "gap_real_bp": (op - prev) / prev * 1e4,
    })
if rows:
    r = pd.DataFrame(rows)
    dev = r["quote_vs_open_bp"].abs()
    print(f"\n最終気配 mid vs 寄り値（{len(r)} 銘柄）: 乖離中央値 {dev.median():.1f}bp / "
          f"最大 {dev.max():.1f}bp / 予測ギャップと実ギャップの相関 {r['gap_pred_bp'].corr(r['gap_real_bp']):.3f}")
    if write_csv:
        out = ROOT / "data" / "exchange" / f"premarket-summary-{day}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        r.to_csv(out, index=False)
        print(f"銘柄別サマリを書き出した: {out}")
