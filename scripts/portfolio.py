"""実運用の形で検証: 毎日リバランスするオーバーラップ・ポートフォリオ。

各日、指標の上位 X% をロング／下位 X% をショート（市場中立）。N 日保有なので毎日 1/N ずつ
入れ替わる。日次リターン系列を作って週次・月次・年次の安定性を測る。コストは往復 3bp を
建玉の入替分にだけ課す（1 日あたり 1/N の回転）。
"""
import numpy as np, pandas as pd
d = pd.read_parquet("data/.cache/daily-6y.parquet").sort_values(["code","date"])
COST = 3.0
feats=[]
for code, g in d.groupby("code", sort=False):
    g=g.reset_index(drop=True); c=g["close"].to_numpy(float); s=pd.Series(c)
    f=pd.DataFrame({"code":code,"date":g["date"]})
    f["ret_20"]=s.pct_change(20).to_numpy()*1e4
    m=s.rolling(20).mean(); f["ma_dev_20"]=((s-m)/m*1e4).to_numpy()
    dd=np.diff(c,prepend=c[0])
    au=pd.Series(np.where(dd>0,dd,0)).ewm(alpha=1/14,adjust=False).mean()
    ad=pd.Series(np.where(dd<0,-dd,0)).ewm(alpha=1/14,adjust=False).mean()
    f["rsi_14"]=(100-100/(1+au/ad.replace(0,np.nan))).to_numpy()
    f["fwd1"]=np.r_[(c[1:]-c[:-1])/c[:-1]*1e4, np.nan]   # 翌日リターン（保有中の日次収益）
    feats.append(f)
p=pd.concat(feats,ignore_index=True).dropna(subset=["ret_20","fwd1"])
p["nfwd1"]=p["fwd1"]-p.groupby("date")["fwd1"].transform("median")   # 市場中立の日次収益

def backtest(feat, pct, N, long_short=True):
    """毎日 上位/下位 pct% を建て N 日保有。日次リターン(bp)の Series を返す。"""
    p["rk"]=p.groupby("date")[feat].rank(pct=True)
    lg = p["rk"] >= 1-pct/100
    sh = p["rk"] <= pct/100
    p["w"] = 0.0
    p.loc[lg,"w"] = 1.0
    if long_short: p.loc[sh,"w"] = -1.0
    # N 日保有 = 過去 N 日の建玉の平均を今日持っている
    w = p.pivot_table(index="date", columns="code", values="w", aggfunc="first").fillna(0.0)
    held = w.rolling(N, min_periods=1).mean()                  # 各日の実効ウェイト
    held = held.div(held.abs().sum(axis=1).replace(0,np.nan), axis=0)  # 総エクスポージャ1に正規化
    ret = p.pivot_table(index="date", columns="code", values="nfwd1", aggfunc="first")
    common = held.index.intersection(ret.index)
    daily = (held.loc[common] * ret.loc[common]).sum(axis=1)
    turn = held.diff().abs().sum(axis=1).reindex(common).fillna(0)/2  # 片側回転率
    return daily - turn*COST

print(f"{'指標':<11}{'上位%':>6}{'保有':>5}{'年率bp':>9}{'年率%':>7}{'Sharpe':>8}{'週勝率':>7}{'月勝率':>7}{'年勝率':>7}{'最大DD%':>8}")
rows=[]
for feat in ("ret_20","ma_dev_20","rsi_14"):
    for pct in (10,20,30):
        for N in (5,10,20,40):
            r = backtest(feat,pct,N).dropna()
            if len(r)<500: continue
            wk=r.groupby(r.index.to_period("W-FRI")).sum(); mo=r.groupby(r.index.to_period("M")).sum()
            yr=r.groupby(r.index.year).sum()
            cum=r.cumsum(); dd=(cum-cum.cummax()).min()/100
            ann=r.mean()*245; sharpe=r.mean()/r.std(ddof=0)*np.sqrt(245)
            rows.append(dict(feat=feat,pct=pct,N=N,ann=ann,sharpe=sharpe,wr=(wk>0).mean(),
                             mr=(mo>0).mean(),yr=(yr>0).mean(),dd=dd,nyr=len(yr)))
            print(f"{feat:<11}{pct:>6}{N:>5}{ann:>+9.0f}{ann/100:>+7.2f}{sharpe:>8.2f}"
                  f"{(wk>0).mean():>7.0%}{(mo>0).mean():>7.0%}{(yr>0).mean():>7.0%}{dd:>8.1f}")
b=pd.DataFrame(rows); b.to_csv("/tmp/portfolio.csv",index=False)
print(f"\n年率プラス {int((b.ann>0).sum())}/{len(b)} / Sharpe>1 が {int((b.sharpe>1).sum())} 組")
