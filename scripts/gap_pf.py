"""ギャップリバーサルを実運用形で検証: 毎日「寄りで買って引けで売る」だけ。

保有が重複しないので日次リターンがそのまま独立サンプル＝統計がクリーン。
市場中立版（同日の全銘柄中央値を引く＝先物ヘッジ相当）と、ヘッジ無しの素の版を両方出す。
"""
import numpy as np, pandas as pd
d = pd.read_parquet("data/.cache/daily-300.parquet").sort_values(["code","date"])
COST=3.0
rows=[]
for code,g in d.groupby("code",sort=False):
    g=g.reset_index(drop=True)
    if len(g)<300: continue
    o=g["open"].to_numpy(float); c=g["close"].to_numpy(float); s=pd.Series(c)
    r1=s.pct_change()
    f=pd.DataFrame({"code":code,"date":g["date"]})
    f["sigma_20"]=(r1.rolling(20).std(ddof=0)*1e4).shift(1).to_numpy()
    pc=s.shift(1).to_numpy()
    f["gap"]=(o-pc)/pc*1e4
    f["gap_sigma"]=f["gap"]/f["sigma_20"]
    f["intraday"]=(c-o)/o*1e4
    rows.append(f)
p=pd.concat(rows,ignore_index=True).dropna(subset=["gap_sigma","intraday"])
p["n_intra"]=p["intraday"]-p.groupby("date")["intraday"].transform("median")

def pf(pct, col="gap_sigma", both=False, neutral=True):
    """毎日 下位 pct% を寄り買い（both なら上位 pct% を寄り売りも）。日次リターン(bp)。"""
    y = "n_intra" if neutral else "intraday"
    out={}
    for dt, g in p.groupby("date"):
        g=g.dropna(subset=[col])
        if len(g)<50: continue
        k=max(1,int(len(g)*pct/100))
        lg=g.nsmallest(k,col)[y].mean()
        r = lg - COST
        if both:
            sh=-g.nlargest(k,col)[y].mean()
            r=(lg+sh)/2 - COST
        out[dt]=r
    return pd.Series(out).sort_index()

def stats(r,label):
    wk=r.groupby(r.index.to_period("W-FRI")).sum(); mo=r.groupby(r.index.to_period("M")).sum()
    yr=r.groupby(r.index.year).sum(); cum=r.cumsum()
    return dict(構成=label, 日数=len(r), 年率=r.mean()*245/100, Sharpe=r.mean()/r.std(ddof=0)*np.sqrt(245),
                日勝率=(r>0).mean(), 週勝率=(wk>0).mean(), 月勝率=(mo>0).mean(), 年勝率=(yr>0).mean(),
                最大DD=(cum-cum.cummax()).min()/100, bp_per=r.mean())

rows2=[]
for pct in (2,5,10,20):
    rows2.append(stats(pf(pct,both=False,neutral=True), f"下位{pct}%を寄り買い(中立)"))
    rows2.append(stats(pf(pct,both=True,neutral=True),  f"下位{pct}%買い+上位{pct}%売り(中立)"))
rows2.append(stats(pf(5,both=False,neutral=False), "下位5%を寄り買い(ヘッジ無)"))
r=pd.DataFrame(rows2)
print(r.to_string(index=False,float_format=lambda v:f"{v:.2f}"))
best=pf(5,both=True,neutral=True)
print(f"\n=== 下位5%買い+上位5%売り(中立) の年別 ===")
for y,v in best.groupby(best.index.year).sum().items(): print(f"  {y}: {v/100:+7.2f}%")
best.to_csv("/tmp/gap_best.csv")
