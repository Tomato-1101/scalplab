"""日足6年での本検証。市場中立・コスト3bp・時系列分割＋年別の安定性まで見る。"""
import numpy as np, pandas as pd
d = pd.read_parquet("data/.cache/daily-6y.parquet").sort_values(["code","date"])
COST = 3.0

feats = []
for code, g in d.groupby("code", sort=False):
    g = g.reset_index(drop=True)
    c = g["close"].to_numpy(float); h = g["high"].to_numpy(float)
    lo = g["low"].to_numpy(float); v = g["volume"].to_numpy(float)
    f = pd.DataFrame({"code": code, "date": g["date"], "close": c})
    s = pd.Series(c)
    for k in (1,5,20,60):
        f[f"ret_{k}"] = s.pct_change(k).to_numpy()*1e4
    for w in (5,20,60):
        m = s.rolling(w).mean()
        f[f"ma_dev_{w}"] = ((s-m)/m*1e4).to_numpy()
    r1 = s.pct_change()
    for w in (20,60):
        f[f"sigma_{w}"] = (r1.rolling(w).std(ddof=0)*1e4).to_numpy()
    tr = np.maximum(h-lo, np.maximum(abs(h-np.roll(c,1)), abs(lo-np.roll(c,1)))); tr[0]=h[0]-lo[0]
    f["atr_20"] = (pd.Series(tr).rolling(20).mean().to_numpy()/c*1e4)
    dd = np.diff(c, prepend=c[0])
    au = pd.Series(np.where(dd>0,dd,0)).ewm(alpha=1/14, adjust=False).mean()
    ad = pd.Series(np.where(dd<0,-dd,0)).ewm(alpha=1/14, adjust=False).mean()
    f["rsi_14"] = (100-100/(1+au/ad.replace(0,np.nan))).to_numpy()
    f["vol_ratio"] = (v/pd.Series(v).rolling(20).mean().replace(0,np.nan)).to_numpy()
    hh = pd.Series(h).rolling(60).max(); ll = pd.Series(lo).rolling(60).min()
    f["range_pos_60"] = ((s-ll)/(hh-ll).replace(0,np.nan)).to_numpy()
    for N in (1,3,5,10,20):
        fwd = np.full(len(c), np.nan)
        if len(c) > N: fwd[:-N] = (c[N:]-c[:-N])/c[:-N]*1e4
        f[f"fwd_{N}"] = fwd
    feats.append(f)
p = pd.concat(feats, ignore_index=True)
# 市場中立化（同じ日の全銘柄中央値を引く）
for N in (1,3,5,10,20):
    p[f"n_{N}"] = p[f"fwd_{N}"] - p.groupby("date")[f"fwd_{N}"].transform("median")
p = p.dropna(subset=["ma_dev_20","sigma_20","rsi_14"])
p["year"] = p["date"].dt.year
print(f"日足パネル: {p.shape} / {p['code'].nunique()}銘柄 / {p['date'].min().date()}〜{p['date'].max().date()}")

FEATS = [c for c in p.columns if c not in ("code","date","close","year") and not c.startswith(("fwd_","n_"))]
dates = np.sort(p["date"].unique()); split = dates[int(len(dates)*0.6)]
TR = (p["date"]<split).to_numpy(); TE = (p["date"]>=split).to_numpy()
print(f"学習 〜{pd.Timestamp(split).date()} ({TR.sum():,}) / 検証 {pd.Timestamp(split).date()}〜 ({TE.sum():,})\n")
rows=[]
for f in FEATS:
    x = p[f].to_numpy(); ok = np.isfinite(x)
    xt = x[TR&ok]
    if len(xt) < 2000: continue
    for pct in (10,20,30):
        hi = np.percentile(xt,100-pct); lo = np.percentile(xt,pct)
        for zl, zm in ((f"top{pct}", ok&(x>=hi)), (f"bot{pct}", ok&(x<=lo))):
            for dd_ in (1,-1):
                for N in (1,3,5,10,20):
                    y = p[f"n_{N}"].to_numpy()
                    a = y[zm&TR]; b = y[zm&TE]
                    a=a[np.isfinite(a)]; b=b[np.isfinite(b)]
                    if len(a)<1000 or len(b)<500: continue
                    rows.append((f,zl,dd_,N,(a*dd_).mean()-COST,len(a),(b*dd_).mean()-COST,len(b)))
r = pd.DataFrame(rows, columns=["feat","zone","dir","N","tr_bp","tr_n","te_bp","te_n"])
r.to_csv("/tmp/daily_scan.csv", index=False)
print(f"評価 {len(r):,} 通り / 相関 corr(学習,検証) = {r.tr_bp.corr(r.te_bp):+.3f}")
print(f"学習+: {(r.tr_bp>0).sum()} / 検証+: {(r.te_bp>0).sum()} / 両方+: {((r.tr_bp>0)&(r.te_bp>0)).sum()}")
for q in (10,30,50):
    t = r.nlargest(q,"tr_bp"); print(f"学習トップ{q:>2}の検証平均: {t.te_bp.mean():+7.1f}bp（プラス率 {(t.te_bp>0).mean():.0%}）")
print("\n=== 学習・検証とも プラス の上位15 ===")
sel = r[(r.tr_bp>0)&(r.te_bp>0)].sort_values("te_bp",ascending=False)
print(sel.head(15).to_string(index=False, float_format=lambda v:f"{v:.1f}"))
