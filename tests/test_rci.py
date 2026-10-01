"""RCI（バッチ / intrabar）の黄金値テスト。"""
import math

import numpy as np

from scalplab.indicators.rci import IntrabarRci, rci_series


def test_monotonic_extremes():
    # 単調上昇 → +100, 単調下降 → -100
    assert rci_series([1, 2, 3, 4, 5], 5)[-1] == 100.0
    assert rci_series([5, 4, 3, 2, 1], 5)[-1] == -100.0


def test_known_mixed_value():
    # [1,3,2,5,4] は手計算で Σd²=4 → RCI=(1-6*4/120)*100=80
    assert rci_series([1, 3, 2, 5, 4], 5)[-1] == 80.0


def test_ties_use_average_rank():
    # [1,2,2,3,4] は同値2つが平均順位3.5 → Σd²=0.5 → RCI=97.5
    assert rci_series([1, 2, 2, 3, 4], 5)[-1] == 97.5


def test_leading_nan_and_length():
    out = rci_series([1, 2, 3, 4, 5, 6], 5)
    assert len(out) == 6
    assert np.all(np.isnan(out[:4]))
    assert np.isfinite(out[4]) and np.isfinite(out[5])


def test_short_series_all_nan():
    out = rci_series([1, 2, 3], 5)
    assert np.all(np.isnan(out))


def test_intrabar_matches_batch_replacement():
    # confirm [1,3,2,5] のあと update_tick(4) は [1,3,2,5,4] の RCI(=80) に一致
    r = IntrabarRci(5)
    for x in [1, 3, 2, 5]:
        r.confirm_bar(x)
    assert r.update_tick(4) == 80.0

    # 任意の確定足列 + 形成価格 p が rci_series(append)[-1] と厳密一致すること
    rng = np.random.default_rng(0)
    closes = list(rng.normal(100, 1, size=12))
    period = 7
    r2 = IntrabarRci(period)
    for c in closes:
        r2.confirm_bar(c)
    for p in [99.0, 100.0, 101.5, 100.2]:
        got = r2.update_tick(p)
        expected = rci_series(np.array(closes + [p]), period)[-1]
        assert math.isclose(got, expected, rel_tol=1e-12, abs_tol=1e-9)


def test_intrabar_warmup_returns_nan():
    r = IntrabarRci(5)
    r.confirm_bar(1)
    r.confirm_bar(2)  # 確定足 2 本（period-1=4 未満）
    assert math.isnan(r.update_tick(3))
