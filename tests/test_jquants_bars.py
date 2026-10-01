"""J-Quants 分足取り込み（datafeed.jquants_bars）のテスト（ネットワーク不使用・全モック）。

実 API キーが無いため実接続は一切試みない。HTTP は request_fn 注入でモックし、以下を固定する:
- レスポンス dict → 契約スキーマ DataFrame 正規化（O/H/L/C/Vo/Va・Date+Time 結合・日付分割）
- 配列キー名に依存しない頑健なバー配列抽出
- ページネーションループ（pagination_key を次リクエストへ引き継ぐ）
- 上書きポリシー純関数（無し→書く / yfinance→置換 / それ以外→書かない）
- コード変換（JP.6981 → 6981、285A は 4 桁のまま）
- API キー無し時に --daily が何も書かず 0 で終了すること
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from scalplab.datafeed import jquants_bars as jq
from scalplab.store.loader import bars1m_path
from scalplab.store.writer import BARS1M_COLUMNS, enforce_bars1m, write_parquet


def _bar(d: str, t: str, o=100.0, h=101.0, low=99.0, c=100.5, vo=1000, va=100500.0) -> dict:
    """J-Quants 風のバー dict（Date/Time/O/H/L/C/Vo/Va）を作る。"""
    return {"Date": d, "Time": t, "Code": "6981",
            "O": o, "H": h, "L": low, "C": c, "Vo": vo, "Va": va}


# --- コード変換 ---

def test_to_jq_code():
    assert jq._to_jq_code("JP.6981") == "6981"
    assert jq._to_jq_code("JP.285A") == "285A"   # 英字入り 4 桁もそのまま
    assert jq._to_jq_code("6981") == "6981"       # 既に裸ならそのまま


# --- レスポンス配列抽出（キー名に依存しない） ---

def test_extract_bar_list_robust():
    # バー配列のキー名が未知でも「最初の list 値」を拾う。pagination_key(str) は誤検出しない。
    resp = {"pagination_key": "k1", "message": "ok", "whatever_bars": [_bar("2026-07-15", "09:00:00")]}
    got = jq._extract_bar_list(resp)
    assert isinstance(got, list) and len(got) == 1
    # list 値が無ければ空
    assert jq._extract_bar_list({"foo": "bar"}) == []
    assert jq._extract_bar_list("not a dict") == []


# --- 正規化・日付分割 ---

def test_normalize_and_split_by_date():
    bars = [_bar("2026-07-15", "09:00:00", o=100, c=100.5, va=100500.0),
            _bar("2026-07-15", "09:01:00"),
            _bar("2026-07-16", "09:00:00")]
    by_date = jq.split_by_date(jq.normalize_bars(bars))
    assert set(by_date) == {"2026-07-15", "2026-07-16"}
    assert len(by_date["2026-07-15"]) == 2
    assert len(by_date["2026-07-16"]) == 1

    df = enforce_bars1m(by_date["2026-07-15"])
    assert list(df.columns) == BARS1M_COLUMNS
    assert (df["source"] == "jquants").all()
    assert df["turnover"].iloc[0] == 100500.0      # Va → turnover
    assert df["volume"].iloc[0] == 1000            # Vo → volume
    assert df["open"].iloc[0] == 100.0             # O → open
    assert str(df["time"].dtype) == "datetime64[ns]"
    assert df["time"].iloc[0] == pd.Timestamp("2026-07-15 09:00:00")


def test_normalize_accepts_compact_datetime():
    # コンパクト形式（20260715 / 0900）も ISO 化されること（実データ未確認への頑健化）。
    row = jq.normalize_bars([_bar("20260715", "0900")])
    assert row["time"].iloc[0] == "2026-07-15 09:00:00"


def test_normalize_empty_is_contract_shaped():
    df = jq.normalize_bars([])
    assert df.empty
    assert list(df.columns) == BARS1M_COLUMNS
    assert jq.split_by_date(df) == {}


# --- ページネーションループ ---

def test_pagination_loop_follows_key():
    pages = [
        {"data": [_bar("2026-07-15", "09:00:00"), _bar("2026-07-15", "09:01:00")], "pagination_key": "k1"},
        {"data": [_bar("2026-07-15", "09:02:00")], "pagination_key": "k2"},
        {"data": [_bar("2026-07-15", "09:03:00")]},  # pagination_key 無し → 停止
    ]
    calls: list[dict] = []

    def request_fn(params):
        calls.append(dict(params))
        return pages[len(calls) - 1]

    by_date = jq.fetch_bars("JP.6981", "20260715", "20260715",
                            request_fn=request_fn, throttle_sec=0)
    # 3 リクエストでちょうど止まる
    assert len(calls) == 3
    # 初回は pagination_key 無し・裸コード・from/to が入る
    assert "pagination_key" not in calls[0]
    assert calls[0]["code"] == "6981"
    assert calls[0]["from"] == "20260715" and calls[0]["to"] == "20260715"
    # 2 回目以降は前レスポンスの pagination_key を引き継ぐ
    assert calls[1]["pagination_key"] == "k1"
    assert calls[2]["pagination_key"] == "k2"
    # 全 4 バーが集約される
    assert sum(len(df) for df in by_date.values()) == 4


def test_pagination_respects_max_requests_cap():
    # pagination_key が尽きなくても max_requests で打ち切る（暴走防止）。
    calls = {"n": 0}

    def request_fn(params):
        calls["n"] += 1
        return {"data": [_bar("2026-07-15", f"09:{calls['n']:02d}:00")], "pagination_key": "always"}

    jq.fetch_bars("JP.6981", "20260715", "20260715",
                  request_fn=request_fn, max_requests=3, throttle_sec=0)
    assert calls["n"] == 3


# --- 上書きポリシー（純関数） ---

def test_should_write_bars_policy():
    assert jq.should_write_bars(False, None) is True        # 無し → 書く
    assert jq.should_write_bars(True, "yfinance") is True    # yfinance → 置換
    assert jq.should_write_bars(True, "ticks") is False      # 録画由来 → 書かない
    assert jq.should_write_bars(True, None) is False         # 既存だが source 不明 → 触らない


def _write_bar(date_str: str, code: str, source: str, root):
    write_parquet(enforce_bars1m(pd.DataFrame({
        "time": [f"{date_str} 09:00:00"], "open": [1.0], "high": [1.0],
        "low": [1.0], "close": [1.0], "volume": [1], "turnover": [1.0],
        "source": [source]})), bars1m_path(date_str, code, root=root))


def test_save_bars_for_date_respects_existing_source(tmp_path):
    code = "JP.6981"
    _write_bar("2026-07-15", code, "yfinance", tmp_path)  # 置換される
    _write_bar("2026-07-16", code, "ticks", tmp_path)     # 保護される
    new = jq.normalize_bars([_bar("2026-07-17", "09:00:00")])  # 中身は流用

    assert jq.save_bars_for_date("2026-07-15", code, new, data_root=tmp_path) is True   # yfinance→置換
    assert jq.save_bars_for_date("2026-07-16", code, new, data_root=tmp_path) is False  # ticks→保護
    assert jq.save_bars_for_date("2026-07-17", code, new, data_root=tmp_path) is True   # 新規

    got15 = pd.read_parquet(bars1m_path("2026-07-15", code, root=tmp_path))
    assert (got15["source"] == "jquants").all()            # 公式データで置換された
    got16 = pd.read_parquet(bars1m_path("2026-07-16", code, root=tmp_path))
    assert (got16["source"] == "ticks").all()              # 録画由来は不変


# --- 日次取り込み ---

def test_run_daily_no_key_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(jq.ENV_KEY, raising=False)
    monkeypatch.setattr(jq, "find_api_key", lambda: None)
    res = jq.run_daily(top_n=5, data_root=tmp_path, uni_dir=tmp_path)
    assert res["skipped"] == "no_api_key"
    assert res["written"] == 0
    assert not (tmp_path / "bars1m").exists()   # 何も書かない


def test_main_daily_no_key_exits_zero(monkeypatch):
    monkeypatch.delenv(jq.ENV_KEY, raising=False)
    monkeypatch.setattr(jq, "find_api_key", lambda: None)
    assert jq.main(["--daily"]) == 0            # スキップメッセージで正常終了


def test_run_daily_with_mock_writes(tmp_path):
    # uni_dir にランキング CSV を置き、上位 1 銘柄を request_fn モックで取り込む。
    (tmp_path / "jp-tick-ranking-2026-07-20.csv").write_text(
        "rank,code,name,tick_count\n1,JP.6981,A,100\n", encoding="utf-8")

    def request_fn(params):
        # daily 期間内（today-4..today）の 07-20 バーを返す（params は無視）。
        return {"data": [_bar("2026-07-20", "09:00:00")]}

    res = jq.run_daily(top_n=1, request_fn=request_fn, data_root=tmp_path,
                       uni_dir=tmp_path, today=date(2026, 7, 20), throttle_sec=0)
    assert res["skipped"] is None
    assert res["written"] >= 1
    got = pd.read_parquet(bars1m_path("2026-07-20", "JP.6981", root=tmp_path))
    assert (got["source"] == "jquants").all()
