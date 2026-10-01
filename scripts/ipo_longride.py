"""IPO「低迷→出来高急増」後を長く持ったら何倍化を取れるか（訂正仮説）。

前回検証は保有 1〜20 日で全構成マイナスだった。今回は
(1) 保有 20/40/60/120 日の固定ホールド
(2) トレーリングストップ 20%/30%（高値から下がるまで持ち続ける＝何倍化を逃がさない出口）
(3) トリガーに「20日高値ブレイク」を足した変種（「急に上がり始める」の直接表現）
を検証する。あわせてベースレート（トリガー無しで全 IPO を保有）と
「そもそも何倍化する IPO がどれだけあるか」を出し、トリガーの寄与を分離する。
エントリーは翌日寄り・往復コスト 10bp。学習=シグナル2024年以前/検証=2025年以降。
"""
import numpy as np
import pandas as pd

COST = 10.0
daily = pd.read_parquet("data/.cache/ipo-daily.parquet")
daily["date"] = pd.to_datetime(daily["date"])
daily = daily.sort_values(["code", "date"]).reset_index(drop=True)
first = daily.groupby("code")["date"].transform("min")
daily = daily[first >= pd.Timestamp("2018-12-01")].reset_index(drop=True)
SPLIT = pd.Timestamp("2025-01-01")

def trades_for(g, trigger, dry_frac, exit_mode):
    """1銘柄分のトレードを列挙。exit_mode: h20/h40/h60/h120 or trail20/trail30。"""
    v = pd.to_numeric(g["volume"], errors="coerce")
    c = pd.to_numeric(g["close"], errors="coerce")
    o = pd.to_numeric(g["open"], errors="coerce")
    n = len(g)
    if n < 30 or v.iloc[:5].median() <= 0:
        return []
    base = v.iloc[:5].median()
    dry = v.rolling(10, min_periods=10).median()
    pre = v.shift(1).rolling(10, min_periods=10).median()
    hi20 = c.shift(1).rolling(20, min_periods=20).max()
    age = np.arange(n)
    sig = (age >= 15) & (age <= 250) & (dry <= dry_frac * base) & (v >= 5 * pre)
    if trigger == "spike_bo":
        sig &= c > hi20
    sig = pd.Series(sig).fillna(False).to_numpy()
    out = []
    t_free = -1
    cn, on = c.to_numpy(), o.to_numpy()
    for t in np.flatnonzero(sig):
        if t <= t_free or t + 1 >= n:
            continue
        e = t + 1
        entry = on[e]
        if not np.isfinite(entry) or entry <= 0:
            continue
        censored = False
        if exit_mode.startswith("h"):
            hold = int(exit_mode[1:])
            x = min(e + hold - 1, n - 1)
            censored = (e + hold - 1) > (n - 1)
            exit_px, x_i = cn[x], x
        else:
            trail = 0.2 if exit_mode == "trail20" else 0.3
            peak = entry
            x_i, exit_px = None, None
            for j in range(e, min(e + 250, n)):
                peak = max(peak, cn[j])
                if cn[j] < peak * (1 - trail):
                    x_i = min(j + 1, n - 1)  # 翌日寄りで手仕舞い
                    exit_px = on[x_i] if np.isfinite(on[x_i]) and on[x_i] > 0 else cn[j]
                    break
            if x_i is None:
                x_i = min(e + 250, n) - 1
                exit_px, censored = cn[x_i], True
        if not np.isfinite(exit_px):
            continue
        t_free = x_i
        out.append({
            "code": g["code"].iloc[0], "signal_date": g["date"].iloc[t],
            "days_held": int(x_i - e + 1), "censored": censored,
            "net_bp": (exit_px / entry - 1) * 1e4 - COST,
        })
    return out

