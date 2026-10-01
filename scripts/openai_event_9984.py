"""OpenAI の発表翌営業日に 9984（ソフトバンクG）がどう動いたかを一覧にする。

なぜ: 2026-09-03 の GPT-6 Astra 発表で「前日引けに買って翌日売る」が +10% になり、
「発表翌日は上がる」「上がる日は寄りで売ると取り逃がす」を確かめる必要があったため。
一般のギャップアップ日の統計（寄りで売るのが最良）が、この材料日には当てはまらない。

データ: yfinance の日足 2 年 + 1 時間足 730 日（1 時間足の列は「その時間の終値→大引け」の変化率。
09:00 列は 10:00 時点、11:00 列は前場引け、12:00 列は 13:00 時点を指す）。
発表日は US 日付なので、日本の反応日は「その日付より後の最初の営業日」。
実行: .venv/bin/python scripts/openai_event_9984.py
"""
import pandas as pd
import yfinance as yf

# 出典: github.com/jqueryscript/chatgpt-timeline と Wikipedia 各モデル記事（2026-09-04 取得）
EVENTS = {
    "2025-01-31": "o3-mini", "2025-02-27": "GPT-4.5", "2025-04-14": "GPT-4.1",
    "2025-04-16": "o3/o4-mini", "2025-08-07": "GPT-5", "2025-09-30": "Sora 2",
    "2025-10-21": "ChatGPT Atlas", "2025-11-12": "GPT-5.1", "2025-12-11": "GPT-5.2",
    "2026-02-05": "GPT-5.3-Codex", "2026-03-05": "GPT-5.4", "2026-04-23": "GPT-5.5",
    "2026-06-26": "GPT-5.6 preview", "2026-07-09": "GPT-5.6", "2026-08-10": "GPT-5.6-Cyber",
    "2026-09-03": "GPT-6 Astra",
}
MAJOR = {"GPT-5", "GPT-5.6", "GPT-6 Astra"}  # 世代交代クラス

t = yf.Ticker("9984.T")
d = t.history(period="2y", interval="1d", auto_adjust=False)
d.index = pd.to_datetime(d.index.tz_convert("Asia/Tokyo").date)
h = t.history(period="730d", interval="1h", auto_adjust=False)
h.index = h.index.tz_convert("Asia/Tokyo")
d["prev"] = d["Close"].shift(1)

rows = []
for us, name in EVENTS.items():
    nxt = d.index[d.index > pd.Timestamp(us)]
    if len(nxt) == 0:
        continue
    day = nxt[0]
    r = d.loc[day].copy()
    g = h[h.index.date == day.date()]
    if pd.isna(r.Close) and len(g):  # 当日分は yfinance 日足の終値が翌朝まで NaN のことがある
        r.Close = g["Close"].iloc[-1]
    hourly = {ts.strftime("%H:%M"): (r.Close / c - 1) * 100 for ts, c in g["Close"].items()}
    rows.append(dict(
        model=name, major="*" if name in MAJOR else "", day=day.strftime("%y/%m/%d"),
        gap=(r.Open / r.prev - 1) * 100, day_ret=(r.Close / r.prev - 1) * 100,
        open_to_close=(r.Close / r.Open - 1) * 100,
        hi_hour=g["High"].idxmax().strftime("%H:%M") if len(g) else "?", **hourly))
E = pd.DataFrame(rows)
pd.set_option("display.width", 220)
print(E.round(2).to_string(index=False))
print(f"\n発表翌日 {len(E)} 件の中央値: gap={E.gap.median():+.2f}% 前日比={E.day_ret.median():+.2f}% "
      f"寄り→引け={E.open_to_close.median():+.2f}%  前日比>+3%: {(E.day_ret > 3).sum()} 件")
M = E[E.major == "*"]
print(f"大型 {len(M)} 件: gap 中央値={M.gap.median():+.2f}% 前日比中央値={M.day_ret.median():+.2f}% "
      f"寄り→引け中央値={M.open_to_close.median():+.2f}%")

# 比較: 発表日以外で寄りギャップ +2% 超の日（一般のギャップアップ日は寄り後に垂れる）
ev_days = set(E.day)
d["gap"] = (d.Open / d.prev - 1) * 100
d["oc"] = (d.Close / d.Open - 1) * 100
o = d[(d.gap > 2) & (~d.index.strftime("%y/%m/%d").isin(ev_days))].dropna()
print(f"比較: 発表日以外のギャップ+2%超 n={len(o)}  寄り→引け中央値={o.oc.median():+.2f}%  >0: {(o.oc > 0).mean():.0%}")
o35 = o[o.gap > 3.5]
print(f"比較: 発表日以外のギャップ+3.5%超 n={len(o35)}  寄り→引け中央値={o35.oc.median():+.2f}%  >0: {(o35.oc > 0).mean():.0%}")
