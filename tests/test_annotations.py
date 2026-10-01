"""エントリー注釈ストア（scalplab.annotations）の CRUD・スキーマ・DataFrame 読み出しのテスト。

ネット・本番 DB 非依存（tmp_path に隔離した sqlite3 ファイルを使う）。
"""
from __future__ import annotations

import sqlite3

import pytest

from scalplab import annotations as A


def _db(tmp_path):
    return tmp_path / "annotations.sqlite3"


def test_schema_created_on_connect(tmp_path):
    """接続時に annotations テーブル（契約どおりの列）が作られる。"""
    path = _db(tmp_path)
    A.list_annotations(db_path=path)   # 接続だけでスキーマ生成
    conn = sqlite3.connect(str(path))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(annotations)").fetchall()}
    finally:
        conn.close()
    assert cols == {"id", "created_at", "code", "bar_time", "kind", "reason", "source", "context"}


def test_add_list_delete_roundtrip(tmp_path):
    path = _db(tmp_path)
    rec = A.add_annotation(
        "JP.7203", "2026-07-17T09:30:00", "entry_long", "V底で切り返し", "bars1m",
        {"price": 2900.0, "rci9": -95.0}, db_path=path,
    )
    assert rec["id"] >= 1
    assert rec["kind"] == "entry_long"
    assert rec["context"] == {"price": 2900.0, "rci9": -95.0}   # JSON→dict 復元
    assert rec["created_at"]                                    # 自動付与

    lst = A.list_annotations("JP.7203", db_path=path)
    assert len(lst) == 1 and lst[0]["reason"] == "V底で切り返し"

    assert A.delete_annotation(rec["id"], db_path=path) is True
    assert A.list_annotations("JP.7203", db_path=path) == []
    assert A.delete_annotation(rec["id"], db_path=path) is False   # 既に無い


def test_list_filters_by_code_and_orders_desc(tmp_path):
    path = _db(tmp_path)
    A.add_annotation("JP.7203", "2026-07-17T09:30:00", "entry_long", db_path=path)
    A.add_annotation("JP.7203", "2026-07-17T10:00:00", "no_entry", db_path=path)
    A.add_annotation("JP.6758", "2026-07-17T09:31:00", "entry_short", db_path=path)
    all_rows = A.list_annotations(db_path=path)
    assert len(all_rows) == 3
    toyota = A.list_annotations("JP.7203", db_path=path)
    assert [r["bar_time"] for r in toyota] == ["2026-07-17T10:00:00", "2026-07-17T09:30:00"]  # bar_time 降順


def test_attention_kind_accepted(tmp_path):
    """注目（事前アラート）kind='attention' を教師注釈として保存できる（2026-07-19 追加。4 択目）。"""
    path = _db(tmp_path)
    rec = A.add_annotation("JP.7203", "2026-07-17T09:30:00", "attention",
                           "トゲの落差が閾値の7割まで来た", db_path=path)
    assert rec["kind"] == "attention"
    assert A.list_annotations("JP.7203", db_path=path)[0]["kind"] == "attention"


def test_invalid_kind_rejected(tmp_path):
    with pytest.raises(ValueError):
        A.add_annotation("JP.7203", "2026-07-17T09:30:00", "bogus", db_path=_db(tmp_path))


def test_missing_required_fields_rejected(tmp_path):
    path = _db(tmp_path)
    with pytest.raises(ValueError):
        A.add_annotation("", "2026-07-17T09:30:00", "entry_long", db_path=path)
    with pytest.raises(ValueError):
        A.add_annotation("JP.7203", "", "entry_long", db_path=path)


def test_load_annotations_dataframe(tmp_path):
    path = _db(tmp_path)
    empty = A.load_annotations(db_path=path)
    assert list(empty.columns) == ["id", "created_at", "code", "bar_time", "kind", "reason", "source", "context"]
    assert len(empty) == 0
    A.add_annotation("JP.7203", "2026-07-17T09:30:00", "entry_long", "x", "bars1m",
                     {"rci9": -90.0}, db_path=path)
    df = A.load_annotations(db_path=path)
    assert len(df) == 1
    assert df.iloc[0]["code"] == "JP.7203"
    assert df.iloc[0]["context"] == {"rci9": -90.0}   # AI が文脈込みで読める