def summarize(rows, label):
    tr = pd.DataFrame(rows)
    if tr.empty:
        print(f"{label}: トレードなし")
        return
    tr["period"] = np.where(tr["signal_date"] < SPLIT, "学習", "検証")
    line = []
    for p in ("学習", "検証"):
        s = tr[tr["period"] == p]["net_bp"]
        if len(s) == 0:
            line.append(f"{p}: n=0")
            continue
        line.append(f"{p}: n={len(s):4d} 平均{s.mean():+7.0f} 中央{s.median():+7.0f} "
                    f"勝率{(s > 0).mean():.2f} 2倍化率{(s > 1e4).mean():.3f}")
    print(f"{label:34s} | " + " | ".join(line) + f" | 平均保有{tr['days_held'].mean():.0f}日 打切り{tr['censored'].mean():.2f}")

groups = [g.reset_index(drop=True) for _, g in daily.groupby("code", sort=False)]

print("=== トリガーあり（エントリー=シグナル翌日寄り、往復10bp控除） ===")
for trigger in ("spike", "spike_bo"):
    for dry_frac in (0.3, 0.5):
        for exit_mode in ("h20", "h40", "h60", "h120", "trail20", "trail30"):
            rows = []
            for g in groups:
                rows.extend(trades_for(g, trigger, dry_frac, exit_mode))
            summarize(rows, f"{trigger}/dry{dry_frac}/{exit_mode}")

print("\n=== ベースレート: トリガー無し・全IPOを20日目の翌日寄りで買う ===")
for exit_mode in ("h60", "h120", "trail20"):
    rows = []
    for g in groups:
        v = pd.to_numeric(g["volume"], errors="coerce")
        if len(g) < 30 or v.iloc[:5].median() <= 0:
            continue
        sig = np.zeros(len(g), dtype=bool)
        if len(g) > 21:
            sig[20] = True
        gg = g.copy()
        c = pd.to_numeric(gg["close"], errors="coerce")
        o = pd.to_numeric(gg["open"], errors="coerce")
        n = len(gg)
        t = 20
        e = t + 1
        entry = o.iloc[e] if e < n else np.nan
        if not np.isfinite(entry) or entry <= 0:
            continue
        if exit_mode.startswith("h"):
            hold = int(exit_mode[1:])
            x = min(e + hold - 1, n - 1)
            rows.append({"code": gg["code"].iloc[0], "signal_date": gg["date"].iloc[t],
                         "days_held": x - e + 1, "censored": (e + hold - 1) > (n - 1),
                         "net_bp": (c.iloc[x] / entry - 1) * 1e4 - COST})
        else:
            cn, on = c.to_numpy(), o.to_numpy()
            peak, x_i, exit_px, censored = entry, None, None, False
            for j in range(e, min(e + 250, n)):
                peak = max(peak, cn[j])
                if cn[j] < peak * 0.8:
                    x_i = min(j + 1, n - 1)
                    exit_px = on[x_i] if np.isfinite(on[x_i]) and on[x_i] > 0 else cn[j]
                    break
            if x_i is None:
                x_i = min(e + 250, n) - 1
                exit_px, censored = cn[x_i], True
            rows.append({"code": gg["code"].iloc[0], "signal_date": gg["date"].iloc[t],
                         "days_held": x_i - e + 1, "censored": censored, "net_bp": (exit_px / entry - 1) * 1e4 - COST})
    summarize(rows, f"全IPO/20日目買い/{exit_mode}")

print("\n=== そもそも何倍化はどれだけ起きるか（上場250日以内、15日目以降の安値→その後の高値） ===")
mult = []
for g in groups:
    c = pd.to_numeric(g["close"], errors="coerce").iloc[:250]
    if len(c) < 40:
        continue
    lo_i = c.iloc[15:].idxmin()
    lo = c.loc[lo_i]
    hi = c.loc[lo_i:].max()
    if np.isfinite(lo) and lo > 0:
        mult.append(hi / lo)
mult = pd.Series(mult)
print(f"対象 {len(mult)} 銘柄 / 安値からの最大倍率: 中央値 {mult.median():.2f}倍 / "
      f"2倍以上 {(mult >= 2).mean():.1%} / 3倍以上 {(mult >= 3).mean():.1%} / 5倍以上 {(mult >= 5).mean():.1%}")
