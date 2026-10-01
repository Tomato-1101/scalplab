"""分散を効かせるため流動性上位300銘柄（pool.csv）の日足を6年分取得する。"""
import pandas as pd, yfinance as yf, time
from scalplab.datafeed.universe import moomoo_to_yf
codes = pd.read_csv("data/universe/pool.csv")["code"].tolist()
print(f"対象 {len(codes)} 銘柄", flush=True)
out={}
for i,c in enumerate(codes):
    try:
        h = yf.Ticker(moomoo_to_yf(c)).history(period="6y", interval="1d", auto_adjust=False)
        if len(h) > 500:
            h.index = pd.to_datetime(h.index).tz_localize(None)
            out[c] = h[["Open","High","Low","Close","Volume"]]
    except Exception: pass
    if (i+1)%50==0: print(f"  {i+1}/{len(codes)} ({len(out)}件取得)", flush=True)
    time.sleep(0.12)
df = pd.concat(out, names=["code","date"]).reset_index()
df.columns=["code","date","open","high","low","close","volume"]
df.to_parquet("data/.cache/daily-300.parquet", index=False)
print(f"\n保存 {df.shape} / {df['code'].nunique()}銘柄 / {df['date'].min().date()}〜{df['date'].max().date()}")
