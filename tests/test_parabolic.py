"""Parabolic SAR と ATR の黄金値・逐次/バッチ一致テスト。"""
import numpy as np

from scalplab.indicators.atr import AtrState, atr_series
from scalplab.indicators.parabolic import SarState, sar_series

# 上昇→反転を含む既知系列（手計算で SAR を追った）
H = [10.0, 10.5, 11.0, 11.5, 10.0, 9.5]
L = [9.0, 9.5, 10.0, 10.5, 9.0, 8.5]
C = [9.5, 10.4, 10.9, 11.4, 9.1, 8.8]


def test_sar_known_values():
    sar, trend = sar_series(H, L, C)
    # 手計算: 初期上昇(SAR=low[0]=9.0), 直近安値でクランプ, bar3 で 9.08, bar4 で安値割れ反転→SAR=EP=11.5
    expected_sar = np.array([np.nan, 9.0, 9.0, 9.08, 11.5, 11.5])
    np.testing.assert_allclose(sar, expected_sar, rtol=1e-9, equal_nan=True)
    assert list(trend) == [0, 1, 1, 1, -1, -1]


def test_sar_state_matches_batch():
    sar, trend = sar_series(H, L, C)
    st = SarState()
    seq_sar, seq_trend = [], []
    for h, l, c in zip(H, L, C):
        s, t = st.confirm_bar(h, l, c)
        seq_sar.append(s)
        seq_trend.append(t)
    np.testing.assert_allclose(np.array(seq_sar), sar, rtol=1e-9, equal_nan=True)
    assert seq_trend == list(trend)


def test_atr_wilder():
    h = [10.0, 10.5, 11.0, 12.0]
    l = [9.0, 9.5, 10.0, 11.0]
    c = [9.5, 10.4, 10.9, 11.8]
    out = atr_series(h, l, c, 3)
    # 前2本 NaN, 3本目 = mean(TR)=1.0, 4本目 Wilder = (1*2 + 1.1)/3 = 1.03333...
    assert np.all(np.isnan(out[:2]))
    np.testing.assert_allclose(out[2], 1.0, rtol=1e-9)
    np.testing.assert_allclose(out[3], (1.0 * 2 + 1.1) / 3, rtol=1e-9)


def test_atr_state_matches_batch():
    h = [10.0, 10.5, 11.0, 12.0, 11.5]
    l = [9.0, 9.5, 10.0, 11.0, 10.8]
    c = [9.5, 10.4, 10.9, 11.8, 11.0]
    out = atr_series(h, l, c, 3)
    st = AtrState(3)
    seq = [st.confirm_bar(hi, lo, cl) for hi, lo, cl in zip(h, l, c)]
    np.testing.assert_allclose(np.array(seq), out, rtol=1e-9, equal_nan=True)
