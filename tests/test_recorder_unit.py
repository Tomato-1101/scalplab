"""recorder のユニットロジック（futu は import できれば実物、接続はしない）。

- 購読リスト構築・クォータ判定
- セッション時刻判定
- 権限エラーで銘柄単位に継続する購読ロジック
- OpenD 未接続時に後処理を空振りさせない（2026-07-28 障害の回帰）
"""
from __future__ import annotations

from datetime import datetime, timedelta

from futu import SubType

from scalplab import record
from scalplab.datafeed.moomoo_client import NoPermissionError, OpenDUnavailableError

DATE = "2026-07-15"
# finalize 時刻を確実に過ぎている過去日。再接続ループに入らず即あきらめる経路を通す。
PAST_DATE = "2020-01-06"


def test_build_subscription_maps_and_ignores_unknown():
    subs = record.build_subscription(["JP.7203"], ["TICKER", "ORDER_BOOK", "BOGUS"])
    assert subs == [SubType.TICKER, SubType.ORDER_BOOK]


def test_within_quota():
    assert record.within_quota(48, 2) is True   # 96 <= 100
    assert record.within_quota(50, 2) is True    # 100 <= 100
    assert record.within_quota(51, 2) is False   # 102 > 100


def test_session_segments_and_membership():
    cfg = record.load_config()
    segments = record.session_segments(cfg, DATE)
    assert len(segments) == 2
    # 10:00 は午前セッション内
    assert record.is_in_session(datetime(2026, 7, 15, 10, 0), segments) is True
    # 12:00 は昼休みで場外
    assert record.is_in_session(datetime(2026, 7, 15, 12, 0), segments) is False
    # 13:00 は午後セッション内
    assert record.is_in_session(datetime(2026, 7, 15, 13, 0), segments) is True
    # 16:00 は引け後
    assert record.is_in_session(datetime(2026, 7, 15, 16, 0), segments) is False


class _FakeClient:
    """subscribe が JP 銘柄で権限エラー、US 銘柄で成功する擬似クライアント。"""

    def __init__(self):
        self.subscribed: list[str] = []

    def subscribe(self, codes, sub_objs):
        if codes[0].startswith("JP."):
            raise NoPermissionError("No permission (Stocks quote permissions)")
        self.subscribed.append(codes[0])


def test_subscribe_all_continues_on_permission_error():
    client = _FakeClient()
    ok, failed = record.subscribe_all(
        client, ["US.AAPL", "JP.7203", "US.TSLA"], [SubType.TICKER]
    )
    assert ok == ["US.AAPL", "US.TSLA"]
    assert [c for c, _ in failed] == ["JP.7203"]
    assert "NoPermission" in failed[0][1]


def test_subscribe_all_all_fail_returns_empty_ok():
    client = _FakeClient()
    ok, failed = record.subscribe_all(client, ["JP.7203", "JP.6758"], [SubType.TICKER])
    assert ok == []
    assert len(failed) == 2


# ---------------- OpenD 未接続時の後処理（2026-07-28 障害の回帰） ----------------

class _UnavailableClient:
    """connect が必ず OpenD 不達で失敗する擬似クライアント。"""

    def __init__(self):
        self.connect_calls = 0
        self.unsubscribed = False

    def connect(self):
        self.connect_calls += 1
        raise OpenDUnavailableError("OpenD へ接続できません（テスト）")

    def unsubscribe_all(self):
        self.unsubscribed = True

    def close(self):
        pass


class _FakeSink:
    def __init__(self, date):
        self.date = date

    def close(self):
        pass


def _patch_post_session(monkeypatch) -> dict:
    """finalize 以降の後処理を呼び出し回数の記録に差し替える。"""
    calls = {"finalize": 0, "fallback": 0}

    def _fake_finalize(date, cleanup=True):
        calls["finalize"] += 1
        return {}

    def _fake_fallback(date, codes):
        calls["fallback"] += 1
        return 0

    monkeypatch.setattr(record, "finalize_date", _fake_finalize)
    monkeypatch.setattr(record, "fetch_fallback_bars", _fake_fallback)
    monkeypatch.setattr(record, "fallback_codes", lambda date, codes, top_n: [])
    monkeypatch.setattr(record, "_walkforward_after_finalize", lambda *a: None)
    monkeypatch.setattr(record, "_walkforward_jp_after_finalize", lambda *a: None)
    monkeypatch.setattr(record, "_jquants_daily_after_finalize", lambda *a: None)
    monkeypatch.setattr(record, "MoomooClient", _UnavailableClient)
    monkeypatch.setattr(record, "LiveCsvSink", _FakeSink)
    return calls


def test_record_skips_finalize_when_opend_never_connected(monkeypatch):
    """OpenD へ一度も繋がらなかった日は finalize を空振りさせない。

    2026-07-28 の OpenD 停止では exit 1 → launchd 即再起動が 10 秒周期で回り、そのたびに
    空の finalize と yfinance 取得が走って JP bars1m を場中の数行で上書きしていた。
    1分足フォールバックは JP の唯一のデータ源なので引け後に 1 回だけ残す。
    """
    calls = _patch_post_session(monkeypatch)
    cfg = record.load_config()
    cfg["fallback_bars"] = True

    assert record.record(PAST_DATE, ["JP.7203"], cfg) == 1
    assert calls["finalize"] == 0
    assert calls["fallback"] == 1


def test_record_adhoc_connect_failure_skips_fallback(monkeypatch):
    """アドホック録画（--duration）は再接続で粘らず、後処理も走らせない。"""
    calls = _patch_post_session(monkeypatch)
    cfg = record.load_config()
    cfg["fallback_bars"] = True

    assert record.record(PAST_DATE, ["JP.7203"], cfg, duration=5.0) == 1
    assert calls["finalize"] == 0
    assert calls["fallback"] == 0


def test_connect_with_retry_recovers_when_opend_starts_later(monkeypatch):
    """場中に OpenD が起動したら、プロセス内リトライでその日の録画を継続できる。"""
    monkeypatch.setattr(record, "CONNECT_RETRY_SEC", 0.01)
    monkeypatch.setattr(record, "finalize_time",
                        lambda cfg, date, market: datetime.now() + timedelta(minutes=5))

    class _FlakyClient:
        def __init__(self):
            self.calls = 0

        def connect(self):
            self.calls += 1
            if self.calls < 3:
                raise OpenDUnavailableError("まだ起動していない")

    client = _FlakyClient()
    record._connect_with_retry(client, record.load_config(), DATE, "JP", None)
    assert client.calls == 3


def test_connect_with_retry_gives_up_after_finalize_time(monkeypatch):
    """引け時刻を過ぎても復活しなければ諦めて例外を投げる（無限待ちにしない）。"""
    monkeypatch.setattr(record, "CONNECT_RETRY_SEC", 0.01)
    client = _UnavailableClient()
    try:
        record._connect_with_retry(client, record.load_config(), PAST_DATE, "JP", None)
    except OpenDUnavailableError:
        pass
    else:  # pragma: no cover - 失敗時のみ
        raise AssertionError("OpenDUnavailableError が送出されなかった")
    assert client.connect_calls == 1  # 過去日は deadline 到達済みでリトライしない
