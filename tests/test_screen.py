"""クロス銘柄スクリーニング（backtest.screen）の集計・RTH フィルタ検証（合成データ）。

- filter_rth が 09:31〜16:00（米国東部）だけ残すこと、
- iter_session_days がセッション日ごとに分割すること、
- expand_grid のデカルト積、
- screen_symbol が銘柄内グリッドを 1 行に集計し中央値/ベストを出すこと、
を合成バーで確認する（ネットワーク・キャッシュ非依存）。
"""
import numpy as np
import pandas as pd

from scalplab.backtest.screen import (
    APTITUDE_RANGE_BP,
    aptitude_badge,
    aptitude_metrics,
    expand_grid,
    filter_rth,
    iter_session_days,
    screen_run_bars1m,
    screen_symbol,
)

# バー版テストと同じロング急伸シナリオ（t=10 で発火する close 列）
LONG_CLOSES = [98.0, 98.4, 98.8, 99.2, 99.6, 100.0,
               100.02, 99.98, 100.03, 99.99, 100.04, 100.00, 100.30,
               100.42, 100.55, 100.60, 100.7]

FIXED = {"af_init": 0.02, "af_step": 0.02, "af_max": 0.2,
         "atr_period": 3, "cooldown_bars": 10, "floor": 0.1}


def _mk_bars(times, closes=None):
    n = len(times)
    if closes is None:
        closes = [100.0] * n
    closes = list(closes)
    o = [closes[i - 1] if i > 0 else closes[i] - 0.02 for i in range(n)]
    h = [max(o[i], closes[i]) + 0.03 for i in range(n)]
    l = [min(o[i], closes[i]) - 0.03 for i in range(n)]
    return pd.DataFrame({
        "time": pd.to_datetime(list(times)),
        "open": np.array(o, float), "high": np.array(h, float),
        "low": np.array(l, float), "close": np.array(closes, float),
        "volume": np.full(n, 100, np.int64), "turnover": np.array(closes, float) * 100.0,
        "source": "moomoo_hist",
    })


def _scenario_day(date_str, closes, start_hm="09:35"):
    """指定日の RTH 内にロング急伸シナリオを配置したバー（先頭に場外 08:00 を1本足す）。"""
    t0 = pd.Timestamp(f"{date_str} {start_hm}:00")
    times = [pd.Timestamp(f"{date_str} 08:00:00")] + [t0 + pd.Timedelta(minutes=i) for i in range(len(closes))]
    cl = [closes[0]] + list(closes)  # 場外バーの close はダミー（RTH フィルタで除外される）
    return _mk_bars(times, cl)


def test_filter_rth_keeps_only_regular_session():
    times = ["2099-01-05 07:00:00", "2099-01-05 09:30:00", "2099-01-05 09:31:00",
             "2099-01-05 12:00:00", "2099-01-05 15:59:00", "2099-01-05 16:00:00",
             "2099-01-05 18:00:00"]
    out = filter_rth(_mk_bars(times))
    kept = {str(t)[-8:] for t in out["time"]}
    assert kept == {"09:31:00", "12:00:00", "15:59:00"}  # 09:30 と 16:00 以降・場外は除外


def test_iter_session_days_splits_by_date():
    times = ["2099-01-05 10:00:00", "2099-01-05 10:01:00", "2099-01-06 10:00:00"]
    days = list(iter_session_days(_mk_bars(times)))
    assert len(days) == 2
    assert len(days[0]) == 2 and len(days[1]) == 1


def test_expand_grid_cartesian_product():
    grid = {"rci_period": [9, 14], "slope_threshold": [10, 20, 30], "eff_min": [1.0, 2.0, 4.0]}
    combos = expand_grid(grid)
    assert len(combos) == 18
    assert {"rci_period": 9, "slope_threshold": 10, "eff_min": 1.0} in combos


