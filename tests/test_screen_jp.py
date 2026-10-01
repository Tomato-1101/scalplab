"""日本株スクリーニング（backtest.screen の bars1m/JP モード）の検証（合成データ）。

- filter_jp_session が JP 立会（前場 09:00〜11:30 / 後場 12:30〜15:30）だけ残すこと、
- iter_jp_sessions が (立会日, 前場/後場) ごとに分割すること、
- screen_symbol(session='jp') が前場引けバー発火＝昼休み跨ぎエントリーを不成立として除外すること、
- bars1m の契約レイアウト parquet を日付横断に連結して読めること（recent_bars1m_dates / auto_symbols_bars1m /
  load_bars1m_history / _screen_worker）、
を検証する（ネットワーク非依存。loader.DATA_DIR を tmp_path に差し替えて契約 parquet を自作）。
"""
import numpy as np
import pandas as pd

from scalplab.backtest.screen import (
    auto_symbols_bars1m,
    filter_jp_session,
    iter_jp_sessions,
    load_bars1m_history,
    recent_bars1m_dates,
    screen_symbol,
    _screen_worker,
)
from scalplab.store import loader, writer
from scalplab.strategy.rci_impulse_bar import bar_signals

# バー版テストと同じロング急伸シナリオ（先頭から数えて 10 本目付近で発火する close 列）
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
        "source": "yfinance",
    })


def _times(date_str, start_hm, n):
    """指定日・指定開始時刻から 1 分刻みで n 本の時刻を作る。"""
    t0 = pd.Timestamp(f"{date_str} {start_hm}:00")
    return [t0 + pd.Timedelta(minutes=i) for i in range(n)]


def test_filter_jp_session_keeps_only_trading_hours():
    times = ["2099-01-05 08:00:00", "2099-01-05 09:00:00", "2099-01-05 11:30:00",
             "2099-01-05 11:45:00", "2099-01-05 12:00:00", "2099-01-05 12:30:00",
             "2099-01-05 15:30:00", "2099-01-05 15:45:00"]
    out = filter_jp_session(_mk_bars(times))
    kept = {str(t)[-8:] for t in out["time"]}
    # 場外(08:00/15:45)と昼休み(11:45/12:00)は除外。前場引け11:30・大引け15:30は残す。
    assert kept == {"09:00:00", "11:30:00", "12:30:00", "15:30:00"}


def test_iter_jp_sessions_splits_morning_afternoon_and_days():
    # 2 立会日 × (前場3本 + 後場2本)。昼休みバーも混ぜてフィルタ→分割の合成を確認。
    frames = []
    for d in ("2099-01-05", "2099-01-06"):
        frames.append(_mk_bars(_times(d, "09:00", 3)))         # 前場
        frames.append(_mk_bars([f"{d} 12:00:00"]))             # 昼休み（除外される）
        frames.append(_mk_bars(_times(d, "12:30", 2)))         # 後場
    bars = pd.concat(frames, ignore_index=True)

    segs = list(iter_jp_sessions(filter_jp_session(bars)))
    assert len(segs) == 4  # 2 日 × 前場/後場
    # 各セグメントは単一日・単一半場に収まる（前場は <=11:30、後場は >=12:30）
    for g in segs:
        mins = pd.to_datetime(g["time"]).dt.hour * 60 + pd.to_datetime(g["time"]).dt.minute
        assert len(pd.to_datetime(g["time"]).dt.date.unique()) == 1
        assert (mins <= 11 * 60 + 30).all() or (mins >= 12 * 60 + 30).all()
    # 前場は3本・後場は2本
    sizes = sorted(len(g) for g in segs)
    assert sizes == [2, 2, 3, 3]


def test_screen_symbol_jp_signal_fires_inside_morning():
    """前場の内側で発火するシナリオは通常どおりエントリーが立つ（対照群）。"""
    # 前場 09:00 から LONG_CLOSES（17本）＋余裕の後続を並べる（発火本は前場引けではない）
    bars = _mk_bars(_times("2099-01-05", "09:00", len(LONG_CLOSES)), LONG_CLOSES)
    grid = [{"rci_period": 5, "slope_threshold": 15.0, "eff_min": 1.0}]
    row = screen_symbol("JP.9999", bars, grid, FIXED, cost_bp=3.0,
                        eval_bars=(1, 2, 3), min_signals=1, session="jp")
    assert row["n_signals_total"] >= 1
    assert row["n_long_total"] >= 1


