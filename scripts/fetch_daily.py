"""検証期間を伸ばすため yfinance で日足を長期取得する（32営業日では 5 日保有が実質6回転しかない）。"""
import pandas as pd, yfinance as yf, time
from pathlib import Path
from scalplab.backtest.explore import available_dates, available_codes
from scalplab.datafeed.universe import moomoo_to_yf

codes = available_codes(available_dates())
print(f"対象 {len(codes)} 銘柄の日足を 6 年分取得", flush=True)
out = {}
for i, c in enumerate(codes):
    try:
        h = yf.Ticker(moomoo_to_yf(c)).history(period="6y", interval="1d", auto_adjust=False)
        if len(h) > 100:
            h.index = pd.to_datetime(h.index).tz_localize(None)
            out[c] = h[["Open","High","Low","Close","Volume"]]
    except Exception as e:
        print(f"  失敗 {c}: {e}", flush=True)
    if (i+1) % 20 == 0:
        print(f"  {i+1}/{len(codes)} 完了", flush=True)
    time.sleep(0.15)
df = pd.concat(out, names=["code","date"]).reset_index()
df.columns = ["code","date","open","high","low","close","volume"]
Path("data/.cache").mkdir(exist_ok=True)
df.to_parquet("data/.cache/daily-6y.parquet", index=False)
print(f"\n保存: {df.shape} / {df['code'].nunique()} 銘柄 / {df['date'].min().date()} 〜 {df['date'].max().date()}")
print(f"銘柄あたり平均 {len(df)/df['code'].nunique():.0f} 日")
