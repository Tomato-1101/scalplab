"""日本株 1 分足バックフィル（datafeed.backfill_jp）のテスト（yfinance モック・OpenD 非依存）。

- date_chunks: 8 日窓制約に収まるよう連続・重複なしで全期間を覆うこと。
- backfill_code: 既存 (date, code) を上書きしない冪等ガードと、契約スキーマ保存の確認。
- resolve_backfill_codes: fixed ∪ 最新ランキング ∪ 既存 bars1m の和集合になること。
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from scalplab.datafeed import backfill_jp as bf
from scalplab.store.loader import bars1m_path
from scalplab.store.writer import BARS1M_COLUMNS, enforce_bars1m, write_parquet


# --- date_chunks ---

def test_date_chunks_within_window_and_contiguous():
    chunks = bf.date_chunks(date(2026, 6, 20), date(2026, 7, 20))
    # 各チャンクは 7 日（=8 日窓の安全側）以下
    assert all((ce - cs).days + 1 <= 7 for cs, ce in chunks)
    # 全期間を端まで覆う
    assert chunks[0][0] == date(2026, 6, 20)
    assert chunks[-1][1] == date(2026, 7, 20)
    # 連続・重複なし（前チャンク末尾 +1 日 = 次チャンク先頭）
    for (a, b), (c, d) in zip(chunks, chunks[1:]):
        assert c == b + timedelta(days=1)


def test_date_chunks_edge_cases():
    # 単日
    assert bf.date_chunks(date(2026, 7, 20), date(2026, 7, 20)) == [
        (date(2026, 7, 20), date(2026, 7, 20))]
    # start > end は空
    assert bf.date_chunks(date(2026, 7, 21), date(2026, 7, 20)) == []
    # ちょうど窓幅
    ch = bf.date_chunks(date(2026, 7, 1), date(2026, 7, 7))
    assert ch == [(date(2026, 7, 1), date(2026, 7, 7))]


# --- yfinance モック ---

def _fake_hist(day_dates: list[date]) -> pd.DataFrame:
    """指定日ごとに 09:00/09:01 の 2 バーを持つ yfinance 風 DataFrame（tz-aware index）を作る。"""
    rows = []
    for d in day_dates:
        for hm in ("09:00", "09:01"):
            rows.append({"ts": f"{d.isoformat()} {hm}:00",
                         "Open": 100.0, "High": 100.5, "Low": 99.5,
                         "Close": 100.2, "Volume": 1000})
    df = pd.DataFrame(rows)
    idx = pd.to_datetime(df["ts"]).dt.tz_localize("Asia/Tokyo")
    return pd.DataFrame(
        {"Open": df["Open"].to_numpy(), "High": df["High"].to_numpy(),
         "Low": df["Low"].to_numpy(), "Close": df["Close"].to_numpy(),
         "Volume": df["Volume"].to_numpy()},
        index=pd.DatetimeIndex(idx, name="Datetime"))


def _make_fetcher(available: list[date]):
    """[start, end) に含まれる available 日だけを返すフェッチャ（end 排他を検証できる）。"""
    def fetcher(ticker, start, end):
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
        days = [d for d in available if s <= d < e]
        return _fake_hist(days) if days else pd.DataFrame()
    return fetcher


# --- backfill_code の冪等ガード ---

def test_backfill_code_does_not_overwrite_existing(tmp_path):
    code = "JP.7203"
    d_existing, d_new = "2026-07-15", "2026-07-16"

    # 既存日は録画ティック由来（source='ticks'）を先に置く。上書きされてはならない。
    sentinel = enforce_bars1m(pd.DataFrame({
        "time": ["2026-07-15 09:00:00"], "open": [1.0], "high": [1.0],
        "low": [1.0], "close": [1.0], "volume": [1], "turnover": [1.0],
        "source": ["ticks"]}))
    write_parquet(sentinel, bars1m_path(d_existing, code, root=tmp_path))

    fetcher = _make_fetcher([date(2026, 7, 15), date(2026, 7, 16)])
    chunks = bf.date_chunks(date(2026, 7, 15), date(2026, 7, 16))
    written, skipped = bf.backfill_code(
        code, chunks, fetcher=fetcher, data_root=tmp_path, throttle_sec=0)

    assert (written, skipped) == (1, 1)  # 07-16 を新規保存 / 07-15 は既存スキップ

    # 既存日は録画由来のまま（上書きされていない）
    keep = pd.read_parquet(bars1m_path(d_existing, code, root=tmp_path))
    assert keep["source"].iloc[0] == "ticks"
    assert len(keep) == 1

    # 新規日は yfinance で契約スキーマ保存されている
    got = pd.read_parquet(bars1m_path(d_new, code, root=tmp_path))
    assert list(got.columns) == BARS1M_COLUMNS
    assert (got["source"] == "yfinance").all()
    assert str(got["time"].dtype) == "datetime64[ns]"
    assert len(got) == 2


def test_backfill_code_empty_returns_zero(tmp_path):
    fetcher = _make_fetcher([])  # 常に空（祝日・休場相当）
    chunks = bf.date_chunks(date(2026, 7, 15), date(2026, 7, 16))
    assert bf.backfill_code("JP.9999", chunks, fetcher=fetcher,
                            data_root=tmp_path, throttle_sec=0) == (0, 0)


# --- 対象銘柄の和集合 ---

def test_resolve_backfill_codes_union(tmp_path):
    # config/universe.yaml（fixed）
    cfg = tmp_path / "universe.yaml"
    cfg.write_text("fixed: [JP.7203]\nauto_top_n: 2\n", encoding="utf-8")

    # 最新ランキング CSV（上位 auto_top_n=2 だけ拾う）
    uni = tmp_path / "universe"
    uni.mkdir()
    (uni / "jp-tick-ranking-2026-07-19.csv").write_text(
        "rank,code,name\n1,JP.6981,A\n2,JP.285A,B\n3,JP.9984,C\n", encoding="utf-8")

    # 既存 bars1m（別銘柄）
    write_parquet(
        enforce_bars1m(pd.DataFrame({
            "time": ["2026-07-14 09:00:00"], "open": [1.0], "high": [1.0],
            "low": [1.0], "close": [1.0], "volume": [1], "turnover": [1.0],
            "source": ["ticks"]})),
        bars1m_path("2026-07-14", "JP.5803", root=tmp_path))

    codes = bf.resolve_backfill_codes(config_path=cfg, uni_dir=uni, data_root=tmp_path)
    # fixed(7203) ∪ ランキング上位2(6981,285A) ∪ 既存(5803)。9984 は top_n 超過で除外。
    assert set(codes) == {"JP.7203", "JP.6981", "JP.285A", "JP.5803"}
    assert "JP.9984" not in codes