def test_screen_symbol_jp_excludes_lunch_straddle():
    """発火バーを前場引け(11:30)に置くと、次バー始値は後場始値＝昼休み跨ぎとなりエントリー不成立。"""
    params = {"rci_period": 5, "slope_threshold": 15.0, "eff_min": 1.0, **FIXED}
    # まず 1 セッション連続で走らせ、最初の発火バーがシナリオ内の何本目かを特定する（発火位置に依存しない）。
    times = _times("2099-01-05", "09:00", len(LONG_CLOSES))
    entries = bar_signals(_mk_bars(times, LONG_CLOSES), params, code="JP.9999",
                          cost_bp=3.0, eval_bars=(1, 2, 3))
    assert len(entries) >= 1
    fire_idx = times.index(pd.Timestamp(entries["time"].iloc[0]))

    # 発火バーが前場の最終バー(11:30)になるよう配置。close 前置は同一なので発火条件も同一に保たれ、
    # かつ最終バーは「次バーが無い」ため screen_symbol(session='jp') ではエントリーが立たない。
    m = fire_idx + 1  # 前場本数（末尾＝発火バー）
    start = pd.Timestamp("2099-01-05 11:30:00") - pd.Timedelta(minutes=fire_idx)
    morning = _mk_bars([start + pd.Timedelta(minutes=i) for i in range(m)], LONG_CLOSES[:m])
    afternoon = _mk_bars(_times("2099-01-05", "12:30", len(LONG_CLOSES) - m), LONG_CLOSES[m:])
    bars = pd.concat([morning, afternoon], ignore_index=True)

    grid = [{"rci_period": 5, "slope_threshold": 15.0, "eff_min": 1.0}]
    row = screen_symbol("JP.9999", bars, grid, FIXED, cost_bp=3.0,
                        eval_bars=(1, 2, 3), min_signals=1, session="jp")
    # 前場引けバーは「次バーが無い」ため発火してもエントリーが立たない＝昼休み跨ぎ除外。
    assert row["n_signals_total"] == 0


def _write_bars1m(root, date, code, bars):
    """契約レイアウト data/bars1m/DATE/CODE.parquet に合成バーを書く。"""
    path = loader.bars1m_path(date, code, root=root / "data")
    writer.write_parquet(writer.enforce_bars1m(bars), path)


def test_bars1m_source_multiday_via_contract(tmp_path, monkeypatch):
    """契約 parquet を tmp に自作し、日付横断連結・auto 列挙・直近N日選択・worker を通しで検証。"""
    data_root = tmp_path / "data"
    monkeypatch.setattr(loader, "DATA_DIR", data_root)

    # 2 立会日ぶんの前場/後場バーを 2 銘柄 + US 1 銘柄で書く（US は auto(jp) で除外される想定）
    for d in ("2099-01-05", "2099-01-06"):
        for code in ("JP.7203", "JP.9432"):
            day = pd.concat([
                _mk_bars(_times(d, "09:00", 20), LONG_CLOSES + [100.7] * 3),
                _mk_bars(_times(d, "12:30", 5)),
            ], ignore_index=True)
            _write_bars1m(tmp_path, d, code, day)
        _write_bars1m(tmp_path, d, "US.AAPL", _mk_bars(_times(d, "09:00", 5)))

    # 直近 N 日選択（新しい順に最大 N 件を昇順で返す）
    assert recent_bars1m_dates(10) == ["2099-01-05", "2099-01-06"]
    assert recent_bars1m_dates(1) == ["2099-01-06"]

    dates = recent_bars1m_dates(10)
    # auto(jp) は JP.* のみ。US は除外。
    assert auto_symbols_bars1m(dates, session="jp") == ["JP.7203", "JP.9432"]
    # session 未指定なら全銘柄
    assert "US.AAPL" in auto_symbols_bars1m(dates, session=None)

    # 日付横断連結: 2 日ぶんが time 昇順で結合される
    hist = load_bars1m_history("JP.7203", dates)
    assert list(hist.columns) == loader.BARS1M_COLUMNS
    assert len(pd.to_datetime(hist["time"]).dt.date.unique()) == 2
    assert hist["time"].is_monotonic_increasing

    # 欠損日はスキップ（存在しない日を混ぜても落ちない）
    assert len(load_bars1m_history("JP.7203", ["2099-01-04"] + dates)) == len(hist)

    # worker（bars1m ソース）を通しで: JP セッション分割で集計 1 行が返る
    task = {
        "code": "JP.7203", "combos": [{"rci_period": 5, "slope_threshold": 15.0, "eff_min": 1.0}],
        "fixed": FIXED, "cost_bp": 3.0, "eval_bars": (1, 2, 3), "min_signals": 1,
        "rth_only": True, "session": "jp", "source": "bars1m", "days": 10,
        "today_iso": "2099-01-06", "dates": dates,
    }
    row = _screen_worker(task)
    assert row is not None and row["code"] == "JP.7203"
    assert row["n_signals_total"] >= 2  # 2 日ぶん発火（前場内で成立）