def test_screen_symbol_aggregates_grid_and_isolates_days():
    # 2 セッション日それぞれにロング急伸シナリオ（各日1本発火）を仕込む
    bars = pd.concat([
        _scenario_day("2099-01-05", LONG_CLOSES),
        _scenario_day("2099-01-06", LONG_CLOSES),
    ], ignore_index=True)

    grid = {"rci_period": [5], "slope_threshold": [15.0], "eff_min": [1.0]}
    combos = expand_grid(grid)
    row = screen_symbol("US.SYN", bars, combos, FIXED, cost_bp=3.0,
                        eval_bars=(1, 2, 3), min_signals=1, rth_only=True)

    assert row["code"] == "US.SYN"
    assert row["n_signals_total"] == 2  # 1 combo × 2 日 × 1 本
    assert row["n_long_total"] == 2 and row["n_short_total"] == 0
    assert row["best_rci_period"] == 5
    assert np.isfinite(row["median_exp1_net_bp"])
    assert np.isfinite(row["median_exp2_net_bp"])
    # 場外バー(08:00)は RTH フィルタで除外され、日跨ぎの汚染も無い（各日独立で 1 本ずつ）


def test_screen_symbol_no_signal_symbol():
    """ほぼ一定価格ではシグナルが出ず、集計は 0 本でも壊れない。"""
    times = [pd.Timestamp("2099-01-05 09:35:00") + pd.Timedelta(minutes=i) for i in range(30)]
    bars = _mk_bars(times, closes=[100.0] * 30)
    grid = {"rci_period": [5], "slope_threshold": [15.0], "eff_min": [1.0]}
    row = screen_symbol("US.FLAT", bars, expand_grid(grid), FIXED, cost_bp=3.0,
                        eval_bars=(1, 2), min_signals=1, rth_only=True)
    assert row["n_signals_total"] == 0
    assert np.isnan(row["median_exp1_net_bp"])


# --- 機能B: 銘柄適性指標・単一組ランキング ---

def test_aptitude_metrics_turnover_range_bars():
    """売買代金(億/日)・レンジ中央(bp)・bars/日を実測どおり計算する。"""
    # 2 日 × 各3本。turnover は 1e8（=1億）固定、レンジ (high-low)/close をコントロール。
    times = ([pd.Timestamp("2099-01-05 09:30:00") + pd.Timedelta(minutes=i) for i in range(3)]
             + [pd.Timestamp("2099-01-06 09:30:00") + pd.Timedelta(minutes=i) for i in range(3)])
    n = len(times)
    close = np.full(n, 100.0)
    bars = pd.DataFrame({
        "time": times,
        "open": close, "high": close + 0.5, "low": close - 0.5, "close": close,   # レンジ 1.0/100=100bp
        "volume": np.full(n, 10, np.int64), "turnover": np.full(n, 1e8, float),
        "source": "yfinance",
    })
    m = aptitude_metrics(bars)
    assert m["bars_per_day"] == 3.0                      # 6 本 / 2 日
    assert abs(m["avg_turnover_oku"] - 3.0) < 1e-9       # 6×1e8 / 2 日 / 1e8 = 3 億/日
    assert abs(m["range_med_bp"] - 100.0) < 1e-6         # (1.0/100)*1e4 = 100bp


def test_aptitude_metrics_empty():
    m = aptitude_metrics(pd.DataFrame(columns=["time", "high", "low", "close", "volume", "turnover"]))
    assert m["bars_per_day"] == 0.0
    assert np.isnan(m["avg_turnover_oku"])


def test_aptitude_badge_thresholds():
    """適性バッジ（レンジ中央値 bp）: ≥25→◎ / ≥15→○ / <15→× / 判定不能→空。しきい値境界を固定する。"""
    lo, hi = APTITUDE_RANGE_BP
    assert (lo, hi) == (15.0, 25.0)                 # 分析根拠に基づく定数（増やさない）
    assert aptitude_badge(30.0) == "◎"
    assert aptitude_badge(hi) == "◎"                # 25 は◎（境界は下限含む）
    assert aptitude_badge(20.0) == "○"
    assert aptitude_badge(lo) == "○"                # 15 は○
    assert aptitude_badge(14.9) == "×"
    assert aptitude_badge(0.0) == "×"
    assert aptitude_badge(None) == ""               # データ無し＝判定不能
    assert aptitude_badge(float("nan")) == ""


