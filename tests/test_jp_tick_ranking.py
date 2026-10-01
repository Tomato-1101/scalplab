"""松井 Tick回数ランキングのパーサ・保存・フォールバックのテスト（ネットは一切叩かない）。

フィクスチャは実ページを切り詰めた tests/fixtures/matsui_tick_page{1,2}.html を使う。
"""
from __future__ import annotations

from pathlib import Path

from scalplab.datafeed import jp_tick_ranking as jr

FIX = Path(__file__).parent / "fixtures"


def _p1() -> str:
    return (FIX / "matsui_tick_page1.html").read_text(encoding="utf-8")


def _p2() -> str:
    return (FIX / "matsui_tick_page2.html").read_text(encoding="utf-8")


# --- パーサ ---

def test_parse_page1_counts_and_head():
    rows = jr.parse_ranking_html(_p1())
    assert len(rows) == 6
    # 先頭行を完全一致で（rank/code/name/tick_count すべて）
    assert rows[0] == {
        "rank": 1, "code": "JP.285A", "name": "キオクシアホールディングス", "tick_count": 205097,
    }
    assert rows[1]["code"] == "JP.9984" and rows[1]["tick_count"] == 93544
    assert rows[2]["code"] == "JP.6981" and rows[2]["name"] == "村田製作所"
    assert [r["rank"] for r in rows] == [1, 2, 3, 4, 5, 6]


def test_parse_page2_absolute_rank_and_desc():
    rows = jr.parse_ranking_html(_p2())
    assert len(rows) == 4
    assert rows[0]["rank"] == 51   # page2 は絶対順位（51〜）が入る
    tc = [r["tick_count"] for r in rows]
    assert tc == sorted(tc, reverse=True)   # 約定回数の降順


def test_parse_ignores_non_ranking_stock_links():
    """ランキング表の外にある /stock/ リンクを混ぜても拾わない（表を data-type で限定している）。"""
    noisy = '<a href=/stock/9999/index>ノイズ</a>\n' + _p1()
    rows = jr.parse_ranking_html(noisy)
    assert len(rows) == 6
    assert all(r["code"] != "JP.9999" for r in rows)


def test_parse_empty_when_no_ranking_table():
    assert jr.parse_ranking_html("<html><body>no table here</body></html>") == []


# --- 保存・読み込み ---

def test_save_and_load_roundtrip(tmp_path):
    rows = jr.parse_ranking_html(_p1())
    path = jr.save_ranking(rows, "2099-01-06", tmp_path)
    assert path.name == "jp-tick-ranking-2099-01-06.csv"
    loaded = jr.load_ranking_csv(path)
    assert loaded == rows   # rank/tick_count の int 型込みで往復一致


def test_latest_saved_csv_picks_newest(tmp_path):
    jr.save_ranking(jr.parse_ranking_html(_p1()), "2099-01-05", tmp_path)
    jr.save_ranking(jr.parse_ranking_html(_p1()), "2099-01-07", tmp_path)
    latest = jr.latest_saved_csv(tmp_path)
    assert latest is not None and latest.name == "jp-tick-ranking-2099-01-07.csv"


# --- get_ranking のフォールバック連鎖 ---

def test_get_ranking_cached_today_does_not_fetch(tmp_path, monkeypatch):
    rows = jr.parse_ranking_html(_p1())
    jr.save_ranking(rows, "2099-01-06", tmp_path)

    def _boom(**k):
        raise AssertionError("当日キャッシュがあるのにフェッチしてはいけない")

    monkeypatch.setattr(jr, "fetch_ranking", _boom)
    got, src = jr.get_ranking("2099-01-06", ranking_dir=tmp_path)
    assert src == "cached_today"
    assert got == rows


def test_get_ranking_fetch_and_save(tmp_path, monkeypatch):
    rows = jr.parse_ranking_html(_p1())
    monkeypatch.setattr(jr, "fetch_ranking", lambda **k: rows)
    got, src = jr.get_ranking("2099-01-07", ranking_dir=tmp_path)
    assert src == "fetched"
    assert got == rows
    assert jr.ranking_csv_path("2099-01-07", tmp_path).exists()   # 取得したら保存する


def test_get_ranking_falls_back_to_saved_previous(tmp_path, monkeypatch):
    prev = jr.parse_ranking_html(_p1())
    jr.save_ranking(prev, "2099-01-05", tmp_path)   # 直近の保存済み

    def _fail(**k):
        raise RuntimeError("network down")

    monkeypatch.setattr(jr, "fetch_ranking", _fail)
    got, src = jr.get_ranking("2099-01-08", ranking_dir=tmp_path)
    assert src == "saved_previous"
    assert got == prev


def test_get_ranking_unavailable_when_nothing(tmp_path, monkeypatch):
    def _fail(**k):
        raise RuntimeError("network down")

    monkeypatch.setattr(jr, "fetch_ranking", _fail)
    got, src = jr.get_ranking("2099-01-09", ranking_dir=tmp_path)
    assert src == "unavailable" and got == []
