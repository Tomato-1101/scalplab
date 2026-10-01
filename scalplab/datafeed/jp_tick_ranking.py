"""松井証券 Tick回数ランキングの取得・保存（日本株の録画対象選定とサイドバー並び順の元データ）。

なぜネットから取るのか:
  ティック回数（＝約定回数）は本来「その日どれだけ約定が入ったか」であり、板情報を数えないと
  出せない。従来の自前ランキングは yfinance の売買代金プロキシ（Close×Volume）で代用していたが、
  これは約定の多寡そのものではない。松井証券が未ログインで公開している Tick回数ランキングを
  そのまま使えば、当日の板の活発さを反映した本物の順位が得られる（ユーザー要件: 自分で計算せず
  ネットから取る）。

取得先: https://finance.matsui.co.jp/ranking-tick/index
  - 50 件/ページ、``?page=2`` で 51〜100 位。市場フィルタ ``?market`` は既定（市場全体）を使う。
  - 依存を増やさないため標準ライブラリ（urllib + 正規表現）だけで取得・パースする。

冪等性:
  当日ぶんの CSV（``data/universe/jp-tick-ranking-YYYY-MM-DD.csv``）が既にあればフェッチせず
  再利用する。取得失敗時は「直近の保存済み CSV」へフォールバックし、それも無ければ空を返す
  （呼び手＝universe.build が売買代金プロキシへさらにフォールバックする）。どのソースを使ったかは
  必ずログに残す。
"""
from __future__ import annotations

import csv
import logging
import re
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RANKING_DIR = PROJECT_ROOT / "data" / "universe"

MATSUI_RANKING_URL = "https://finance.matsui.co.jp/ranking-tick/index"
# ブラウザ相当の UA を付けないと弾かれる可能性があるため明示する。
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) scalp-lab/1.0"

DEFAULT_TIMEOUT = 15.0   # 1 リクエストのタイムアウト秒（録画起動を絶対にハングさせない）
DEFAULT_PAGES = (1, 2)   # 上位 100 件（50 件/ページ）

# ランキング本体テーブル（ページ内には別用途の /stock/ リンクも多数あるため、この表に限定する）。
_TABLE_RE = re.compile(r'<table[^>]*data-type="rankingTk"[^>]*>(.*?)</table>', re.S)
_ROW_RE = re.compile(r"<tr\b.*?</tr>", re.S)
# href は実測で属性値が引用符なし（<a href=/stock/285A/index>）。両対応にする。
_CODE_RE = re.compile(r'/stock/([0-9A-Za-z]+)/index')
_NAME_RE = re.compile(r'/stock/[0-9A-Za-z]+/index[^>]*>\s*([^<]+?)\s*</a>')
_RANK_RE = re.compile(r"<td[^>]*>\s*(\d+)\s*</td>")
# 約定回数：</span>205,097 の数値（カンマ込み）。
_TICK_RE = re.compile(r"約定回数[：:]\s*</span>\s*([\d,]+)")


def _to_moomoo_code(raw: str) -> str:
    """松井のコード（285A / 9984）を moomoo コード（JP.285A）に変換する。"""
    return f"JP.{raw}"


def parse_ranking_html(html: str) -> list[dict]:
    """ランキング HTML 1 ページ分を [{rank, code, name, tick_count}, ...] にパースする。

    - ``data-type="rankingTk"`` のテーブル内の行だけを対象にする（ページ内の他の /stock/ リンクを拾わない）。
    - code は href の ``/stock/<code>/index`` から取り、``JP.`` を前置する（本プロジェクトの正規形）。
    - 順位は行頭 ``<td>`` の数値（page2 は 51〜100 が入るので絶対順位になる）。
    - 約定回数はカンマを除いた整数。name・code・tick のいずれかが取れない行は捨てる（壊れた行で全体を落とさない）。
    """
    m = _TABLE_RE.search(html)
    if not m:
        return []
    body = m.group(1)
    out: list[dict] = []
    for row in _ROW_RE.findall(body):
        if "<th" in row:  # ヘッダ行はスキップ
            continue
        code_m = _CODE_RE.search(row)
        name_m = _NAME_RE.search(row)
        tick_m = _TICK_RE.search(row)
        if not (code_m and name_m and tick_m):
            continue
        rank_m = _RANK_RE.search(row)
        rank = int(rank_m.group(1)) if rank_m else len(out) + 1
        out.append({
            "rank": rank,
            "code": _to_moomoo_code(code_m.group(1)),
            "name": name_m.group(1).strip(),
            "tick_count": int(tick_m.group(1).replace(",", "")),
        })
    return out


