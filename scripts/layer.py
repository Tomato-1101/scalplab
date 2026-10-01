"""「どれを選ぶか」の余地を測る: ギャップの深さ・出来高・値幅で層別すると成績が変わるか。
LLM 判断を入れる前に、定量ルールでどこまで選別できるかを確認する。"""
import numpy as np, pandas as pd
d = pd.read_parquet("data/.cache/daily-300.parquet").sort_values(["code","date"])
rows=[]
for code,g in d.groupby("code",sort=False):
    g=g.reset_index(drop=True)
    if len(g)<300: continue
    o=g["open"].to_numpy(float); c=g["close"].to_numpy(float)
    h=g["high"].to_numpy(float); l=g["low"].to_numpy(float); v=g["volume"].to_numpy(float); s=pd.Series(c)
    f=pd.DataFrame({"code":code,"date":g["date"]})
    f["sigma_20"]=(s.pct_change().rolling(20).std(ddof=0)*1e4).shift(1).to_numpy()
    pc=s.shift(1).to_numpy()
    f["gap_sigma"]=((o-pc)/pc*1e4)/f["sigma_20"]
    f["prev_vol_ratio"]=(v/pd.Series(v).rolling(20).mean().replace(0,np.nan)).shift(1).to_numpy()
    f["prev_range"]=pd.Series((h-l)/c*1e4).shift(1).to_numpy()
    f["px"]=o
    f["to_close"]=(c-o)/o*1e4
    rows.append(f)
p=pd.concat(rows,ignore_index=True).dropna(subset=["gap_sigma","to_close"])
p=p.merge(p.groupby("date")["to_close"].median().rename("mkt"),on="date")
p["net"]=p["to_close"]-p["mkt"]-3.0
sel=p[p["gap_sigma"]<=p.groupby("date")["gap_sigma"].transform(lambda x:x.quantile(0.05))].copy()
print(f"母集団（ギャップ下位5%）{len(sel):,} 銘柄日 / 平均 {sel['net'].mean():+.1f}bp 勝率 {(sel['net']>0).mean():.0%}\n")

print("=== ギャップの深さで層別（σ何倍下に窓を開けたか）===")
sel["gs_bin"]=pd.cut(sel["gap_sigma"],[-99,-5,-3,-2,-1.5,0])
for b,g in sel.groupby("gs_bin",observed=True):
    print(f"  {str(b):>14}: {len(g):>6,}件 平均 {g['net'].mean():>+7.1f}bp 勝率 {(g['net']>0).mean():>4.0%}")
print("\n=== 前日の出来高比で層別 ===")
sel["v_bin"]=pd.qcut(sel["prev_vol_ratio"],4,duplicates="drop")
for b,g in sel.groupby("v_bin",observed=True):
    print(f"  {str(b):>18}: {len(g):>6,}件 平均 {g['net'].mean():>+7.1f}bp 勝率 {(g['net']>0).mean():>4.0%}")
print("\n=== 株価水準で層別（流動性・呼値の代理）===")
sel["p_bin"]=pd.cut(sel["px"],[0,500,1000,3000,10000,1e9])
for b,g in sel.groupby("p_bin",observed=True):
    print(f"  {str(b):>18}: {len(g):>6,}件 平均 {g['net'].mean():>+7.1f}bp 勝率 {(g['net']>0).mean():>4.0%}")
print("\n=== 前日の値幅で層別 ===")
sel["r_bin"]=pd.qcut(sel["prev_range"],4,duplicates="drop")
for b,g in sel.groupby("r_bin",observed=True):
    print(f"  {str(b):>18}: {len(g):>6,}件 平均 {g['net'].mean():>+7.1f}bp 勝率 {(g['net']>0).mean():>4.0%}")
