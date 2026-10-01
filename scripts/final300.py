"""300銘柄版の本検証。分散が効いて Sharpe と週勝率が上がるかを見る。"""
import numpy as np, pandas as pd
d = pd.read_parquet("data/.cache/daily-300.parquet").sort_values(["code","date"])
COST=3.0
feats=[]
for code,g in d.groupby("code",sort=False):
    g=g.reset_index(drop=True); c=g["close"].to_numpy(float); s=pd.Series(c)
    if len(c)<300: continue
    f=pd.DataFrame({"code":code,"date":g["date"]})
    for k in (5,20,60): f[f"ret_{k}"]=s.pct_change(k).to_numpy()*1e4
    for w in (5,20,60):
        m=s.rolling(w).mean(); f[f"ma_dev_{w}"]=((s-m)/m*1e4).to_numpy()
    dd=np.diff(c,prepend=c[0])
    au=pd.Series(np.where(dd>0,dd,0)).ewm(alpha=1/14,adjust=False).mean()
    ad=pd.Series(np.where(dd<0,-dd,0)).ewm(alpha=1/14,adjust=False).mean()
    f["rsi_14"]=(100-100/(1+au/ad.replace(0,np.nan))).to_numpy()
    f["fwd1"]=np.r_[(c[1:]-c[:-1])/c[:-1]*1e4, np.nan]
    feats.append(f)
p=pd.concat(feats,ignore_index=True).dropna(subset=["ma_dev_20","fwd1"])
p["nfwd1"]=p["fwd1"]-p.groupby("date")["fwd1"].transform("median")
print(f"パネル {p.shape} / {p['code'].nunique()}銘柄 / 1日あたり平均 {p.groupby('date').size().mean():.0f}銘柄")
ret=p.pivot_table(index="date",columns="code",values="nfwd1",aggfunc="first")

def run(feat,pct,N):
    sig=p.pivot_table(index="date",columns="code",values=feat,aggfunc="first")
    rk=sig.rank(axis=1,pct=True)
    w=pd.DataFrame(0.0,index=rk.index,columns=rk.columns)
    w[rk>=1-pct/100]=1.0; w[rk<=pct/100]=-1.0
    held=w.rolling(N,min_periods=1).mean()
    held=held.div(held.abs().sum(axis=1).replace(0,np.nan),axis=0)
    idx=held.index.intersection(ret.index)
    daily=(held.loc[idx]*ret.loc[idx]).sum(axis=1)
    turn=held.diff().abs().sum(axis=1).reindex(idx).fillna(0)/2
    r=(daily-turn*COST).dropna()
    wk=r.groupby(r.index.to_period("W-FRI")).sum(); mo=r.groupby(r.index.to_period("M")).sum()
    yr=r.groupby(r.index.year).sum(); cum=r.cumsum()
    return dict(feat=feat,pct=pct,N=N,ann=r.mean()*245,sharpe=r.mean()/r.std(ddof=0)*np.sqrt(245),
                wr=(wk>0).mean(),mr=(mo>0).mean(),yr=(yr>0).mean(),
                dd=(cum-cum.cummax()).min()/100,ser=r,years=yr)
print(f"\n{'指標':<11}{'上位%':>6}{'保有':>5}{'年率%':>8}{'Sharpe':>8}{'週勝率':>7}{'月勝率':>7}{'年勝率':>7}{'最大DD%':>8}")
best=None; rows=[]
for feat in ("ma_dev_20","ret_20","rsi_14","ma_dev_60","ret_5"):
    for pct in (5,10,20):
        for N in (5,10,20):
            r=run(feat,pct,N); rows.append({k:v for k,v in r.items() if k not in("ser","years")})
            print(f"{feat:<11}{pct:>6}{N:>5}{r['ann']/100:>+8.2f}{r['sharpe']:>8.2f}"
                  f"{r['wr']:>7.0%}{r['mr']:>7.0%}{r['yr']:>7.0%}{r['dd']:>8.1f}")
            if best is None or r["sharpe"]>best["sharpe"]: best=r
b=pd.DataFrame(rows); b.to_csv("/tmp/final300.csv",index=False)
print(f"\n★最良: {best['feat']} 上位{best['pct']}% 保有{best['N']}日")
print(f"   年率 {best['ann']/100:+.2f}% / Sharpe {best['sharpe']:.2f} / 最大DD {best['dd']:.1f}% / 週勝率 {best['wr']:.0%} / 月勝率 {best['mr']:.0%}")
print("   年別: " + " ".join(f"{y}:{v/100:+.1f}%" for y,v in best["years"].items()))
print(f"\n年率プラス {int((b.ann>0).sum())}/{len(b)} / Sharpe>1 {int((b.sharpe>1).sum())}組")
best["ser"].to_csv("/tmp/best300.csv")
