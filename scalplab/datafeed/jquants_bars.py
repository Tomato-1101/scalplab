"""J-Quants API（v2）株価分足（1 分足）の取得・保存。

背景（2026-07-22 要件）: これまで JP の 1 分足は yfinance フォールバック（直近およそ
30 日制限）で埋めていた。J-Quants が 2026-01-19 から公式の株価分足（過去 2 年・Light プラン以上
＋アドオン）を提供開始したので、契約済みなら公式データで bars1m を置き換えられる。ただしユーザーは
まだ未契約で **API キーが無い状態が既定**であるため、本モジュールは「キーが無ければ全経路が無害に
スキップして正常終了する」ことを絶対条件にする（録画・walkforward・録画後始末に一切波及させない）。

タイムスタンプ規約（重要・仮定）:
  レスポンスの Time は **バー開始時刻**とみなして Date と結合し、JST naive Timestamp（tz 情報なし）で
  time 列に入れる（既存 bars1m と同一基準。moomoo/yfinance 由来のバーもバー開始時刻）。J-Quants の
  Time がバー終了/中央時刻だった場合はここを直せば全体が追従する（実キーが無いため実データ未確認）。

レスポンス配列キーの頑健化:
  バー配列を持つキー名は未確認のため、レスポンス dict の値のうち **最初に現れる list** をバー配列として
  拾う（`pagination_key` は文字列なので誤検出しない）。`pagination_key` があれば次リクエストに付けて
  最大 ``MAX_PAGES_PER_CALL`` 回までページ送りする（暴走防止の安全上限）。

上書きポリシー（正データ保護）:
  bars1m が無ければ書く。あれば parquet の source 列を見て 'yfinance'（フォールバック）のときだけ
  公式データで置き換える。'ticks'（録画由来）等は絶対に上書きしない。判定は純関数
  ``should_write_bars`` に切り出してテストする。

実行:
  python -m scalplab.datafeed.jquants_bars --daily [--top N]
  python -m scalplab.datafeed.jquants_bars --backfill [--top N] [--codes JP.XXXX ...] [--from YYYYMMDD]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import date as date_t, datetime, timedelta
from pathlib import Path

import pandas as pd

from ..store.loader import bars1m_path
from ..store.writer import BARS1M_COLUMNS, DATA_DIR, enforce_bars1m, write_parquet
from .backfill_jp import date_chunks
from .jp_tick_ranking import RANKING_DIR, latest_saved_csv, load_ranking_csv

log = logging.getLogger(__name__)

API_URL = "https://api.jquants.com/v2/equities/bars/minute"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) scalp-lab/1.0"

ENV_KEY = "JQUANTS_API_KEY"                 # 探索①: 環境変数
KEYCHAIN_SERVICE = "jquants-api-key"        # 探索②: macOS Keychain のサービス名

DEFAULT_TIMEOUT = 15.0    # 1 リクエストのタイムアウト秒（録画後始末を絶対にハングさせない）
DEFAULT_RETRIES = 1       # ネットワーク系の再試行回数
MAX_PAGES_PER_CALL = 50   # 1 回の from/to 取得でのページ送り上限（暴走防止）
BACKFILL_CHUNK_DAYS = 30  # backfill の銘柄×日チャンク幅
BACKFILL_YEARS_DAYS = 730  # backfill の既定遡り日数（過去 2 年）
DAILY_LOOKBACK_DAYS = 4   # daily は直近 4 暦日〜今日（T+1 公開ラグ・週末を吸収）
REQUEST_THROTTLE_SEC = 0.3  # リクエスト間の待機秒（レート配慮）


# ---------------- API キー探索 ----------------

def find_api_key() -> str | None:
    """API キーを ①環境変数 ②macOS Keychain の順に探す。無ければ None。

    ②は ``security find-generic-password -s jquants-api-key -w`` を timeout 付き subprocess で叩く。
    security 不在・タイムアウト・未登録などは全て「鍵無し」（None）として握る。キーの中身はログに出さない。
    """
    env = os.environ.get(ENV_KEY)
    if env and env.strip():
        return env.strip()
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5.0,
        )
    except Exception:  # noqa: BLE001 - security 不在/タイムアウト等は鍵無し扱い
        return None
    if proc.returncode == 0:
        key = proc.stdout.strip()
        if key:
            return key
    return None


# ---------------- コード変換 ----------------

def _to_jq_code(code: str) -> str:
    """moomoo 形式コードを J-Quants の裸コードへ（"JP.6981" → "6981"）。

    "JP." 接頭辞を剥がすだけ。既に裸（"6981"）ならそのまま。英字入り 4 桁（"285A"）も 4 桁のまま渡す。
    """
    return code[3:] if code.startswith("JP.") else code


# ---------------- レスポンス正規化 ----------------

def _extract_bar_list(resp: dict) -> list:
    """レスポンス dict からバー配列を頑健に拾う（値のうち最初の list を返す）。

    バー配列のキー名は未確認のため名前に依存しない。`pagination_key` 等の文字列は list ではないので
    誤検出しない。dict でない/ list 値が無い場合は空リスト。
    """
    if not isinstance(resp, dict):
        return []
    for v in resp.values():
        if isinstance(v, list):
            return v
    return []


def _combine_datetime(date_val, time_val) -> str:
    """Date と Time を JST naive 用の "YYYY-MM-DD HH:MM:SS" 文字列へ結合する。

    実データ未確認のため ISO（"2026-07-15" / "09:00:00"）とコンパクト（"20260715" / "0900"）の
    双方を受ける。最終的な dtype 強制（datetime64[ns]）は enforce_bars1m の _coerce_time に委ねる。
    """
    d = str(date_val).strip()
    if len(d) == 8 and d.isdigit():  # 20260715 → 2026-07-15
        d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    t = str(time_val).strip()
    if t.isdigit():                  # 900/0900 → 09:00:00, 090000 → 09:00:00
        t = t.zfill(4)
        t = f"{t[:2]}:{t[2:4]}:00" if len(t) == 4 else f"{t[:2]}:{t[2:4]}:{t[4:6]}"
    return f"{d} {t}"


def normalize_bars(bars: list[dict]) -> pd.DataFrame:
    """J-Quants の分足バー配列を bars1m 契約スキーマ（未強制）へ正規化する。

    入力フィールド: Date, Time, Code, O(始値), H(高値), L(安値), C(終値), Vo(出来高), Va(売買代金)。
    出力列: time(Date+Time の結合文字列。バー開始時刻の仮定＝モジュール docstring 参照),
            open, high, low, close, volume, turnover(=Va), source='jquants'。
    Vo/Va は欠損時 0 とする（それ以外の OHLC/Date/Time はスキーマ前提として直接参照）。
    """
    if not bars:
        return pd.DataFrame({c: [] for c in BARS1M_COLUMNS})
    rows = []
    for b in bars:
        rows.append({
            "time": _combine_datetime(b["Date"], b["Time"]),
            "open": b["O"],
            "high": b["H"],
            "low": b["L"],
            "close": b["C"],
            "volume": b.get("Vo", 0) or 0,
            "turnover": b.get("Va", 0.0) or 0.0,
            "source": "jquants",
        })
    return pd.DataFrame(rows, columns=BARS1M_COLUMNS)


def split_by_date(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """正規化済み DataFrame を日付（YYYY-MM-DD）ごとに分割して返す。

    ページ送りで順不同・重複が混じりうるので、time で昇順ソートし同一 time は後勝ちで除去する。
    """
    if df.empty:
        return {}
    ts = pd.to_datetime(df["time"])
    tagged = (df.assign(_t=ts, _d=ts.dt.strftime("%Y-%m-%d"))
                .sort_values("_t")
                .drop_duplicates(subset="_t", keep="last"))
    out: dict[str, pd.DataFrame] = {}
    for d, g in tagged.groupby("_d", sort=True):
        out[str(d)] = g.drop(columns=["_t", "_d"]).reset_index(drop=True)
    return out


# ---------------- HTTP ----------------

def _http_get_json(params: dict, api_key: str | None, timeout: float, retries: int) -> dict:
    """API へ GET し JSON を dict で返す（x-api-key ヘッダ・タイムアウト・再試行付き）。"""
    url = API_URL + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001 - ネットワーク系は種類を問わず再試行/送出
            last_err = e
            log.warning("J-Quants 分足取得失敗（試行%d）: %s", attempt + 1, e)
    raise last_err if last_err else RuntimeError("fetch failed")


def fetch_bars(code: str, date_from: str, date_to: str, *,
               api_key: str | None = None, request_fn=None,
               timeout: float = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES,
               max_requests: int = MAX_PAGES_PER_CALL,
               throttle_sec: float = 0.0) -> dict[str, pd.DataFrame]:
    """code と from/to（YYYYMMDD）を受け、ページ送りして全バーを日付別 DataFrame にする。

    request_fn（params dict → レスポンス dict）を注入するとテストで HTTP をモックできる。省略時は
    api_key を使った実 HTTP。安全上限 max_requests に達したら打ち切る（暴走防止）。
    """
    if request_fn is None:
        def request_fn(params, _key=api_key):
            return _http_get_json(params, _key, timeout, retries)

    base = {"code": _to_jq_code(code), "from": date_from, "to": date_to}
    params = dict(base)
    all_bars: list[dict] = []
    for _ in range(max_requests):
        resp = request_fn(params)
        all_bars.extend(_extract_bar_list(resp))
        pk = resp.get("pagination_key") if isinstance(resp, dict) else None
        if not pk:
            break
        params = {**base, "pagination_key": pk}
        if throttle_sec:
            time.sleep(throttle_sec)
    else:
        log.warning("J-Quants 分足: ページ送り上限 %d に到達（code=%s %s..%s）",
                    max_requests, code, date_from, date_to)
    return split_by_date(normalize_bars(all_bars))


# ---------------- 上書きポリシー・保存 ----------------

def should_write_bars(path_exists: bool, existing_source: str | None) -> bool:
    """bars1m を J-Quants データで書いてよいか判定する純関数（正データ保護）。

    - ファイル無し（path_exists=False）→ True（新規書き込み）
    - 'yfinance'（フォールバック）→ True（公式データ優先で置換）
    - それ以外（'ticks' 等）／既存だが source 不明（None）→ False（絶対に上書きしない）
    """
    if not path_exists:
        return True
    return existing_source == "yfinance"


def _existing_source(path: Path) -> str | None:
    """既存 parquet の source 列先頭値を返す。読めない/空/欠損は None。"""
    try:
        df = pd.read_parquet(path, columns=["source"])
    except Exception:  # noqa: BLE001 - 読めない parquet は上書き対象にしない（None→触らない側へ）
        return None
    if df.empty:
        return None
    val = df["source"].iloc[0]
    return None if pd.isna(val) else str(val)


def save_bars_for_date(date_str: str, code: str, df: pd.DataFrame, *,
                       data_root: Path = DATA_DIR) -> bool:
    """1 日ぶんの bars1m を上書きポリシーに従って保存する。書いたら True。"""
    path = bars1m_path(date_str, code, root=data_root)
    exists = path.exists()
    source = _existing_source(path) if exists else None
    if not should_write_bars(exists, source):
        return False
    write_parquet(enforce_bars1m(df), path)
    return True


# ---------------- 1 銘柄処理 ----------------

def _process_code(code: str, start: date_t, end: date_t, *,
                  api_key: str | None, request_fn, data_root: Path,
                  throttle_sec: float) -> dict:
    """1 銘柄を 30 日チャンクで取得し、日付別に保存する。返り値 {written, skipped}。

    銘柄あたりの実リクエスト数はチャンク数（期間から確定）× 各チャンクのページ数（MAX_PAGES_PER_CALL
    で上限）で有界。冪等（上書きポリシー準拠）なので何度実行しても新規/置換対象ぶんだけ書く。
    """
    written = skipped = 0
    chunks = date_chunks(start, end, max_days=BACKFILL_CHUNK_DAYS)
    for i, (cs, ce) in enumerate(chunks):
        if i and throttle_sec:
            time.sleep(throttle_sec)
        by_date = fetch_bars(code, cs.strftime("%Y%m%d"), ce.strftime("%Y%m%d"),
                             api_key=api_key, request_fn=request_fn, throttle_sec=throttle_sec)
        for d, df in by_date.items():
            if save_bars_for_date(d, code, df, data_root=data_root):
                written += 1
            else:
                skipped += 1
    return {"written": written, "skipped": skipped}


# ---------------- 対象銘柄 ----------------

def _default_codes(top_n: int, uni_dir: Path = RANKING_DIR) -> list[str]:
    """松井 Tick 回数ランキング最新 CSV の上位 top_n 銘柄コードを返す（無ければ空）。"""
    path = latest_saved_csv(uni_dir)
    if path is None:
        return []
    rows = load_ranking_csv(path)
    return [r["code"] for r in rows[:top_n]]


# ---------------- オーケストレーション ----------------

def _run_codes(codes: list[str], start: date_t, end: date_t, *,
               api_key: str | None, request_fn, data_root: Path,
               throttle_sec: float) -> dict:
    """複数銘柄を順に処理し、銘柄ごとに進捗 1 行ログを出す。1 銘柄の失敗で全体を止めない。"""
    total_written = total_skipped = errors = 0
    dates: set[str] = set()
    for idx, code in enumerate(codes):
        if idx and throttle_sec:
            time.sleep(throttle_sec)
        try:
            r = _process_code(code, start, end, api_key=api_key, request_fn=request_fn,
                              data_root=data_root, throttle_sec=throttle_sec)
        except Exception as e:  # noqa: BLE001 - 1 銘柄の失敗で全体を止めない
            log.warning("J-Quants 分足取り込み失敗 %s: %s", code, e)
            errors += 1
            continue
        total_written += r["written"]
        total_skipped += r["skipped"]
        log.info("[%d/%d] %s: 新規 %d 日 / スキップ %d 日", idx + 1, len(codes), code,
                 r["written"], r["skipped"])
    return {
        "written": total_written,
        "skipped_days": total_skipped,
        "errors": errors,
        "codes": len(codes),
        "span": (start.isoformat(), end.isoformat()),
        "skipped": None,
    }


def _resolve_key_or_skip(request_fn, api_key: str | None, what: str) -> tuple[str | None, dict | None]:
    """実 HTTP 時のみキーを解決し、無ければスキップ用サマリを返す。

    request_fn が注入されている（テスト等）ときはキー解決を省く。戻り値 (api_key, skip_summary)。
    skip_summary が非 None なら呼び手はそれを返して終了する。
    """
    if request_fn is not None:
        return api_key, None
    key = api_key or find_api_key()
    if key is None:
        log.info("J-Quants 分足: API キー未設定のため%sをスキップ"
                 "（環境変数 %s か Keychain '%s' を設定すると有効）", what, ENV_KEY, KEYCHAIN_SERVICE)
        return None, {"written": 0, "skipped_days": 0, "errors": 0, "codes": 0,
                      "skipped": "no_api_key"}
    return key, None


def run_daily(*, top_n: int = 10, request_fn=None, api_key: str | None = None,
             data_root: Path = DATA_DIR, uni_dir: Path = RANKING_DIR,
             today: date_t | None = None, throttle_sec: float = REQUEST_THROTTLE_SEC) -> dict:
    """直近 DAILY_LOOKBACK_DAYS 暦日〜今日を上位 top_n 銘柄で取り込む（日次フック用）。

    API キー未設定なら 1 行 INFO でスキップし {"skipped": "no_api_key"} を返す（何も書かない・正常終了）。
    """
    key, skip = _resolve_key_or_skip(request_fn, api_key, "日次取り込み")
    if skip is not None:
        return skip
    today = today or date_t.today()
    start = today - timedelta(days=DAILY_LOOKBACK_DAYS)
    codes = _default_codes(top_n, uni_dir)
    log.info("J-Quants 分足 日次取り込み: %d 銘柄 / 期間 %s..%s", len(codes), start, today)
    return _run_codes(codes, start, today, api_key=key, request_fn=request_fn,
                      data_root=data_root, throttle_sec=throttle_sec)


def run_backfill(*, top_n: int = 10, codes: list[str] | None = None,
                date_from: str | None = None, request_fn=None, api_key: str | None = None,
                data_root: Path = DATA_DIR, uni_dir: Path = RANKING_DIR,
                today: date_t | None = None, throttle_sec: float = REQUEST_THROTTLE_SEC) -> dict:
    """上位 top_n（または明示 codes）を既定 2 年前〜今日、30 日チャンクで一括取り込む。

    API キー未設定なら 1 行 INFO でスキップ（何も書かない・正常終了）。冪等（上書きポリシー準拠）。
    """
    key, skip = _resolve_key_or_skip(request_fn, api_key, "バックフィル")
    if skip is not None:
        return skip
    today = today or date_t.today()
    if date_from:
        start = datetime.strptime(date_from, "%Y%m%d").date()
    else:
        start = today - timedelta(days=BACKFILL_YEARS_DAYS)
    codes = codes if codes else _default_codes(top_n, uni_dir)
    log.info("J-Quants 分足 バックフィル: %d 銘柄 / 期間 %s..%s / チャンク幅 %d 日",
             len(codes), start, today, BACKFILL_CHUNK_DAYS)
    return _run_codes(codes, start, today, api_key=key, request_fn=request_fn,
                      data_root=data_root, throttle_sec=throttle_sec)


# ---------------- CLI ----------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="J-Quants API（v2）株価分足（1 分足）の取得・保存")
    ap.add_argument("--backfill", action="store_true", help="過去分を一括バックフィル")
    ap.add_argument("--daily", action="store_true", help="直近数日ぶんの日次取り込み")
    ap.add_argument("--top", type=int, default=10, help="対象銘柄数（松井 Tick ランキング上位 N。既定 10）")
    ap.add_argument("--codes", nargs="+", default=None, help="対象銘柄を明示指定（例 JP.6981 JP.285A。backfill のみ）")
    ap.add_argument("--from", dest="date_from", default=None,
                    help="バックフィル開始日 YYYYMMDD（既定は 2 年前）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.daily:
        res = run_daily(top_n=args.top)
    elif args.backfill:
        res = run_backfill(top_n=args.top, codes=args.codes, date_from=args.date_from)
    else:
        ap.print_help()
        return 0

    if res.get("skipped"):
        print(f"=== jquants-bars: スキップ（{res['skipped']}）===")
        return 0
    print("=== jquants-bars ===")
    print(f"対象銘柄: {res['codes']} / 新規保存: {res['written']} 日 / "
          f"既存スキップ: {res['skipped_days']} 日 / エラー銘柄: {res['errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
