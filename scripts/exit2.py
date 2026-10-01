"""【修正版】利確ルールの比較（市場中立の基準を全銘柄で統一）＋ ピーク到達時刻の分布。"""
import numpy as np, pandas as pd
d = pd.read_parquet("data/.cache/daily-300.parquet").sort_values(["code","date"])
rows=[]
for code,g in d.groupby("code",sort=False):
    g=g.reset_index(drop=True)
    if len(g)<300: continue
    o=g["open"].to_numpy(float); c=g["close"].to_numpy(float)
    h=g["high"].to_numpy(float); l=g["low"].to_numpy(float); s=pd.Series(c)
    f=pd.DataFrame({"code":code,"date":g["date"]})
    f["sigma_20"]=(s.pct_change().rolling(20).std(ddof=0)*1e4).shift(1).to_numpy()
    pc=s.shift(1).to_numpy()
    f["gap_sigma"]=((o-pc)/pc*1e4)/f["sigma_20"]
    f["to_close"]=(c-o)/o*1e4; f["to_high"]=(h-o)/o*1e4; f["to_low"]=(l-o)/o*1e4
    rows.append(f)
p=pd.concat(rows,ignore_index=True).dropna(subset=["gap_sigma","to_close"])
mkt=p.groupby("date")["to_close"].median().rename("mkt")     # 市場の日中リターン（全銘柄）
p=p.merge(mkt,on="date")
sel=p[p["gap_sigma"]<=p.groupby("date")["gap_sigma"].transform(lambda x:x.quantile(0.05))].copy()
COST=3.0
def ev(tp,sl):
    v=np.where((sl is not None)&(sel["to_low"]<=-(sl or 0)), -(sl or 0),
        np.where((tp is not None)&(sel["to_high"]>=(tp or 1e9)), (tp or 0), sel["to_close"]))
    r=v-sel["mkt"].to_numpy()-COST
    return r
base=ev(None,None)
print(f"対象 {len(sel):,} 銘柄日 / 基準（引けまで持つ）: {base.mean():+.1f}bp 勝率 {(base>0).mean():.0%}\n")
print(f"{'利確bp':>7}{'損切bp':>7}{'平均bp':>9}{'勝率':>7}{'引け比':>9}")
for tp in (20,40,60,100,150,None):
    for sl in (30,60,100,None):
        r=ev(tp,sl)
        lt="なし" if tp is None else str(tp); ls="なし" if sl is None else str(sl)
        print(f"{lt:>7}{ls:>7}{r.mean():>+9.1f}{(r>0).mean():>7.0%}{r.mean()-base.mean():>+9.1f}")

print("\n=== ピーク到達時刻の分布（自前1分足32日・09:05以降）===")
from scalplab.backtest.explore import available_dates, available_codes
from scalplab.store import loader as store_loader
from scalplab.backtest.screen import filter_jp_session
dates=available_dates(); codes=available_codes(dates)
recs=[]
for dt in dates:
    for c_ in codes:
        try: b=store_loader.load_bars1m(dt,c_)
        except FileNotFoundError: continue
        b=filter_jp_session(b).reset_index(drop=True)
        if len(b)<200: continue
        recs.append(dict(date=dt,code=c_,o=b["open"].iloc[0],
                         am=b.iloc[:150] if len(b)>150 else b, full=b))
# ギャップ下位を選ぶため前日終値が要る
dd=pd.read_parquet("data/.cache/daily-6y.parquet"); dd["ds"]=pd.to_datetime(dd["date"]).dt.strftime("%Y-%m-%d")
pcmap={(r.code,r.ds):r.close for r in dd.itertuples()}
out=[]
for r in recs:
    di=dates.index(r["date"])
    if di==0: continue
    pc=pcmap.get((r["code"],dates[di-1]))
    if pc is None: continue
    gap=(r["o"]-pc)/pc*1e4
    b=r["full"]; c0=b["close"].iloc[0]
    hi_idx=int(np.argmax(b["high"].to_numpy()))
    out.append(dict(date=r["date"],code=r["code"],gap=gap,peak_min=hi_idx,
                    n=len(b), to_high=(b["high"].max()-c0)/c0*1e4,
                    to_close=(b["close"].iloc[-1]-c0)/c0*1e4))
o=pd.DataFrame(out)
g=o[o["gap"]<=o.groupby("date")["gap"].transform(lambda x:x.quantile(0.10))]
print(f"対象 {len(g)} 銘柄日（ギャップ下位10%）/ 09:05 を 0 分とする")
print(f"  ピーク到達の中央値: {g['peak_min'].median():.0f} 分後 / 平均 {g['peak_min'].mean():.0f} 分後")
for lo,hi,lbl in ((0,10,"0-10分"),(10,30,"10-30分"),(30,60,"30-60分"),(60,150,"1-2.5時間"),(150,400,"後場以降")):
    m=(g["peak_min"]>=lo)&(g["peak_min"]<hi)
    print(f"  {lbl:>9}: {m.mean():>5.0%}")
print(f"  09:05→高値 {g['to_high'].mean():+.0f}bp / 09:05→引け {g['to_close'].mean():+.0f}bp")
