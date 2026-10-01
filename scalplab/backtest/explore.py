"""手法探索の共通基盤（`python -m scalplab.backtest.explore`）。

既存の rci_impulse_bar / walkforward / autotune には一切触らず、独立に「往復コストを払っても
週次・月次で安定して勝てる条件」を総当たりで探すための土台。2026-08-10 の要件:

  - **コストは往復で引く**。既存 CSV の net は片道 3bp しか引いておらず、往復 6bp にすると
    チャンピオンのエッジがちょうど消えた。ここでは gross を持ち回り、評価時に往復コストを引く。
  - **保有時間を決め打ちしない**。1〜20 バー（分）の複数ホライズンを同時に測る。
  - **週次・月次の安定性で選ぶ**。合計 bp だけを見ると単一日の当たりで勝ってしまう。

方針は「条件の総当たり」ではなく **特徴量 → 将来リターンの統計** から入る。閾値グリッドを
先に切ると組み合わせ爆発する一方、分位別の期待値を見れば「そもそもエッジのある軸」が
少数に絞れるため、探索効率が桁で違う。

セッション（前場/後場）を跨ぐリターンは構造的に発生させない（昼休み・オーバーナイトの
価格跳びを利益と誤認しないため）。全特徴量は当該バーまでの情報のみで計算する（先読み禁止）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from scalplab.backtest.screen import filter_jp_session, iter_jp_sessions
from scalplab.indicators.atr import atr_series
from scalplab.indicators.parabolic import sar_series
from scalplab.indicators.rci import rci_series
from scalplab.store import loader as store_loader

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BARS1M_DIR = PROJECT_ROOT / "data" / "bars1m"

# 将来リターンのホライズン（バー=分）。1〜3 が既存スキャル、5〜20 は「往復コストを超える
# 値幅が取れるのか」を確かめるための拡張。
HORIZONS = (1, 2, 3, 5, 10, 20, 30, 60)

# ウォームアップ（各セッション先頭の N バーは指標未確定として捨てる）
WARMUP = 20


# ---------------- データロード ----------------

def available_dates() -> list[str]:
    """JP の bars1m がある営業日を昇順で返す。"""
    if not BARS1M_DIR.exists():
        return []
    return sorted(p.name for p in BARS1M_DIR.iterdir()
                  if p.is_dir() and any(p.glob("JP.*.parquet")))


def available_codes(dates: list[str], min_ratio: float = 0.9) -> list[str]:
    """指定日のうち min_ratio 以上の日に存在する JP 銘柄を返す（歯抜けを除く）。"""
    cnt: dict[str, int] = {}
    for d in dates:
        for p in (BARS1M_DIR / d).glob("JP.*.parquet"):
            cnt[p.stem] = cnt.get(p.stem, 0) + 1
    need = len(dates) * min_ratio
    return sorted(c for c, n in cnt.items() if n >= need)


# ---------------- 特徴量 ----------------

def _rsi(close: np.ndarray, period: int) -> np.ndarray:
    """Wilder の RSI。値は 0〜100、未確定は NaN。"""
    d = np.diff(close, prepend=close[0])
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = pd.Series(up).ewm(alpha=1 / period, adjust=False).mean().to_numpy()
    ad = pd.Series(dn).ewm(alpha=1 / period, adjust=False).mean().to_numpy()
    rs = np.divide(au, ad, out=np.full_like(au, np.inf), where=ad > 0)
    out = 100 - 100 / (1 + rs)
    out[:period] = np.nan
    return out


def _zdev(x: np.ndarray, window: int) -> np.ndarray:
    """x の移動平均からの乖離を移動標準偏差で割った z（当該バーを含む）。"""
    s = pd.Series(x)
    m = s.rolling(window).mean()
    sd = s.rolling(window).std(ddof=0)
    return ((s - m) / sd.replace(0, np.nan)).to_numpy()


def _ma_dev_bp(close: np.ndarray, window: int) -> np.ndarray:
    """終値の移動平均からの乖離（bp）。"""
    m = pd.Series(close).rolling(window).mean().to_numpy()
    return (close - m) / m * 1e4


def session_features(bars: pd.DataFrame) -> pd.DataFrame:
    """1 セッション（前場 or 後場）の DataFrame に特徴量と将来リターンを付ける。

    全特徴量は当該バーの終値までで計算する。将来リターンだけが未来を見るが、これは
    エントリー後の評価なので先読みではない（セッション末尾は NaN で切れる）。
    """
    o = bars["open"].to_numpy(float)
    h = bars["high"].to_numpy(float)
    lo = bars["low"].to_numpy(float)
    c = bars["close"].to_numpy(float)
    v = bars["volume"].to_numpy(float)
    n = len(c)
    f = pd.DataFrame(index=bars.index)

    # --- 価格・トレンド ---
    for k in (1, 3, 5, 10):
        f[f"ret_{k}"] = pd.Series(c).pct_change(k).to_numpy() * 1e4
    for w in (5, 20, 60):
        f[f"ma_dev_{w}"] = _ma_dev_bp(c, w)
    for w in (20, 60):
        f[f"bb_z_{w}"] = _zdev(c, w)
    # 当日 VWAP（セッション内累積）からの乖離
    tp = (h + lo + c) / 3
    cum_v = np.cumsum(v)
    vwap = np.divide(np.cumsum(tp * v), cum_v, out=tp.copy(), where=cum_v > 0)
    f["vwap_dev"] = (c - vwap) / vwap * 1e4
    # 直近 20 バーの高安レンジ内での位置（0=安値 1=高値）
    hh = pd.Series(h).rolling(20).max().to_numpy()
    ll = pd.Series(lo).rolling(20).min().to_numpy()
    rng = hh - ll
    f["range_pos_20"] = np.divide(c - ll, rng, out=np.full(n, np.nan), where=rng > 0)
    # 移動平均の傾き（20 バー平均の 5 バー変化 bp）
    m20 = pd.Series(c).rolling(20).mean()
    f["ma_slope_20"] = (m20.diff(5) / m20 * 1e4).to_numpy()
    sar, _trend = sar_series(h, lo, c)
    f["sar_side"] = np.where(c > sar, 1.0, -1.0)

    # --- モメンタム ---
    for p in (7, 14):
        f[f"rsi_{p}"] = _rsi(c, p)
    for p in (9, 14, 26):
        f[f"rci_{p}"] = rci_series(c, p)
    f["rci_9_d3"] = pd.Series(f["rci_9"]).diff(3).to_numpy()
    f["rci_9_d1"] = pd.Series(f["rci_9"]).diff(1).to_numpy()
    ema12 = pd.Series(c).ewm(span=12, adjust=False).mean()
    ema26 = pd.Series(c).ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    f["macd_hist"] = ((macd - macd.ewm(span=9, adjust=False).mean()) / c * 1e4).to_numpy()
    lo14 = pd.Series(lo).rolling(14).min().to_numpy()
    hi14 = pd.Series(h).rolling(14).max().to_numpy()
    d14 = hi14 - lo14
    f["stoch_k"] = np.divide(c - lo14, d14, out=np.full(n, np.nan), where=d14 > 0) * 100

    # --- ボラティリティ ---
    atr = atr_series(h, lo, c, 14)
    f["atr_pct"] = atr / c * 1e4
    ret1 = pd.Series(c).pct_change()
    sd20 = ret1.rolling(20).std(ddof=0)
    f["sigma_20"] = (sd20 * 1e4).to_numpy()
    f["sigma_ratio"] = (sd20 / ret1.rolling(60).std(ddof=0)).to_numpy()
    f["bar_range"] = (h - lo) / c * 1e4
    # 直前 20 バーのσ（当該バーを除く）に対する当バーリターンの倍率＝急変度
    f["ret1_sigma"] = (ret1 / sd20.shift(1)).to_numpy()

    # --- 出来高 ---
    vs = pd.Series(v)
    f["vol_ratio_20"] = (v / vs.rolling(20).mean().replace(0, np.nan)).to_numpy()
    f["vol_z_20"] = _zdev(v, 20)

    # --- 時間・形状 ---
    f["bar_idx"] = np.arange(n, dtype=float)
    f["bars_left"] = (n - 1 - np.arange(n)).astype(float)
    body = np.abs(c - o)
    total = np.where((h - lo) > 0, h - lo, np.nan)
    f["body_frac"] = body / total
    f["upper_wick"] = (h - np.maximum(o, c)) / total
    f["lower_wick"] = (np.minimum(o, c) - lo) / total

    # --- 将来リターン（bp、ロング方向。セッション末尾は NaN） ---
    for k in HORIZONS:
        fwd = np.full(n, np.nan)
        if n > k:
            fwd[:-k] = (c[k:] - c[:-k]) / c[:-k] * 1e4
        f[f"fwd_{k}"] = fwd

    f["close"] = c
    return f


def build_panel(dates: list[str], codes: list[str], *, verbose: bool = True) -> pd.DataFrame:
    """全 (日付 × 銘柄 × セッション) の特徴量パネルを 1 枚の DataFrame にして返す。"""
    frames = []
    for i, d in enumerate(dates):
        for code in codes:
            try:
                bars = store_loader.load_bars1m(d, code)
            except FileNotFoundError:
                continue
            sess = filter_jp_session(bars)
            for si, day in enumerate(iter_jp_sessions(sess)):
                if len(day) < WARMUP + max(HORIZONS) + 1:
                    continue
                f = session_features(day.reset_index(drop=True))
                f = f.iloc[WARMUP:].copy()
                f["date"] = d
                f["code"] = code
                f["sess"] = si
                f["time"] = day["time"].to_numpy()[WARMUP:]
                frames.append(f)
        if verbose and (i + 1) % 5 == 0:
            print(f"  {i+1}/{len(dates)} 日 読み込み済み（{sum(len(x) for x in frames):,} バー）", flush=True)
    if not frames:
        return pd.DataFrame()
    panel = pd.concat(frames, ignore_index=True)
    panel["date"] = pd.to_datetime(panel["date"])
    return panel


# ---------------- 評価 ----------------

def evaluate(panel: pd.DataFrame, mask: np.ndarray, direction: np.ndarray,
             horizon: int, cost_bp: float) -> dict:
    """mask で選んだバーに direction(+1/-1) で入った場合の成績を返す。

    cost_bp は**往復**コスト（gross からそのまま引く）。週次・月次の安定性も同時に測る。
    """
    sub = panel.loc[mask]
    if len(sub) == 0:
        return {"n": 0}
    d = direction[mask] if isinstance(direction, np.ndarray) else direction
    net = sub[f"fwd_{horizon}"].to_numpy() * d - cost_bp
    ok = np.isfinite(net)
    net = net[ok]
    if len(net) == 0:
        return {"n": 0}
    dates = sub["date"].to_numpy()[ok]
    df = pd.DataFrame({"date": dates, "net": net})
    wk = df.groupby(pd.to_datetime(df["date"]).dt.to_period("W-FRI"))["net"].sum()
    mo = df.groupby(pd.to_datetime(df["date"]).dt.to_period("M"))["net"].sum()
    dy = df.groupby("date")["net"].sum()
    return {
        "n": int(len(net)),
        "mean_bp": float(net.mean()),
        "median_bp": float(np.median(net)),
        "total_bp": float(net.sum()),
        "win": float((net > 0).mean()),
        "weeks": int(len(wk)), "weeks_pos": int((wk > 0).sum()),
        "months": int(len(mo)), "months_pos": int((mo > 0).sum()),
        "days": int(len(dy)), "days_pos": int((dy > 0).sum()),
        "week_min_bp": float(wk.min()), "day_min_bp": float(dy.min()),
        "sharpe_day": float(dy.mean() / dy.std(ddof=0)) if len(dy) > 1 and dy.std(ddof=0) > 0 else float("nan"),
    }
