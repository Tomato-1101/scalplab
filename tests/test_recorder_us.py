"""米国株録画モード（--market US）のユニットロジック。

- NY 09:30-16:00 → マシンローカル(JST 固定で検証) naive datetime への変換（夏冬時間）
- セッション日（NY 現在日付）の決定と JST 深夜起動の境界
- config/universe_us.yaml の読み込み・クリップ

zoneinfo の夏冬時間差はライブラリに委ねるので、期待値は JST を明示（local_tz=Asia/Tokyo）して
テスト機のタイムゾーンに依存しないよう固定する。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

from scalplab import record
from scalplab.datafeed import universe

JST = ZoneInfo("Asia/Tokyo")


# ---------------- セッション時刻（夏時間 / 標準時間） ----------------

def test_us_session_segments_summer_july():
    """夏時間（EDT）: NY 09:30-16:00 は JST 22:30〜翌05:00 になる。"""
    segs = record.us_session_segments("2026-07-15", local_tz=JST)
    assert len(segs) == 1
    open_dt, close_dt = segs[0]
    assert open_dt == datetime(2026, 7, 15, 22, 30)
    assert close_dt == datetime(2026, 7, 16, 5, 0)  # JST では翌日にまたぐ


def test_us_session_segments_winter_january():
    """標準時間（EST）: NY 09:30-16:00 は JST 23:30〜翌06:00 になる。"""
    segs = record.us_session_segments("2026-01-15", local_tz=JST)
    open_dt, close_dt = segs[0]
    assert open_dt == datetime(2026, 1, 15, 23, 30)
    assert close_dt == datetime(2026, 1, 16, 6, 0)


def test_us_finalize_summer_and_winter():
    """finalize は NY 16:05 相当（夏=JST 翌05:05 / 冬=JST 翌06:05）。"""
    assert record.us_finalize_at("2026-07-15", local_tz=JST) == datetime(2026, 7, 16, 5, 5)
    assert record.us_finalize_at("2026-01-15", local_tz=JST) == datetime(2026, 1, 16, 6, 5)


def test_us_session_is_in_session_uses_local_naive():
    """変換後の区間は datetime.now()（ローカル naive）と直接突き合わせられる。"""
    segs = record.us_session_segments("2026-07-15", local_tz=JST)
    # 夏場: JST 23:00 は場中、JST 21:00（寄り前）と JST 06:00（引け後）は場外
    assert record.is_in_session(datetime(2026, 7, 15, 23, 0), segs) is True
    assert record.is_in_session(datetime(2026, 7, 15, 21, 0), segs) is False
    assert record.is_in_session(datetime(2026, 7, 16, 6, 0), segs) is False


def test_market_segments_dispatch_keeps_jp_intact():
    """market='JP' は東証2区間（従来どおり）、'US' は US 1区間を返す。"""
    cfg = record.load_config()
    jp = record.market_segments(cfg, "2026-07-15", "JP")
    assert jp == record.session_segments(cfg, "2026-07-15")  # JP パス無改変
    assert len(jp) == 2
    us = record.market_segments(cfg, "2026-07-15", "US")
    assert len(us) == 1


# ---------------- セッション日（NY 現在日付） ----------------

def test_us_session_day_jst_midnight_maps_to_previous_ny_date():
    """JST 深夜 2:00 に起動すると NY 日付は前日になる（夏: JST 02:00 = NY 前日 13:00 EDT）。"""
    now = datetime(2026, 7, 15, 2, 0, tzinfo=JST)
    assert record.us_session_day(now) == "2026-07-14"


def test_us_session_day_launchd_2220_is_same_ny_date():
    """launchd 起動時刻 JST 22:20 は NY 同日（夏: NY 09:20 EDT）。"""
    now = datetime(2026, 7, 15, 22, 20, tzinfo=JST)
    assert record.us_session_day(now) == "2026-07-15"


def test_us_session_day_winter_midnight_boundary():
    """冬（EST）でも JST 深夜 1:00 は NY 前日（JST 01:00 = NY 前日 11:00 EST）。"""
    now = datetime(2026, 1, 15, 1, 0, tzinfo=JST)
    assert record.us_session_day(now) == "2026-01-14"


# ---------------- universe_us の読み込み・クリップ ----------------

def test_load_config_us_defaults(tmp_path):
    """欠損ファイルでも既定（fixed=[], max_symbols=48）で読める。"""
    cfg = universe.load_config_us(tmp_path / "nope.yaml")
    assert cfg["fixed"] == []
    assert cfg["max_symbols"] == 48


def test_build_us_clips_to_max_symbols(tmp_path, monkeypatch):
    """fixed を max_symbols にクリップし US-YYYY-MM-DD.yaml に書く。"""
    # 50 銘柄・max 48 のテスト用 config
    fixed = [f"US.SYM{i}" for i in range(50)]
    cfg_path = tmp_path / "universe_us.yaml"
    cfg_path.write_text(yaml.safe_dump({"fixed": fixed, "max_symbols": 48}), encoding="utf-8")
    # 出力先は tmp に隔離（実 data/universe を汚さない）
    monkeypatch.setattr(universe, "UNIVERSE_DIR", tmp_path / "uni")

    doc = universe.build_us("2026-07-15", config_path=cfg_path)
    assert doc["market"] == "US"
    assert len(doc["codes"]) == 48
    assert doc["codes"][0] == "US.SYM0"
    # ファイルが US-*.yaml 命名で書かれ、JP の {date}.yaml と衝突しない
    out = universe.universe_us_yaml_path("2026-07-15")
    assert out.name == "US-2026-07-15.yaml"
    assert out.exists()


def test_resolve_codes_us_reads_existing_yaml(tmp_path, monkeypatch):
    """既存 US-*.yaml があればそれを読む（再 build しない）。"""
    monkeypatch.setattr(universe, "UNIVERSE_DIR", tmp_path)
    (tmp_path / "US-2026-07-15.yaml").write_text(
        yaml.safe_dump({"date": "2026-07-15", "codes": ["US.AAPL", "US.TSLA"]}), encoding="utf-8"
    )
    assert universe.resolve_codes_us("2026-07-15") == ["US.AAPL", "US.TSLA"]


def test_shipped_universe_us_config_is_within_quota():
    """同梱の config/universe_us.yaml が max_symbols とクォータに収まっている。"""
    cfg = universe.load_config_us()
    assert 0 < len(cfg["fixed"]) <= cfg["max_symbols"]
    assert all(c.startswith("US.") for c in cfg["fixed"])
    # 2購読/銘柄でクォータ100内
    assert record.within_quota(len(cfg["fixed"]), 2) is True
