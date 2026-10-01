"""store 層のテスト（ネットワーク・OpenD 非依存）。

- live CSV → finalize → loader の契約スキーマ round-trip
- seq 重複排除・time 昇順
- resample_ticks_to_bars の正しさ（手計算フィクスチャ）
"""
from __future__ import annotations

import csv

import pandas as pd
import pytest

from scalplab.store import loader
from scalplab.store.finalize import finalize_date
from scalplab.store.writer import (
    BARS1M_COLUMNS,
    QUOTES_COLUMNS,
    TICKS_COLUMNS,
)

DATE = "2026-07-15"
CODE = "US.AAPL"


def _write_live_csv(live_dir, code, kind, columns, rows):
    d = live_dir / DATE
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{code}.{kind}.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def test_ticks_roundtrip_dedup_and_sort(tmp_path, monkeypatch):
    """live CSV（重複 seq・時刻バラバラ）→ finalize → load_ticks で契約通り返るか。"""
    live = tmp_path / "live"
    out = tmp_path / "out"
    # loader は module 変数 DATA_DIR を参照するので差し替える
    monkeypatch.setattr(loader, "DATA_DIR", out)

    rows = [
        # time 昇順でない・seq=101 が重複している
        {"time": "2026-07-15 09:00:02.000", "price": 102.0, "volume": 3, "turnover": 306.0, "direction": "BUY", "seq": 103, "tick_type": "AUTO_MATCH"},
        {"time": "2026-07-15 09:00:00.100", "price": 100.0, "volume": 10, "turnover": 1000.0, "direction": "SELL", "seq": 101, "tick_type": "AUTO_MATCH"},
        {"time": "2026-07-15 09:00:01.000", "price": 101.0, "volume": 5, "turnover": 505.0, "direction": "NEUTRAL", "seq": 102, "tick_type": "ODD_LOT"},
        {"time": "2026-07-15 09:00:00.100", "price": 100.0, "volume": 10, "turnover": 1000.0, "direction": "SELL", "seq": 101, "tick_type": "AUTO_MATCH"},  # 重複
    ]
    _write_live_csv(live, CODE, "ticks", TICKS_COLUMNS, rows)

    results = finalize_date(DATE, live_root=live, out_root=out)
    assert results[CODE]["ticks"] == 3  # 重複が1件除かれる

    df = loader.load_ticks(DATE, CODE)
    assert list(df.columns) == TICKS_COLUMNS
    assert df["seq"].tolist() == [101, 102, 103]  # time 昇順で並ぶ
    assert df["seq"].is_unique
    assert df["time"].is_monotonic_increasing
    assert str(df["time"].dtype) == "datetime64[ns]"
    assert df["volume"].dtype == "int64"
    assert df["price"].dtype == "float64"


def test_ticks_finalize_generates_bars1m(tmp_path, monkeypatch):
    """finalize がティックから1分足（source='ticks'）も生成するか。"""
    live = tmp_path / "live"
    out = tmp_path / "out"
    monkeypatch.setattr(loader, "DATA_DIR", out)

    rows = [
        {"time": "2026-07-15 09:00:00.100", "price": 100.0, "volume": 10, "turnover": 1000.0, "direction": "BUY", "seq": 1, "tick_type": "AUTO_MATCH"},
        {"time": "2026-07-15 09:00:30.000", "price": 105.0, "volume": 5, "turnover": 525.0, "direction": "BUY", "seq": 2, "tick_type": "AUTO_MATCH"},
        {"time": "2026-07-15 09:00:59.900", "price": 102.0, "volume": 3, "turnover": 306.0, "direction": "SELL", "seq": 3, "tick_type": "AUTO_MATCH"},
        {"time": "2026-07-15 09:01:05.000", "price": 110.0, "volume": 2, "turnover": 220.0, "direction": "BUY", "seq": 4, "tick_type": "AUTO_MATCH"},
    ]
    _write_live_csv(live, CODE, "ticks", TICKS_COLUMNS, rows)
    finalize_date(DATE, live_root=live, out_root=out)

    bars = loader.load_bars1m(DATE, CODE)
    assert list(bars.columns) == BARS1M_COLUMNS
    assert len(bars) == 2  # 09:00 台と 09:01 台の2本
    first = bars.iloc[0]
    assert first["open"] == 100.0
    assert first["high"] == 105.0
    assert first["low"] == 100.0
    assert first["close"] == 102.0
    assert first["volume"] == 18
    assert first["turnover"] == pytest.approx(1831.0)
    assert first["source"] == "ticks"
    second = bars.iloc[1]
    assert second["open"] == 110.0 and second["close"] == 110.0
    assert second["volume"] == 2


def test_quotes_roundtrip(tmp_path, monkeypatch):
    """L1 気配の live CSV → finalize → load_quotes の round-trip。"""
    live = tmp_path / "live"
    out = tmp_path / "out"
    monkeypatch.setattr(loader, "DATA_DIR", out)

    rows = [
        {"time": "2026-07-15 09:00:01.500", "bid": 100.0, "ask": 100.5, "bid_vol": 300, "ask_vol": 200},
        {"time": "2026-07-15 09:00:00.500", "bid": 99.5, "ask": 100.0, "bid_vol": 100, "ask_vol": 400},
    ]
    _write_live_csv(live, CODE, "quotes", QUOTES_COLUMNS, rows)
    finalize_date(DATE, live_root=live, out_root=out)

    df = loader.load_quotes(DATE, CODE)
    assert list(df.columns) == QUOTES_COLUMNS
    assert df["time"].is_monotonic_increasing
    assert df["bid_vol"].dtype == "int64"
    assert df["bid"].dtype == "float64"
    assert df.iloc[0]["bid"] == 99.5


def test_resample_ticks_to_bars_15s():
    """15秒足への resample が epoch 起点で :00/:15/:30/:45 に整列するか。"""
    ticks = pd.DataFrame([
        {"time": "2026-07-15 09:00:00.100", "price": 100.0, "volume": 10, "turnover": 1000.0, "direction": "BUY", "seq": 1, "tick_type": "AUTO_MATCH"},
        {"time": "2026-07-15 09:00:05.000", "price": 101.0, "volume": 4, "turnover": 404.0, "direction": "BUY", "seq": 2, "tick_type": "AUTO_MATCH"},
        {"time": "2026-07-15 09:00:20.000", "price": 103.0, "volume": 2, "turnover": 206.0, "direction": "SELL", "seq": 3, "tick_type": "AUTO_MATCH"},
    ])
    bars = loader.resample_ticks_to_bars(ticks, 15)
    assert len(bars) == 2  # 09:00:00-15 と 09:00:15-30
    b0 = bars.iloc[0]
    assert str(b0["time"]) == "2026-07-15 09:00:00"
    assert b0["open"] == 100.0 and b0["close"] == 101.0
    assert b0["high"] == 101.0 and b0["low"] == 100.0
    assert b0["volume"] == 14
    b1 = bars.iloc[1]
    assert str(b1["time"]) == "2026-07-15 09:00:15"
    assert b1["open"] == 103.0 and b1["volume"] == 2


def test_load_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "DATA_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        loader.load_ticks("2000-01-01", "US.NOPE")
    assert loader.list_recorded("2000-01-01") == []
