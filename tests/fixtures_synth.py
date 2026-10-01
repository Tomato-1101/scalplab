"""合成データ生成ユーティリティ（テスト用）。

data-contract.md のスキーマに厳密に準拠した ticks/quotes DataFrame と parquet を作る。
store.loader（別エージェント実装）が未提供でも e2e を回せるよう、契約準拠の MiniLoader も提供する。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

BASE_DATE = "2099-01-05"  # 実データと衝突しない架空営業日
_BASE_TS = pd.Timestamp(f"{BASE_DATE} 09:00:00")


def sec(t: float) -> pd.Timestamp:
    """基準時刻(09:00:00)からの経過秒を JST naive datetime に変換する。"""
    return _BASE_TS + pd.Timedelta(seconds=float(t))


def make_ticks(times, prices, volume=100, direction="NEUTRAL") -> pd.DataFrame:
    """契約 ticks スキーマの DataFrame を作る（time は datetime64[ns] naive）。"""
    times = pd.to_datetime(list(times))
    n = len(times)
    prices = np.asarray(prices, dtype=np.float64)
    return pd.DataFrame({
        "time": pd.Series(times).astype("datetime64[ns]"),
        "price": prices,
        "volume": np.full(n, int(volume), dtype=np.int64),
        "turnover": prices * float(volume),
        "direction": np.array([direction] * n, dtype=object),
        "seq": np.arange(n, dtype=np.int64),
        "tick_type": np.array(["AUTO_MATCH"] * n, dtype=object),
    })


def make_quotes(times, bids, asks, bid_vol=500, ask_vol=500) -> pd.DataFrame:
    """契約 quotes(L1) スキーマの DataFrame を作る。"""
    times = pd.to_datetime(list(times))
    n = len(times)
    return pd.DataFrame({
        "time": pd.Series(times).astype("datetime64[ns]"),
        "bid": np.asarray(bids, dtype=np.float64),
        "ask": np.asarray(asks, dtype=np.float64),
        "bid_vol": np.full(n, int(bid_vol), dtype=np.int64),
        "ask_vol": np.full(n, int(ask_vol), dtype=np.int64),
    })


def write_parquet(df: pd.DataFrame, root, kind: str, date: str, code: str) -> Path:
    """契約パス data/<kind>/<date>/<code>.parquet に保存する。"""
    d = Path(root) / "data" / kind / date
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{code}.parquet"
    df.to_parquet(p, index=False)
    return p


class MiniLoader:
    """契約準拠 parquet を読む最小 loader（store.loader 未提供時の DI 代替）。"""

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, kind, date, code):
        return self.root / "data" / kind / date / f"{code}.parquet"

    def load_ticks(self, date, code):
        p = self._path("ticks", date, code)
        if not p.exists():
            raise FileNotFoundError(p)
        return pd.read_parquet(p)

    def load_quotes(self, date, code):
        p = self._path("quotes", date, code)
        if not p.exists():
            raise FileNotFoundError(p)
        return pd.read_parquet(p)

    def load_bars1m(self, date, code):
        p = self._path("bars1m", date, code)
        if not p.exists():
            raise FileNotFoundError(p)
        return pd.read_parquet(p)

    def list_recorded(self, date, kind="ticks"):
        d = self.root / "data" / kind / date
        if not d.exists():
            return []
        return sorted(f.stem for f in d.glob("*.parquet"))


def _bar_ticks(minute_idx: int, close: float, half_range: float, offsets=(5, 20, 35, 50)):
    """1 分足を模す 4 ティック。high=close+hr, low=close-hr, 最後のティック=close。"""
    base = minute_idx * 60
    prices = [close - half_range, close + half_range, close - half_range / 2.0, close]
    times = [sec(base + off) for off in offsets]
    return times, prices


def long_signal_scenario():
    """ロングシグナルが1本発火し、+60s で約 +30bp 動く合成ティック列を返す。

    構成（既定 rci_period=9 / atr_period=14 を満たすようウォームアップ足を 15 本用意）:
      - 分0..5: 強めの上昇（99.00→99.75）で SAR をロング側に確定させ、価格下方に置く。
      - 分6..14: 密集した微上昇（100.00→100.08, 9本）＝直近確定足を狭いレンジに固める。
        （14 本を超えるので ATR(14) が確定する）
      - 分15: 形成足の価格を 99.99→100.15 へ掃引 → intrabar RCI が急伸(ΔRCI大)、価格レンジは
        0.16 と小さい（効率フィルタ通過）。SAR より十分上でロング可。
      - 掃引後、+0.005/秒(≈0.5bp/秒)で線形上昇させ、シグナル+60s で約 +30bp とする。
    戻り値: (ticks_df, signal_minute_start_ts, signal_minute_end_ts)
    """
    times = []
    prices = []
    sig_minute = 15

    # 分0..5: 強い上昇でトレンド確立（6本）
    for m, cl in enumerate([99.00, 99.15, 99.30, 99.45, 99.60, 99.75]):
        t, p = _bar_ticks(m, cl, half_range=0.05)
        times += t
        prices += p

    # 分6..14: 密集した微上昇（9本）→ ATR(14) 確定 + 直近8本を狭いレンジに固める
    clustered = [100.00 + 0.01 * k for k in range(9)]
    for k, cl in enumerate(clustered):
        m = 6 + k
        t, p = _bar_ticks(m, cl, half_range=0.01)
        times += t
        prices += p

    # 分15: 形成足の掃引（99.99→100.15）を 2 秒刻みで
    sweep_start = sig_minute * 60  # = 900s
    sweep_prices = np.linspace(99.99, 100.15, 16)  # t=900..930
    for j, pr in enumerate(sweep_prices):
        times.append(sec(sweep_start + 2 * j))
        prices.append(float(pr))

    # 掃引後: +0.005/秒 の線形上昇（120s 窓を満たすよう十分長く）
    ramp_start_t = sweep_start + 2 * (len(sweep_prices) - 1)  # =930s
    ramp_start_p = float(sweep_prices[-1])
    for s in range(2, 160, 2):  # 932s .. 1088s
        times.append(sec(ramp_start_t + s))
        prices.append(ramp_start_p + 0.005 * s)

    df = make_ticks(times, prices)
    sig_min_start = sec(sig_minute * 60)
    sig_min_end = sec((sig_minute + 1) * 60)
    return df, sig_min_start, sig_min_end
