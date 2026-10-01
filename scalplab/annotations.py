"""エントリー注釈の保存・読み出し（ユーザーが手法を教えるための教師データ）。

なぜ:
  ユーザーはチャート上で「ここでエントリー（ロング/ショート）」「ここは入っちゃダメ」を
  理由つきで記録したい。これを溜めれば、AI（Claude）が「その時点の RCI 等の文脈」と一緒に
  読んで、ユーザーのエントリー手法を学べる。特別な DB を足さず標準ライブラリ sqlite3 だけで
  実装する（依存追加なし・ファイル1つで持ち運べる・AI がそのまま読める）。

保存先: data/annotations.sqlite3（個人の教師データなのでコミットしない＝.gitignore 済み）。
テーブル annotations:
  id INTEGER PRIMARY KEY AUTOINCREMENT
  created_at TEXT   -- 記録した日時（JST ISO）
  code TEXT         -- 銘柄コード（例 JP.7203）
  bar_time TEXT     -- クリックしたバーの時刻（JST ISO・市場ローカル壁時計）
  kind TEXT         -- 'entry_long' | 'entry_short' | 'no_entry' | 'attention'
  reason TEXT       -- ユーザーが書いた理由（任意）
  source TEXT       -- データソース（bars1m / ticks / hist1m 等。文脈のため）
  context TEXT      -- 保存時にサーバが採取した指標スナップショット（JSON 文字列）
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "annotations.sqlite3"

# 許可する注釈種別（UI の4択と一致）。これ以外は弾く（データ品質のため）。
# attention=「注目すべき（このタイミングで事前アラートを出してほしい）」の正解ラベル（2026-07-19 追加）。
VALID_KINDS = ("entry_long", "entry_short", "no_entry", "attention")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS annotations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  code TEXT NOT NULL,
  bar_time TEXT NOT NULL,
  kind TEXT NOT NULL,
  reason TEXT,
  source TEXT,
  context TEXT
);
CREATE INDEX IF NOT EXISTS idx_annotations_code ON annotations(code);
"""


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    """DB に接続しスキーマを保証する（ファイル・親ディレクトリが無ければ作る）。

    リクエストごと（＝スレッドごと）に開いて閉じる前提。conn.row_factory で dict 風に読む。
    """
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    """DB 行を API/DataFrame 向けの dict にする（context は JSON→dict に復元）。"""
    d = dict(row)
    raw = d.get("context")
    try:
        d["context"] = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        d["context"] = {}
    return d


def add_annotation(
    code: str,
    bar_time: str,
    kind: str,
    reason: str = "",
    source: str = "",
    context: dict | None = None,
    created_at: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """注釈を1件保存し、作成した行（id 付き dict）を返す。

    kind は VALID_KINDS のいずれか（不正は ValueError→呼び手が 400 にする）。created_at 未指定は
    現在時刻（ローカル＝JST の ISO）。context は指標スナップショット dict（JSON 文字列で保存）。
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind は {VALID_KINDS} のいずれかです: {kind!r}")
    if not code:
        raise ValueError("code は必須です")
    if not bar_time:
        raise ValueError("bar_time は必須です")
    created = created_at or datetime.now().isoformat(timespec="seconds")
    ctx_json = json.dumps(context or {}, ensure_ascii=False)
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO annotations (created_at, code, bar_time, kind, reason, source, context)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (created, code, bar_time, kind, reason or "", source or "", ctx_json),
        )
        conn.commit()
        new_id = cur.lastrowid
        row = conn.execute("SELECT * FROM annotations WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row)


def list_annotations(code: str | None = None, db_path: Path | None = None) -> list[dict]:
    """注釈を新しい順（bar_time 降順）で返す。code 指定でその銘柄だけに絞る。"""
    conn = _connect(db_path)
    try:
        if code:
            rows = conn.execute(
                "SELECT * FROM annotations WHERE code = ? ORDER BY bar_time DESC, id DESC", (code,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM annotations ORDER BY bar_time DESC, id DESC"
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def delete_annotation(annotation_id: int, db_path: Path | None = None) -> bool:
    """id 指定で1件削除する。削除できたら True（存在しなければ False）。"""
    conn = _connect(db_path)
    try:
        cur = conn.execute("DELETE FROM annotations WHERE id = ?", (int(annotation_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def load_annotations(db_path: Path | None = None) -> pd.DataFrame:
    """全注釈を DataFrame で返す（AI が手法を学ぶための可読 API）。

    列は id / created_at / code / bar_time / kind / reason / source / context（dict）。
    context には保存時点の指標スナップショット（price, rci9, rci14, sar, atr, sma5, sma25 等）が入る。
    データが無ければ同じ列を持つ空 DataFrame を返す。
    """
    cols = ["id", "created_at", "code", "bar_time", "kind", "reason", "source", "context"]
    recs = list_annotations(db_path=db_path)
    if not recs:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(recs, columns=cols)
