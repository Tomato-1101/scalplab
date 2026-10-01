"""ギャップリバーサルの頑健性検証: ①コスト感度 ②自前録画1分足での再現 ③日中の値動きの形。"""
import numpy as np, pandas as pd
d = pd.read_parquet("data/.cache/daily-300.parquet").sort_values(["code","date"])
rows=[]
for code,g in d.groupby("code",sort=False):
    g=g.reset_index(drop=True)
    if len(g)<300: continue
    o=g["open"].to_numpy(float); c=g["close"].to_numpy(float); s=pd.Series(c)
    f=pd.DataFrame({"code":code,"date":g["date"]})
    f["sigma_20"]=(s.pct_change().rolling(20).std(ddof=0)*1e4).shift(1).to_numpy()
    pc=s.shift(1).to_numpy(); f["gap"]=(o-pc)/pc*1e4
    f["gap_sigma"]=f["gap"]/f["sigma_20"]; f["intraday"]=(c-o)/o*1e4
    rows.append(f)
p=pd.concat(rows,ignore_index=True).dropna(subset=["gap_sigma","intraday"])
p["n_intra"]=p["intraday"]-p.groupby("date")["intraday"].transform("median")

def pf(pct, cost):
    out={}
    for dt,g in p.groupby("date"):
        g=g.dropna(subset=["gap_sigma"])
        if len(g)<50: continue
        k=max(1,int(len(g)*pct/100))
        out[dt]=g.nsmallest(k,"gap_sigma")["n_intra"].mean()-cost
    return pd.Series(out).sort_index()

print("=== ① コスト感度（下位5%を寄り買い・市場中立）===")
print(f"{'往復コスト':<12}{'年率%':>9}{'Sharpe':>9}{'日勝率':>8}{'週勝率':>8}{'月勝率':>8}{'年勝率':>8}")
for cost in (3,10,20,30,50):
    r=pf(5,cost)
    wk=r.groupby(r.index.to_period("W-FRI")).sum(); mo=r.groupby(r.index.to_period("M")).sum()
    yr=r.groupby(r.index.year).sum()
    print(f"{cost:<12}{r.mean()*245/100:>+9.1f}{r.mean()/r.std(ddof=0)*np.sqrt(245):>9.2f}"
          f"{(r>0).mean():>8.0%}{(wk>0).mean():>8.0%}{(mo>0).mean():>8.0%}{(yr>0).mean():>8.0%}")

print("\n=== ② 自前録画の1分足で再現するか（32営業日・68銘柄）===")
from scalplab.backtest.explore import available_dates, available_codes
from scalplab.store import loader as store_loader
from scalplab.backtest.screen import filter_jp_session
dates=available_dates(); codes=available_codes(dates)
recs=[]
for i,dt in enumerate(dates):
    for c_ in codes:
        try: b=store_loader.load_bars1m(dt,c_)
        except FileNotFoundError: continue
        b=filter_jp_session(b)
        if len(b)<100: continue
        recs.append(dict(date=dt,code=c_,open=b["open"].iloc[0],close=b["close"].iloc[-1]))
r0=pd.DataFrame(recs)
r0["date"]=pd.to_datetime(r0["date"])
r0=r0.sort_values(["code","date"])
r0["pclose"]=r0.groupby("code")["close"].shift(1)
r0=r0.dropna()
r0["gap"]=(r0["open"]-r0["pclose"])/r0["pclose"]*1e4
r0["intraday"]=(r0["close"]-r0["open"])/r0["open"]*1e4
r0["n_intra"]=r0["intraday"]-r0.groupby("date")["intraday"].transform("median")
print(f"自前データ {len(r0)} 銘柄日 / ギャップ中央値 {r0['gap'].median():+.0f}bp")
for pct in (10,20,30):
    out={}
    for dt,g in r0.groupby("date"):
        k=max(1,int(len(g)*pct/100))
        out[dt]=g.nsmallest(k,"gap")["n_intra"].mean()-3.0
    s=pd.Series(out)
    print(f"  下位{pct}%を寄り買い: {s.mean():+7.2f}bp/日 / 日勝率 {(s>0).mean():.0%} / {len(s)}日 / 合計 {s.sum():+.0f}bp")
# 比較: 上位（ギャップアップ）を買った場合
for pct in (10,):
    out={}
    for dt,g in r0.groupby("date"):
        k=max(1,int(len(g)*pct/100))
        out[dt]=g.nlargest(k,"gap")["n_intra"].mean()-3.0
    s=pd.Series(out)
    print(f"  上位{pct}%を寄り買い（対照）: {s.mean():+7.2f}bp/日 / 日勝率 {(s>0).mean():.0%}")
