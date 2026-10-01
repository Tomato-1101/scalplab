"""【先読み修正版】寄りで入って引けで出る。判断材料は前日終値まで＋当日始値のみ。

前版のバグ: ret_5 / ma_dev_20 が「当日終値」を含んでいた。当日終値を知って寄りで買えば
勝つのは当たり前で、見かけの好成績はその産物だった。ここでは全特徴量を 1 日シフトする。
当日始値（ギャップ）は寄りの時点で観測できるので使ってよい。
"""
import numpy as np, pandas as pd
d = pd.read_parquet("data/.cache/daily-300.parquet").sort_values(["code","date"])
COST=3.0
rows=[]
for code,g in d.groupby("code",sort=False):
    g=g.reset_index(drop=True)
    if len(g)<300: continue
    o=g["open"].to_numpy(float); c=g["close"].to_numpy(float)
    h=g["high"].to_numpy(float); l=g["low"].to_numpy(float); v=g["volume"].to_numpy(float)
    s=pd.Series(c)
    f=pd.DataFrame({"code":code,"date":g["date"]})
    # ---- 前日終値までで確定する特徴量（すべて shift(1)）----
    f["ret_5"]=s.pct_change(5).shift(1).to_numpy()*1e4
    f["ret_20"]=s.pct_change(20).shift(1).to_numpy()*1e4
    m=s.rolling(20).mean(); f["ma_dev_20"]=((s-m)/m*1e4).shift(1).to_numpy()
    r1=s.pct_change(); f["sigma_20"]=(r1.rolling(20).std(ddof=0)*1e4).shift(1).to_numpy()
    f["prev_intraday"]=pd.Series((c-o)/o*1e4).shift(1).to_numpy()
    f["vol_ratio"]=(v/pd.Series(v).rolling(20).mean().replace(0,np.nan)).shift(1).to_numpy()
    f["prev_range"]=pd.Series((h-l)/c*1e4).shift(1).to_numpy()
    dd=np.diff(c,prepend=c[0])
    au=pd.Series(np.where(dd>0,dd,0)).ewm(alpha=1/14,adjust=False).mean()
    ad=pd.Series(np.where(dd<0,-dd,0)).ewm(alpha=1/14,adjust=False).mean()
    f["rsi_14"]=(100-100/(1+au/ad.replace(0,np.nan))).shift(1).to_numpy()
    # ---- 当日の寄りで観測できるもの ----
    pc=s.shift(1).to_numpy()
    f["gap"]=(o-pc)/pc*1e4
    f["gap_sigma"]=f["gap"]/f["sigma_20"]
    # ---- 取りにいく対象（寄り→引け）----
    f["intraday"]=(c-o)/o*1e4
    rows.append(f)
p=pd.concat(rows,ignore_index=True).dropna(subset=["gap","intraday","sigma_20","ret_5"])
p["n_intra"]=p["intraday"]-p.groupby("date")["intraday"].transform("median")
print(f"パネル {p.shape} / {p['code'].nunique()}銘柄")
days=np.sort(p["date"].unique()); split=days[int(len(days)*0.6)]
TR=(p["date"]<split).to_numpy(); TE=(p["date"]>=split).to_numpy()
FE=["gap","gap_sigma","ret_5","ret_20","ma_dev_20","sigma_20","prev_intraday","vol_ratio","prev_range","rsi_14"]
res=[]
for f in FE:
    x=p[f].to_numpy(); ok=np.isfinite(x); xt=x[TR&ok]
    if len(xt)<2000: continue
    for pct in (5,10,20):
        hi=np.percentile(xt,100-pct); lo=np.percentile(xt,pct)
        for zl,zm in ((f"top{pct}",ok&(x>=hi)),(f"bot{pct}",ok&(x<=lo))):
            for dr in (1,-1):
                y=p["n_intra"].to_numpy()
                a=y[zm&TR]; b=y[zm&TE]; a=a[np.isfinite(a)]; b=b[np.isfinite(b)]
                if len(a)<800 or len(b)<500: continue
                res.append((f,zl,dr,(a*dr).mean()-COST,len(a),(b*dr).mean()-COST,len(b)))
r=pd.DataFrame(res,columns=["feat","zone","dir","tr","tr_n","te","te_n"])
r.to_csv("/tmp/gap_fixed.csv",index=False)
print(f"\n=== 先読み修正後 ===")
print(f"評価 {len(r)} / 相関 corr(学習,検証) = {r.tr.corr(r.te):+.3f}")
print(f"学習+: {(r.tr>0).sum()} / 検証+: {(r.te>0).sum()} / 両方+: {((r.tr>0)&(r.te>0)).sum()}")
for q in (5,10):
    t=r.nlargest(q,"tr"); print(f"  学習トップ{q}の検証平均 {t.te.mean():+6.2f}bp（プラス率 {(t.te>0).mean():.0%}）")
sel=r[(r.tr>0)&(r.te>0)].sort_values("te",ascending=False)
print(f"\n学習・検証とも プラス: {len(sel)}組")
if len(sel): print(sel.head(12).to_string(index=False,float_format=lambda v:f"{v:.2f}"))
