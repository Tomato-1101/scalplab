"""バックテスト end-to-end テスト。

契約スキーマの parquet を一時ディレクトリに生成し、仕込んだロングシグナルが期待時刻±に発火し、
+60s リターンが仕込んだ値幅（約 +30bp）に一致することを確認する。
loader は MiniLoader を DI（store.loader 非依存）。engine の loader DI 設計もここで担保する。
"""
import numpy as np
import pandas as pd

from scalplab.backtest.engine import ParquetLoader, get_loader, run_backtest
from scalplab.backtest.metrics import summarize

from fixtures_synth import (
    BASE_DATE,
    MiniLoader,
    long_signal_scenario,
    make_quotes,
    make_ticks,
    write_parquet,
)


def test_get_loader_falls_back_to_parquet_loader():
    # store.loader が未提供でも ParquetLoader にフォールバックする（自動フォールバックの担保）
    loader = get_loader()
    assert hasattr(loader, "load_ticks")


def test_end_to_end_long_signal(tmp_path):
    code = "SYNTH.LONG"
    ticks, sig_start, sig_end = long_signal_scenario()
    write_parquet(ticks, tmp_path, "ticks", BASE_DATE, code)

    loader = MiniLoader(tmp_path)
    entries = run_backtest(BASE_DATE, [code], loader=loader)

    assert len(entries) >= 1, "シグナルが1本も出ていない"

    # 仕込んだ分(11分台)に発火したロングシグナルを抽出
    in_minute = entries[(entries["time"] >= sig_start) & (entries["time"] < sig_end)]
    longs = in_minute[in_minute["direction"] == "long"]
    assert len(longs) == 1, f"仕込んだ分のロングは1本のはず: {len(longs)}"

    row = longs.iloc[0]
    assert not bool(row["eval_truncated"]), "評価窓が切れている"
    assert row["sar"] < row["entry_price"], "SAR は価格より下（ロング可）のはず"
    # +0.005/秒(≈+0.5bp/秒) の線形上昇を仕込んだので +60s は約 +30bp
    assert 15.0 <= row["ret_60s_gross"] <= 45.0, f"ret_60s_gross={row['ret_60s_gross']}"
    assert row["ret_60s_net"] > 0.0
    # MFE は正、方向符号込みで妥当
    assert row["mfe_bp"] >= row["ret_60s_gross"] - 1e-6


def test_end_to_end_with_quotes_entry(tmp_path):
    """quotes があればエントリー価格に ask/bid を使い、コストに half-spread を反映する。"""
    code = "SYNTH.Q"
    ticks, sig_start, sig_end = long_signal_scenario()
    # 全ティック時刻に spread=0.02 の気配を張る
    q = make_quotes(ticks["time"], ticks["price"] - 0.01, ticks["price"] + 0.01)
    write_parquet(ticks, tmp_path, "ticks", BASE_DATE, code)
    write_parquet(q, tmp_path, "quotes", BASE_DATE, code)

    loader = MiniLoader(tmp_path)
    entries = run_backtest(BASE_DATE, [code], loader=loader)
    longs = entries[(entries["time"] >= sig_start) & (entries["time"] < sig_end)]
    longs = longs[longs["direction"] == "long"]
    assert len(longs) == 1
    row = longs.iloc[0]
    # half-spread ≈ 0.01/entry*1e4 ≈ 1bp が計上されている
    assert row["half_spread_bp"] > 0.0
    assert row["cost_bp"] >= row["half_spread_bp"] - 1e-9


def test_summarize_and_truncation_flag(tmp_path):
    """評価窓が切れるシグナルは truncated として集計から除外される。"""
    code = "SYNTH.TRUNC"
    ticks, _s, _e = long_signal_scenario()
    # 末尾を削ってシグナル後 120s を確保できないようにする（truncated を誘発）
    cut = ticks.iloc[: len(ticks) - 40].reset_index(drop=True)
    write_parquet(cut, tmp_path, "ticks", BASE_DATE, code)

    loader = MiniLoader(tmp_path)
    entries = run_backtest(BASE_DATE, [code], loader=loader)
    s = summarize(entries, horizon=60)
    assert s["n_signals"] >= 1
    assert s["n_signals"] == s["n_valid"] + s["n_truncated"]


def test_empty_when_no_ticks_file(tmp_path):
    """録画ファイルが無い銘柄はスキップされ、空 entries でも列スキーマは保たれる。"""
    loader = MiniLoader(tmp_path)
    entries = run_backtest(BASE_DATE, ["NOPE.X"], loader=loader)
    assert len(entries) == 0
    assert "ret_60s_net" in entries.columns
