"""日本株 1 分足の過去データ一括バックフィル（yfinance）。

背景（2026-07-20 要件）: バックテストに使うのは 1 分足なのでティック録画が無くても
API で過去に遡れる。yfinance の 1 分足は「直近およそ 30 日・1 リクエスト最大 8 日窓」という
制約があるため、期間を 8 日以下（既定 7 日）のチャンクに割って複数回取得し、契約スキーマ
（bars1m・source='yfinance'）で日付ディレクトリへ保存する。これで数営業日分しか無い bars1m を
約 20 営業日分へ広げ、より多くのデータで日本株バックテストを回せるようにする。

冪等性・正データ保護: 既にある (date, code) の bars1m は上書きしない（録画ティック由来＝正）。
何度実行しても新規日付ぶんだけ増える。祝日・休場日は yfinance が空を返すだけなので特別扱いしない。

対象銘柄: config/universe.yaml の fixed + 最新の松井 Tick 回数ランキング上位 + 既存
data/bars1m/*/ に一度でも出現した全 JP 銘柄、の和集合（重複除去）。

実行: python -m scalplab.datafeed.backfill_jp --days 30
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date as date_t, timedelta
from pathlib import Path

import pandas as pd

from .universe import CONFIG_PATH, load_config, moomoo_to_yf
from ..store.loader import bars1m_path
from ..store.writer import DATA_DIR, enforce_bars1m, write_parquet

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
UNIVERSE_DIR = PROJECT_ROOT / "data" / "universe"

# yfinance の 1 分足は 1 リクエスト最大 8 日窓。安全側で 7 日チャンクに割る。
YF_CHUNK_DAYS = 7


# --- チャンク分割 ---

def date_chunks(start: date_t, end: date_t, max_days: int = YF_CHUNK_DAYS) -> list[tuple[date_t, date_t]]:
    """[start, end]（両端含む）を max_days 日以下の連続チャンクへ分割する。

    yfinance の 1 分足は 1 リクエスト最大 8 日窓のため、期間をこの窓以下に割って
    複数回取得する。チャンクは連続・重複なしで全期間を覆う。
    """
    if start > end:
        return []
    chunks: list[tuple[date_t, date_t]] = []
    cs = start
    while cs <= end:
        ce = min(cs + timedelta(days=max_days - 1), end)
        chunks.append((cs, ce))
        cs = ce + timedelta(days=1)
    return chunks


# --- 対象銘柄の解決 ---

def _existing_bars1m_codes(data_root: Path = DATA_DIR) -> set[str]:
    """既存 data/bars1m/*/ に一度でも出現した JP 銘柄コードの集合を返す。"""
    codes: set[str] = set()
    bars_dir = data_root / "bars1m"
    if not bars_dir.exists():
        return codes
    for day_dir in bars_dir.iterdir():
        if not day_dir.is_dir():
            continue
        for p in day_dir.glob("*.parquet"):
            if p.stem.startswith("JP."):
                codes.add(p.stem)
    return codes


def _latest_ranking_codes(top_n: int, uni_dir: Path = UNIVERSE_DIR) -> list[str]:
    """最新の松井 Tick 回数ランキング CSV から上位 top_n の code を返す（無ければ空）。"""
    files = sorted(uni_dir.glob("jp-tick-ranking-*.csv"))
    if not files:
        return []
    df = pd.read_csv(files[-1])
    if "code" not in df.columns:
        return []
    return [str(c) for c in df["code"].head(top_n).tolist() if str(c).startswith("JP.")]


def resolve_backfill_codes(*, config_path: Path = CONFIG_PATH,
                           uni_dir: Path = UNIVERSE_DIR,
                           data_root: Path = DATA_DIR) -> list[str]:
    """バックフィル対象 JP 銘柄（fixed ∪ 最新ランキング上位 ∪ 既存 bars1m 出現分）を返す。"""
    cfg = load_config(config_path)
    fixed = [str(c) for c in cfg.get("fixed", []) if str(c).startswith("JP.")]
    ranking = _latest_ranking_codes(int(cfg.get("auto_top_n", 25)), uni_dir)
    existing = _existing_bars1m_codes(data_root)
    return sorted(set(fixed) | set(ranking) | existing)


# --- yfinance 取得 → 契約スキーマ変換 ---

def _yf_fetcher(ticker: str, start: str, end: str) -> pd.DataFrame:
    """yfinance で 1 分足を取得する既定フェッチャ（end は排他日なので呼び出し側で +1 日）。"""
    import yfinance as yf  # type: ignore[import-not-found]

    return yf.Ticker(ticker).history(start=start, end=end, interval="1m", auto_adjust=False)


def _to_contract_df(hist: pd.DataFrame) -> pd.DataFrame:
    """yfinance history の返却を契約スキーマ列（JST naive time・source='yfinance'）へ整形する。

    index は取引所 tz-aware。JST naive に落とす（record.fetch_fallback_bars と同一基準）。
    dtype/列順の最終強制は enforce_bars1m に委ねる。
    """
    idx = hist.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("Asia/Tokyo").tz_localize(None)
    return pd.DataFrame({
        "time": idx,
        "open": hist["Open"].to_numpy(),
        "high": hist["High"].to_numpy(),
        "low": hist["Low"].to_numpy(),
        "close": hist["Close"].to_numpy(),
        "volume": hist["Volume"].to_numpy(),
        "turnover": (hist["Close"] * hist["Volume"]).to_numpy(),
        "source": "yfinance",
    })


# --- 1 銘柄分のバックフィル ---

def backfill_code(code: str, chunks: list[tuple[date_t, date_t]], *,
                  fetcher=_yf_fetcher, data_root: Path = DATA_DIR,
                  throttle_sec: float = 1.0) -> tuple[int, int]:
    """1 銘柄をチャンク毎に取得し、日付別に bars1m を保存する。

    返り値 (written_days, skipped_days)。既存 (date, code) は上書きしない（正データ保護）。
    """
    ticker = moomoo_to_yf(code)
    frames: list[pd.DataFrame] = []
    for i, (cs, ce) in enumerate(chunks):
        if i and throttle_sec:
            time.sleep(throttle_sec)
        try:
            # yfinance の end は排他なので最終日を含めるため +1 日する。
            hist = fetcher(ticker, cs.isoformat(), (ce + timedelta(days=1)).isoformat())
        except Exception as e:  # noqa: BLE001 - 銘柄単位でスキップし全体は止めない
            log.warning("yfinance 取得失敗 %s [%s..%s]: %s", code, cs, ce, e)
            continue
        if hist is None or hist.empty:
            continue
        frames.append(_to_contract_df(hist))

    if not frames:
        return 0, 0

    allbars = pd.concat(frames, ignore_index=True).dropna(subset=["time"])
    allbars["_date"] = pd.to_datetime(allbars["time"]).dt.strftime("%Y-%m-%d")

    written = skipped = 0
    for d, g in allbars.groupby("_date"):
        path = bars1m_path(str(d), code, root=data_root)
        if path.exists():
            skipped += 1  # 録画ティック由来など既存データは上書きしない
            continue
        df = (g.drop(columns="_date")
                .sort_values("time")
                .drop_duplicates(subset="time", keep="last"))
        write_parquet(enforce_bars1m(df), path)
        written += 1
    return written, skipped


# --- 全体オーケストレーション ---

def run_backfill(days: int = 30, *, fetcher=None, data_root: Path = DATA_DIR,
                 config_path: Path = CONFIG_PATH, uni_dir: Path = UNIVERSE_DIR,
                 throttle_sec: float = 1.0, today: date_t | None = None) -> dict:
    """対象 JP 銘柄群を過去 days 日ぶん yfinance からバックフィルする。

    返り値サマリ: {codes, days_span, written, skipped, errors, dates}
    （dates は bars1m を持つ全営業日の昇順リスト＝walkforward 種まきの対象。
    冪等なので既存日を含めても再計算はスキップされる）。
    """
    fetcher = fetcher or _yf_fetcher
    today = today or date_t.today()
    start = today - timedelta(days=days)
    chunks = date_chunks(start, today)
    codes = resolve_backfill_codes(config_path=config_path, uni_dir=uni_dir, data_root=data_root)

    log.info("バックフィル開始: %d 銘柄 / 期間 %s..%s / %d チャンク",
             len(codes), start, today, len(chunks))

    total_written = total_skipped = errors = 0
    dates_written: set[str] = set()
    for idx, code in enumerate(codes):
        if idx and throttle_sec:
            time.sleep(throttle_sec)
        try:
            w, s = backfill_code(code, chunks, fetcher=fetcher,
                                 data_root=data_root, throttle_sec=throttle_sec)
        except Exception as e:  # noqa: BLE001 - 1 銘柄の失敗で全体を止めない
            log.warning("銘柄バックフィル失敗 %s: %s", code, e)
            errors += 1
            continue
        total_written += w
        total_skipped += s
        log.info("[%d/%d] %s: 新規 %d 日 / スキップ %d 日", idx + 1, len(codes), code, w, s)

    # bars1m を持つ営業日を data/bars1m から走査して確定（walkforward 種まき対象）。
    for day_dir in sorted((data_root / "bars1m").glob("*")):
        if day_dir.is_dir():
            dates_written.add(day_dir.name)

    summary = {
        "codes": len(codes),
        "days_span": (start.isoformat(), today.isoformat()),
        "written": total_written,
        "skipped": total_skipped,
        "errors": errors,
        "dates": sorted(dates_written),
    }
    log.info("バックフィル完了: 新規 %d (code,date) / スキップ %d / エラー %d 銘柄 / 営業日 %d",
             total_written, total_skipped, errors, len(summary["dates"]))
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="日本株 1 分足の過去データ一括バックフィル（yfinance）")
    ap.add_argument("--days", type=int, default=30,
                    help="今日から遡る日数（yfinance 1分足は直近約30日が上限）")
    ap.add_argument("--throttle", type=float, default=1.0,
                    help="リクエスト間の待機秒（レート配慮）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    res = run_backfill(days=args.days, throttle_sec=args.throttle)
    span = res["days_span"]
    print(f"=== backfill-jp {span[0]}..{span[1]} ===")
    print(f"対象銘柄: {res['codes']} / 新規保存: {res['written']} (code,date) / "
          f"既存スキップ: {res['skipped']} / エラー銘柄: {res['errors']}")
    print(f"bars1m を持つ営業日: {len(res['dates'])} 日 -> {', '.join(res['dates'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
