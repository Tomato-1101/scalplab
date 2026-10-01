"""Parabolic SAR（Wilder 標準）。

本手法の「方向の絶対条件」（価格が SAR より上ならロングのみ・下ならショートのみ）を担う。
SAR は確定 1分足でのみ更新する。足内では前足確定値を据え置く（チャートの見た目と同じ）ので、
intrabar では最後に confirm_bar した値をそのまま方向判定に使えばよい。

バッチ(sar_series)は逐次器(SarState)へ委譲して実装し、両者の一致を構造的に保証する。
"""
from __future__ import annotations

import numpy as np


class SarState:
    """確定足を 1 本ずつ与えて SAR を更新する逐次器。

    Wilder のアルゴリズム:
      1. SAR_today = SAR_prev + AF*(EP − SAR_prev)
      2. クランプ: 上昇中は直近2本の安値を超えない / 下降中は直近2本の高値を割らない
      3. 反転判定: 上昇中に安値が SAR を割れば下降へ（SAR=直前EP, EP=安値, AF=af_init）
      4. 非反転なら EP 更新時に AF を af_step 刻みで af_max まで加速
    最初の1本は NaN、2本目で初期トレンドを終値比較で決めて最初の SAR を出力する。
    """

    def __init__(self, af_init: float = 0.02, af_step: float = 0.02, af_max: float = 0.2):
        self.af_init = float(af_init)
        self.af_step = float(af_step)
        self.af_max = float(af_max)
        self._n = 0
        self._prev_high = self._prev_low = self._prev_close = None
        self._prev2_high = self._prev2_low = None
        self._ep = None
        self._af = self.af_init
        self._sar_run = None  # 直近 SAR（次バー計算の SAR_prev）
        self.sar = float("nan")
        self.trend = 0

    def confirm_bar(self, h: float, l: float, c: float):
        """確定足 (high, low, close) を投入し (sar, trend) を返す。"""
        h = float(h)
        l = float(l)
        c = float(c)
        self._n += 1

        if self._n == 1:
            # 1本目: 初期化材料を溜めるだけ
            self._prev_high, self._prev_low, self._prev_close = h, l, c
            self.sar, self.trend = float("nan"), 0
            return self.sar, self.trend

        if self._n == 2:
            up = c >= self._prev_close
            if up:
                self._ep = h
                self._sar_run = self._prev_low
                self.trend = 1
            else:
                self._ep = l
                self._sar_run = self._prev_high
                self.trend = -1
            self._af = self.af_init
            self.sar = self._sar_run
            self._roll(h, l, c)
            return self.sar, self.trend

        # 3本目以降: 通常の漸化式
        sar_calc = self._sar_run + self._af * (self._ep - self._sar_run)
        if self.trend == 1:
            sar_calc = min(sar_calc, self._prev_low, self._prev2_low)
            if l < sar_calc:  # 反転 → 下降
                self.trend = -1
                sar_calc = self._ep
                self._ep = l
                self._af = self.af_init
            elif h > self._ep:
                self._ep = h
                self._af = min(self._af + self.af_step, self.af_max)
        else:
            sar_calc = max(sar_calc, self._prev_high, self._prev2_high)
            if h > sar_calc:  # 反転 → 上昇
                self.trend = 1
                sar_calc = self._ep
                self._ep = h
                self._af = self.af_init
            elif l < self._ep:
                self._ep = l
                self._af = min(self._af + self.af_step, self.af_max)

        self._sar_run = sar_calc
        self.sar = sar_calc
        self._roll(h, l, c)
        return self.sar, self.trend

    def _roll(self, h, l, c):
        """前バー・前々バーの高安を1本ずらす。"""
        self._prev2_high, self._prev2_low = self._prev_high, self._prev_low
        self._prev_high, self._prev_low, self._prev_close = h, l, c


def sar_series(high, low, close, af_init=0.02, af_step=0.02, af_max=0.2):
    """確定足 OHLC のバッチ SAR。戻り値 (sar[np.nan前置], trend[+1/-1/0])。"""
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    n = len(high)
    sar = np.full(n, np.nan, dtype=np.float64)
    trend = np.zeros(n, dtype=np.int8)
    st = SarState(af_init, af_step, af_max)
    for i in range(n):
        s, t = st.confirm_bar(high[i], low[i], close[i])
        sar[i] = s
        trend[i] = t
    return sar, trend
