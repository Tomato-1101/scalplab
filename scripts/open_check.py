"""yfinance 日足の Open が本当に寄り付き価格か、自前録画の 1 分足 09:00 始値と直接照合する。"""
import numpy as np, pandas as pd
from scalplab.backtest.explore import available_dates, available_codes
from scalplab.store import loader as store_loader
from scalplab.backtest.screen import filter_jp_session

dates=available_dates(); codes=available_codes(dates)
d=pd.read_parquet("data/.cache/daily-6y.parquet")
d["date"]=pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
dd={(r.code,r.date):(r.open,r.close,r.high,r.low) for r in d.itertuples()}

rows=[]
for dt in dates:
    for c_ in codes:
        try: b=store_loader.load_bars1m(dt,c_)
        except FileNotFoundError: continue
        b=filter_jp_session(b)
        if len(b)<100: continue
        k=dd.get((c_,dt))
        if k is None: continue
        rows.append(dict(date=dt,code=c_,my_open=b["open"].iloc[0],yf_open=k[0],
                         my_close=b["close"].iloc[-1],yf_close=k[1],
                         my_high=b["high"].max(), yf_high=k[2], my_low=b["low"].min(), yf_low=k[3]))
r=pd.DataFrame(rows)
for col in ("open","close","high","low"):
    r[f"d_{col}"]=(r[f"yf_{col}"]-r[f"my_{col}"])/r[f"my_{col}"]*1e4
print(f"照合 {len(r)} 銘柄日\n")
print(f"{'項目':<8}{'一致率(±5bp)':>14}{'|差| 中央値':>12}{'|差| 平均':>11}{'|差| 95%':>11}")
for col in ("open","close","high","low"):
    a=r[f"d_{col}"].abs()
    print(f"{col:<8}{(a<5).mean():>13.1%}{a.median():>12.1f}{a.mean():>11.1f}{a.quantile(.95):>11.1f}")
print("\n=== 始値がズレている例（|差|上位8）===")
print(r.reindex(r["d_open"].abs().sort_values(ascending=False).index)
      [["date","code","my_open","yf_open","d_open"]].head(8).to_string(index=False,float_format=lambda v:f"{v:.1f}"))
# 自前の始値で作ったギャップ vs yf の始値で作ったギャップ
print("\n=== 前日終値(yf) 起点のギャップを、どちらの始値で作るかで比較 ===")
r2=r.copy(); r2["date"]=pd.to_datetime(r2["date"]); r2=r2.sort_values(["code","date"])
r2["pc"]=r2.groupby("code")["yf_close"].shift(1)
r2=r2.dropna(subset=["pc"])
r2["gap_yf"]=(r2["yf_open"]-r2["pc"])/r2["pc"]*1e4
r2["gap_my"]=(r2["my_open"]-r2["pc"])/r2["pc"]*1e4
r2["intra_yf"]=(r2["yf_close"]-r2["yf_open"])/r2["yf_open"]*1e4
r2["intra_my"]=(r2["my_close"]-r2["my_open"])/r2["my_open"]*1e4
print(f"ギャップ相関 {r2['gap_yf'].corr(r2['gap_my']):+.3f} / 日中リターン相関 {r2['intra_yf'].corr(r2['intra_my']):+.3f}")
print(f"ギャップ中央値  yf {r2['gap_yf'].median():+.0f}bp / 自前 {r2['gap_my'].median():+.0f}bp")
print(f"日中リターン中央値 yf {r2['intra_yf'].median():+.0f}bp / 自前 {r2['intra_my'].median():+.0f}bp")
