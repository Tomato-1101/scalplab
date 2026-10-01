"""デイトレ再挑戦①: 1分足のノイズ（bid-ask bounce）を潰すため 5/15 分足へ集約して再探索。

1分足では gross の平均がちょうどゼロだった。時間軸を粗くすると信号対雑音比が上がる可能性がある。
同じ枠組み（市場中立・学習検証分割・コスト3bp）で比較する。
"""
import numpy as np, pandas as pd
from scalplab.backtest.screen import filter_jp_session, iter_jp_sessions
from scalplab.store import loader as store_loader
from scalplab.backtest.explore import available_dates, available_codes, WARMUP

COST = 3.0
dates = available_dates(); codes = available_codes(dates)

def agg_bars(bars, k):
    """k 分足へ集約（セッション先頭から k 本ずつ）。"""
    g = np.arange(len(bars)) // k
    return pd.DataFrame({
        "open": bars["open"].groupby(g).first(), "high": bars["high"].groupby(g).max(),
        "low": bars["low"].groupby(g).min(), "close": bars["close"].groupby(g).last(),
        "volume": bars["volume"].groupby(g).sum(),
        "time": bars["time"].groupby(g).first()}).reset_index(drop=True)

def feats(b):
    c = b["close"].to_numpy(float); h=b["high"].to_numpy(float); lo=b["low"].to_numpy(float)
    v = b["volume"].to_numpy(float); s = pd.Series(c); n=len(c)
    f = pd.DataFrame({"time": b["time"], "close": c})
    for k in (1,3,6):
        f[f"ret_{k}"] = s.pct_change(k).to_numpy()*1e4
    for w in (6,12):
        m = s.rolling(w).mean(); f[f"ma_dev_{w}"] = ((s-m)/m*1e4).to_numpy()
        sd = s.rolling(w).std(ddof=0)
        f[f"bb_z_{w}"] = ((s-m)/sd.replace(0,np.nan)).to_numpy()
    r1 = s.pct_change()
    f["sigma_12"] = (r1.rolling(12).std(ddof=0)*1e4).to_numpy()
    f["ret1_sigma"] = (r1/r1.rolling(12).std(ddof=0).shift(1)).to_numpy()
    dd = np.diff(c, prepend=c[0])
    au = pd.Series(np.where(dd>0,dd,0)).ewm(alpha=1/9,adjust=False).mean()
    ad = pd.Series(np.where(dd<0,-dd,0)).ewm(alpha=1/9,adjust=False).mean()
    f["rsi_9"] = (100-100/(1+au/ad.replace(0,np.nan))).to_numpy()
    tp=(h+lo+c)/3; cv=np.cumsum(v)
    vw=np.divide(np.cumsum(tp*v),cv,out=tp.copy(),where=cv>0)
    f["vwap_dev"]=(c-vw)/vw*1e4
    f["vol_ratio"]=(v/pd.Series(v).rolling(12).mean().replace(0,np.nan)).to_numpy()
    f["bar_idx"]=np.arange(n,dtype=float)
    for H in (1,2,3,6):
        fw=np.full(n,np.nan)
        if n>H: fw[:-H]=(c[H:]-c[:-H])/c[:-H]*1e4
        f[f"fwd_{H}"]=fw
    return f

for K in (5, 15):
    rows=[]
    for d in dates:
        for code in codes:
            try: bars = store_loader.load_bars1m(d, code)
            except FileNotFoundError: continue
            for si, day in enumerate(iter_jp_sessions(filter_jp_session(bars))):
                b = agg_bars(day.reset_index(drop=True), K)
                if len(b) < 20: continue
                f = feats(b).iloc[12:].copy()
                f["date"]=d; f["code"]=code; f["sess"]=si
                rows.append(f)
    p = pd.concat(rows, ignore_index=True)
    p["date"]=pd.to_datetime(p["date"])
    # 市場中立化
    for H in (1,2,3,6):
        p[f"n_{H}"] = p[f"fwd_{H}"] - p.groupby(["date","sess","time"])[f"fwd_{H}"].transform("median")
    days = np.array(sorted(p["date"].unique())); split=days[len(days)//2]
    TR=(p["date"]<split).to_numpy(); TE=(p["date"]>=split).to_numpy()
    FE=[c for c in p.columns if c not in ("date","code","sess","time","close") and not c.startswith(("fwd_","n_"))]
    res=[]
    for f in FE:
        x=p[f].to_numpy(); ok=np.isfinite(x); xt=x[TR&ok]
        if len(xt)<3000: continue
        for pct in (5,10,20):
            hi=np.percentile(xt,100-pct); lo_=np.percentile(xt,pct)
            for zl,zm in ((f"top{pct}",ok&(x>=hi)),(f"bot{pct}",ok&(x<=lo_))):
                for dr in (1,-1):
                    for H in (1,2,3,6):
                        y=p[f"n_{H}"].to_numpy()
                        a=y[zm&TR]; b_=y[zm&TE]; a=a[np.isfinite(a)]; b_=b_[np.isfinite(b_)]
                        if len(a)<500 or len(b_)<500: continue
                        res.append((f,zl,dr,H,(a*dr).mean()-COST,len(a),(b_*dr).mean()-COST,len(b_)))
    r=pd.DataFrame(res,columns=["feat","zone","dir","H","tr","tr_n","te","te_n"])
    r.to_csv(f"/tmp/bar{K}.csv",index=False)
    print(f"\n=== {K}分足（{len(p):,}バー / {p['code'].nunique()}銘柄）===")
    print(f"評価 {len(r):,} / 相関 corr(学習,検証) = {r.tr.corr(r.te):+.3f}")
    print(f"学習+: {(r.tr>0).sum()} / 検証+: {(r.te>0).sum()} / 両方+: {((r.tr>0)&(r.te>0)).sum()}")
    for q in (10,30):
        t=r.nlargest(q,"tr"); print(f"  学習トップ{q:>2}の検証平均 {t.te.mean():+7.2f}bp（プラス率 {(t.te>0).mean():.0%}）")
    sel=r[(r.tr>2)&(r.te>2)].sort_values("te",ascending=False)
    print(f"  学習・検証とも+2bp超: {len(sel)}組")
    if len(sel): print(sel.head(8).to_string(index=False,float_format=lambda v:f"{v:.2f}"))
