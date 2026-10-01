"""独立した複数戦略を合成して Sharpe が上がるか。相関が低ければ分散効果で改善するはず。"""
import numpy as np, pandas as pd
d = pd.read_parquet("data/.cache/daily-300.parquet").sort_values(["code","date"])
COST=3.0
feats=[]
for code,g in d.groupby("code",sort=False):
    g=g.reset_index(drop=True); c=g["close"].to_numpy(float); v=g["volume"].to_numpy(float)
    h=g["high"].to_numpy(float); lo=g["low"].to_numpy(float); s=pd.Series(c)
    if len(c)<300: continue
    f=pd.DataFrame({"code":code,"date":g["date"]})
    f["mom"]=s.pct_change(20).to_numpy()*1e4                       # モメンタム
    f["rev"]=-s.pct_change(3).to_numpy()*1e4                       # 短期リバーサル（符号反転）
    r1=s.pct_change(); f["lowvol"]=-(r1.rolling(60).std(ddof=0)*1e4).to_numpy()  # 低ボラ選好
    f["volume"]=-(v/pd.Series(v).rolling(20).mean().replace(0,np.nan)).to_numpy() # 出来高が少ない方
    hh=pd.Series(h).rolling(60).max(); ll=pd.Series(lo).rolling(60).min()
    f["breakout"]=((s-ll)/(hh-ll).replace(0,np.nan)).to_numpy()    # 高値圏
    f["fwd1"]=np.r_[(c[1:]-c[:-1])/c[:-1]*1e4, np.nan]
    feats.append(f)
p=pd.concat(feats,ignore_index=True).dropna(subset=["mom","fwd1"])
p["nfwd1"]=p["fwd1"]-p.groupby("date")["fwd1"].transform("median")
ret=p.pivot_table(index="date",columns="code",values="nfwd1",aggfunc="first")

def strat(feat,pct=5,N=5):
    sig=p.pivot_table(index="date",columns="code",values=feat,aggfunc="first")
    rk=sig.rank(axis=1,pct=True)
    w=pd.DataFrame(0.0,index=rk.index,columns=rk.columns)
    w[rk>=1-pct/100]=1.0; w[rk<=pct/100]=-1.0
    held=w.rolling(N,min_periods=1).mean()
    held=held.div(held.abs().sum(axis=1).replace(0,np.nan),axis=0)
    idx=held.index.intersection(ret.index)
    daily=(held.loc[idx]*ret.loc[idx]).sum(axis=1)
    turn=held.diff().abs().sum(axis=1).reindex(idx).fillna(0)/2
    return (daily-turn*COST).dropna()

def stats(r,label):
    wk=r.groupby(r.index.to_period("W-FRI")).sum(); mo=r.groupby(r.index.to_period("M")).sum()
    yr=r.groupby(r.index.year).sum(); cum=r.cumsum()
    return dict(戦略=label, 年率=r.mean()*245/100, Sharpe=r.mean()/r.std(ddof=0)*np.sqrt(245),
                週勝率=(wk>0).mean(), 月勝率=(mo>0).mean(), 年勝率=(yr>0).mean(),
                最大DD=(cum-cum.cummax()).min()/100)

S={n:strat(n) for n in ("mom","rev","lowvol","volume","breakout")}
rows=[stats(v,k) for k,v in S.items()]
df=pd.DataFrame(S).dropna()
print("=== 戦略間の相関 ===")
print(df.corr().round(2).to_string())
eq=df.mean(axis=1); rows.append(stats(eq,"等ウェイト合成"))
# Sharpe が正の戦略だけ合成
pos=[k for k,v in S.items() if v.mean()>0]
rows.append(stats(df[pos].mean(axis=1), f"プラス戦略のみ合成({len(pos)})"))
r=pd.DataFrame(rows)
print("\n=== 単体 vs 合成 ===")
print(r.to_string(index=False, float_format=lambda v:f"{v:.2f}"))
