"""過去1分足キャッシュ（datafeed.history）のテスト（OpenD 非依存）。

fetch_history_1m がキャッシュを尊重し、
  - 全期間キャッシュ済みなら OpenD へ一切リクエストしない、
  - 差分（新しい取引日）だけを取りに行く、
  - 複数ページを連結する、
ことをモッククライアントで検証する。契約スキーマ（bars1m + source='moomoo_hist'）も確認。
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from scalplab.datafeed import history

BARS1M_COLS = ["time", "open", "high", "low", "close", "volume", "turnover", "source"]


class FakeClient:
    """[start, end] のカレンダー日ごとに 1 バー（09:30）を返すモック。呼び出し範囲を記録する。"""

    def __init__(self):
        self.calls = []  # (start, end) の列

    def request_history_kline(self, code, start, end, ktype, autype, max_count, page_req_key, extended_time):
        self.calls.append((start, end))
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
        rows = []
        d = s
        while d <= e:
            rows.append({
                "time_key": f"{d.isoformat()} 09:30:00",
                "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2,
                "volume": 1000, "turnover": 100200.0,
            })
            d += timedelta(days=1)
        return pd.DataFrame(rows), None  # 単一ページ


def test_fetch_caches_and_skips_when_fully_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HIST_DIR", tmp_path)
    fake = FakeClient()
    today = date(2099, 1, 20)

    b1 = history.fetch_history_1m("US.TEST", days=5, client=fake, throttle_sec=0.0, today=today)
    assert len(b1) > 0
    n1 = len(fake.calls)
    assert n1 >= 1
    assert (tmp_path / "US.TEST.parquet").exists()
    # 契約スキーマ・source
    assert list(b1.columns) == BARS1M_COLS
    assert (b1["source"] == "moomoo_hist").all()
    assert str(b1["time"].dtype) == "datetime64[ns]"

    # 同じ窓で再取得 → 追加リクエストは発生しない（クォータ・レート節約）
    b2 = history.fetch_history_1m("US.TEST", days=5, client=fake, throttle_sec=0.0, today=today)
    assert len(fake.calls) == n1  # 呼び出し回数が増えていない
    assert len(b2) == len(b1)


def test_fetch_incremental_only_requests_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HIST_DIR", tmp_path)
    fake = FakeClient()
    history.fetch_history_1m("US.INC", days=5, client=fake, throttle_sec=0.0, today=date(2099, 1, 20))
    fake.calls.clear()

    # today を 2 日進める → キャッシュ最終日(01-20)の翌日〜新 today だけ取りに行く
    b = history.fetch_history_1m("US.INC", days=5, client=fake, throttle_sec=0.0, today=date(2099, 1, 22))
    assert len(fake.calls) == 1
    s, e = fake.calls[0]
    assert s == "2099-01-21"
    assert e == "2099-01-22"
    assert pd.to_datetime(b["time"]).dt.date.max() == date(2099, 1, 22)


class PagingClient:
    """1 回目に next_page_req_key を返し、2 回目で None を返す（ページング検証）。"""

    def __init__(self):
        self.pages = 0

    def request_history_kline(self, code, start, end, ktype, autype, max_count, page_req_key, extended_time):
        self.pages += 1
        if page_req_key is None:
            df = pd.DataFrame([{
                "time_key": f"{start} 09:30:00", "open": 100.0, "high": 100.5,
                "low": 99.5, "close": 100.2, "volume": 1000, "turnover": 100200.0,
            }])
            return df, "PAGE2"
        df = pd.DataFrame([{
            "time_key": f"{start} 09:31:00", "open": 100.2, "high": 100.7,
            "low": 100.0, "close": 100.5, "volume": 900, "turnover": 90450.0,
        }])
        return df, None


def test_fetch_concatenates_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HIST_DIR", tmp_path)
    fake = PagingClient()
    b = history.fetch_history_1m("US.PAGE", days=1, client=fake, throttle_sec=0.0, today=date(2099, 1, 10))
    assert fake.pages == 2  # 2 ページ取得した
    assert len(b) == 2  # 両ページが連結され、異なる時刻なので重複排除されない
    assert b["time"].is_monotonic_increasing