def _fetch_page(page: int, timeout: float, retries: int) -> str:
    """1 ページ分の HTML を取得する（UA・タイムアウト付き、失敗時 retries 回まで再試行）。"""
    url = MATSUI_RANKING_URL if page <= 1 else f"{MATSUI_RANKING_URL}?page={page}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - ネットワーク系は種類を問わず再試行/フォールバック
            last_err = e
            log.warning("Tick回数ランキング取得失敗 (page=%d, 試行%d): %s", page, attempt + 1, e)
    raise last_err if last_err else RuntimeError("fetch failed")


def fetch_ranking(pages: tuple[int, ...] = DEFAULT_PAGES,
                  timeout: float = DEFAULT_TIMEOUT, retries: int = 1) -> list[dict]:
    """指定ページを取得・パースして上位ランキング（最大 100 件）を返す。

    1 ページでも取得に失敗したら例外を送出する（呼び手＝get_ranking がフォールバックを判断する）。
    """
    rows: list[dict] = []
    for page in pages:
        rows.extend(parse_ranking_html(_fetch_page(page, timeout, retries)))
    return rows


# --- 保存・読み込み ---

def ranking_csv_path(date: str, ranking_dir: Path | None = None) -> Path:
    """その日のランキング CSV パス（data/universe/jp-tick-ranking-YYYY-MM-DD.csv）。"""
    d = ranking_dir if ranking_dir is not None else RANKING_DIR
    return d / f"jp-tick-ranking-{date}.csv"


def save_ranking(rows: list[dict], date: str, ranking_dir: Path | None = None) -> Path:
    """ランキングを CSV（rank,code,name,tick_count）に保存する。"""
    path = ranking_csv_path(date, ranking_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "code", "name", "tick_count"])
        for r in rows:
            w.writerow([r["rank"], r["code"], r["name"], r["tick_count"]])
    return path


def load_ranking_csv(path: Path) -> list[dict]:
    """保存済み CSV を読み、[{rank, code, name, tick_count}, ...]（順位昇順のまま）を返す。

    壊れた行・数値化できない行はスキップする（フォールバック源が壊れていても落とさない）。
    """
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            code = (row.get("code") or "").strip()
            if not code:
                continue
            try:
                out.append({
                    "rank": int(row["rank"]),
                    "code": code,
                    "name": (row.get("name") or "").strip(),
                    "tick_count": int(row["tick_count"]),
                })
            except (KeyError, ValueError, TypeError):
                continue
    return out


def latest_saved_csv(ranking_dir: Path | None = None) -> Path | None:
    """保存済みランキング CSV のうち最新（日付が最大）のパスを返す。無ければ None。"""
    d = ranking_dir if ranking_dir is not None else RANKING_DIR
    if not d.exists():
        return None
    files = sorted(d.glob("jp-tick-ranking-*.csv"))
    return files[-1] if files else None


def get_ranking(date: str, ranking_dir: Path | None = None,
                timeout: float = DEFAULT_TIMEOUT, retries: int = 1) -> tuple[list[dict], str]:
    """その日の Tick回数ランキングを取得する。戻り値は (rows, source)。

    source は使ったソースを表す:
      - "cached_today"   … 当日 CSV を再利用（冪等・フェッチせず）
      - "fetched"        … 松井からフェッチして保存
      - "saved_previous" … フェッチ失敗 → 直近保存済み CSV を使用
      - "unavailable"    … いずれも不可（rows は空。呼び手が売買代金プロキシへ）
    """
    csv_path = ranking_csv_path(date, ranking_dir)
    if csv_path.exists():
        rows = load_ranking_csv(csv_path)
        if rows:
            log.info("Tick回数ランキング: 当日キャッシュを再利用（%s, %d件）", csv_path.name, len(rows))
            return rows, "cached_today"

    try:
        rows = fetch_ranking(timeout=timeout, retries=retries)
    except Exception as e:  # noqa: BLE001 - 取得不可は下でフォールバック
        rows = []
        log.warning("Tick回数ランキングの取得に失敗: %s", e)
    if rows:
        save_ranking(rows, date, ranking_dir)
        log.info("Tick回数ランキングを取得・保存（%s, %d件）", csv_path.name, len(rows))
        return rows, "fetched"

    prev = latest_saved_csv(ranking_dir)
    if prev is not None and prev != csv_path:
        prows = load_ranking_csv(prev)
        if prows:
            log.warning("Tick回数ランキング: 取得不可のため直近保存を使用（%s, %d件）", prev.name, len(prows))
            return prows, "saved_previous"

    log.warning("Tick回数ランキング: 取得も保存も無し（売買代金プロキシへフォールバック）")
    return [], "unavailable"