def test_screen_run_bars1m_has_aptitude_badge(tmp_path, monkeypatch):
    """screen_run_bars1m の各行に適性バッジ列 aptitude が付く（レンジ中央値と整合する）。"""
    from scalplab.store import loader
    monkeypatch.setattr(loader, "DATA_DIR", tmp_path)
    day = _scenario_day("2099-01-05", LONG_CLOSES, start_hm="09:35")
    _write_bars1m(tmp_path, "2099-01-05", "JP.TEST", day)
    params = {"rci_period": 5, "slope_threshold": 15.0, "eff_min": 1.0,
              "prev_diff_min": 0.0, "rci_sign_gate": 0, "spike_depth_min": 0.0, "spike_turn_min": 0.0,
              "cooldown_bars": 3, "af_init": 0.02, "af_step": 0.02, "af_max": 0.2}
    ranking, _ = screen_run_bars1m(["JP.TEST"], ["2099-01-05"], params, cost_bp=3.0)
    assert "aptitude" in ranking.columns
    row = ranking.iloc[0]
    assert row["aptitude"] == aptitude_badge(row["range_med_bp"])   # 列とヘルパが整合


def _write_bars1m(root, date, code, bars):
    d = root / "bars1m" / date
    d.mkdir(parents=True, exist_ok=True)
    bars.to_parquet(d / f"{code}.parquet", index=False)


def test_screen_run_bars1m_columns_and_aptitude(tmp_path, monkeypatch):
    """単一組ランキングが ranking.csv 互換列＋適性列＋Tick回数を返す。"""
    from scalplab.store import loader
    monkeypatch.setattr(loader, "DATA_DIR", tmp_path)
    day = _scenario_day("2099-01-05", LONG_CLOSES, start_hm="09:35")
    _write_bars1m(tmp_path, "2099-01-05", "JP.TEST", day)

    params = {"rci_period": 5, "slope_threshold": 15.0, "eff_min": 1.0,
              "prev_diff_min": 0.0, "rci_sign_gate": 0, "spike_depth_min": 0.0, "spike_turn_min": 0.0,
              "cooldown_bars": 3, "af_init": 0.02, "af_step": 0.02, "af_max": 0.2}
    ranking, n_missing = screen_run_bars1m(
        ["JP.TEST"], ["2099-01-05"], params, cost_bp=3.0, eval_bars=(1, 2, 3),
        tick_counts={"JP.TEST": 12345})
    assert n_missing == 0
    assert len(ranking) == 1
    row = ranking.iloc[0]
    assert row["code"] == "JP.TEST"
    # 適性列・Tick回数が入っている
    for col in ("avg_turnover_oku", "range_med_bp", "bars_per_day", "tick_count"):
        assert col in ranking.columns
    assert row["tick_count"] == 12345
    assert row["bars_per_day"] > 0


def test_screen_run_bars1m_missing_code(tmp_path, monkeypatch):
    """録画の無い銘柄は n_missing に数え、行に出さない。"""
    from scalplab.store import loader
    monkeypatch.setattr(loader, "DATA_DIR", tmp_path)
    params = {"rci_period": 5, "slope_threshold": 15.0, "eff_min": 1.0,
              "prev_diff_min": 0.0, "rci_sign_gate": 0, "spike_depth_min": 0.0, "spike_turn_min": 0.0,
              "cooldown_bars": 3, "af_init": 0.02, "af_step": 0.02, "af_max": 0.2}
    ranking, n_missing = screen_run_bars1m(["JP.NOPE"], ["2099-01-05"], params, cost_bp=3.0)
    assert n_missing == 1 and len(ranking) == 0
