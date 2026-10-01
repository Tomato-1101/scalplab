"""ATR（Average True Range, Wilder 平滑）。

用途: 効率フィルタのボラ正規化 atr_pct = ATR(14) / close。ATR が未確定の間は
シグナルを無効化する（手法要件）。バッチ(atr_series)は逐次器(AtrState)へ委譲。
"""
from __future__ import annotations

import numpy as np


class AtrState:
    """確定足を 1 本ずつ与えて ATR を更新する逐次器。

    TR = max(high−low, |high−prev_close|, |low−prev_close|)。最初の period 本は
    TR の単純平均で初期化し、以降は Wilder 平滑 ATR=(ATR_prev*(period−1)+TR)/period。
    period 本に満たない間は NaN。
    """

    def __init__(self, period: int = 14):
        self.period = int(period)
        self._prev_close = None
        self._tr_sum = 0.0
        self._count = 0
        self.atr = float("nan")

    def confirm_bar(self, h: float, l: float, c: float) -> float:
        h = float(h)
        l = float(l)
        c = float(c)
        if self._prev_close is None:
            tr = h - l  # 初回は前終値が無いので高安幅
        else:
            tr = max(h - l, abs(h - self._prev_close), abs(l - self._prev_close))
        self._prev_close = c
        self._count += 1
        if self._count < self.period:
            self._tr_sum += tr
            self.atr = float("nan")
        elif self._count == self.period:
            self._tr_sum += tr
            self.atr = self._tr_sum / self.period
        else:
            self.atr = (self.atr * (self.period - 1) + tr) / self.period
        return self.atr


def atr_series(high, low, close, period: int = 14) -> np.ndarray:
    """確定足 OHLC のバッチ ATR。前方 period-1 本は NaN。"""
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    n = len(high)
    out = np.full(n, np.nan, dtype=np.float64)
    st = AtrState(period)
    for i in range(n):
        out[i] = st.confirm_bar(high[i], low[i], close[i])
    return out
