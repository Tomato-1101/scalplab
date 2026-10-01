"""moomoo の過去 1 分足を取得しディスクキャッシュする（クロス銘柄スクリーニングのデータ供給）。

なぜキャッシュ必須か（クォータ・レート制約）:
  過去 K ライン取得には 2 つの制約がある。
   (1) リクエストレート制限（30 秒あたり 10 リクエスト程度）→ 1 リクエスト毎に既定 3.2 秒スリープ。
   (2) 30 日で 100 銘柄までの銘柄クォータ → 同じ期間を二度取りしない。
  そこで銘柄ごとに data/hist1m/{code}.parquet に貯め、既にキャッシュ済みの日付範囲は
  再リクエストせず、不足している範囲（新しい側・古い側の端）だけを取りに行く。

出力スキーマ:
  bars1m 契約（docs/data-contract.md）に source='moomoo_hist' を入れる。日次ディレクトリ
  （data/bars1m/DATE/CODE.parquet）ではなく、銘柄 1 ファイル（data/hist1m/CODE.parquet）に
  期間を連結して持つ点が録画データと異なる（スクリーニングは複数日を一括で読むため）。

  time_key（米国東部ローカル時刻文字列）を time 列に写像する。契約どおり tz は付けない
  （検証用途の US ローカル時刻をそのまま入れる。data-contract.md「US 銘柄のアドホック検証」）。
"""
from __future__ import annotations

import argparse
import logging
import time as _time
from datetime import date as date_t
from datetime import timedelta
from pathlib import Path

import pandas as pd
import yaml

from futu import AuType, KLType

from scalplab.store.writer import enforce_bars1m
from .moomoo_client import MoomooClient, NoPermissionError

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HIST_DIR = PROJECT_ROOT / "data" / "hist1m"
SCREEN_CONFIG_PATH = PROJECT_ROOT / "config" / "screen.yaml"

SOURCE = "moomoo_hist"
DEFAULT_THROTTLE_SEC = 3.2  # レート制限（30s/10req 程度）を守る 1 リクエスト毎のスリープ
DEFAULT_MAX_COUNT = 1000    # 1 ページ最大行数


def hist_path(code: str) -> Path:
    """銘柄キャッシュのパス（HIST_DIR はテストで差し替え可能なモジュール変数）。"""
    return HIST_DIR / f"{code}.parquet"


def load_cache(code: str) -> pd.DataFrame:
    """銘柄キャッシュを bars1m 契約スキーマで返す。無ければ空 DataFrame。"""
    p = hist_path(code)
    if not p.exists():
        return enforce_bars1m(pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "turnover", "source"]))
    return enforce_bars1m(pd.read_parquet(p))


def save_cache(code: str, bars: pd.DataFrame) -> pd.DataFrame:
    """銘柄キャッシュを time 昇順・重複排除して保存し、保存した DataFrame を返す。"""
    p = hist_path(code)
    p.parent.mkdir(parents=True, exist_ok=True)
    out = enforce_bars1m(bars).drop_duplicates(subset=["time"], keep="last").sort_values("time").reset_index(drop=True)
    out.to_parquet(p, index=False)
    return out


def _kline_to_bars1m(raw: pd.DataFrame) -> pd.DataFrame:
    """request_history_kline の返す DataFrame を bars1m 契約スキーマへ写像する。"""
    df = pd.DataFrame({
        "time": raw["time_key"],
        "open": raw["open"],
        "high": raw["high"],
        "low": raw["low"],
        "close": raw["close"],
        "volume": raw["volume"],
        "turnover": raw["turnover"],
        "source": SOURCE,
    })
    return enforce_bars1m(df)


def _missing_ranges(cache: pd.DataFrame, desired_start: date_t, today: date_t):
    """キャッシュ済み日付の外側（新しい側・古い側）で取得すべき日付範囲のリストを返す。

    キャッシュに含まれる取引日の [min, max] 内は「取得済み」とみなし再リクエストしない
    （内部の欠損日=非取引日は無視。連続した取引日データを前提にクォータを節約する）。
    """
    if cache.empty:
        return [(desired_start, today)]
    dates = pd.to_datetime(cache["time"]).dt.date
    cmin, cmax = dates.min(), dates.max()
    ranges = []
    if today > cmax:  # 新しい側のギャップ（前回取得以降の新しい取引日）
        ranges.append((cmax + timedelta(days=1), today))
    if desired_start < cmin:  # 古い側のギャップ（days を広げた場合など）
        ranges.append((desired_start, cmin - timedelta(days=1)))
    return ranges


def _request_range(client, code, start: date_t, end: date_t, throttle_sec: float, extended_time: bool) -> pd.DataFrame:
    """[start, end] の 1 分足を全ページ取得して連結する（レートスロットル込み）。"""
    frames = []
    page_key = None
    while True:
        if throttle_sec > 0:
            _time.sleep(throttle_sec)  # レート制限順守（1 リクエスト毎）
        data, page_key = client.request_history_kline(
            code,
            start=start.isoformat(),
            end=end.isoformat(),
            ktype=KLType.K_1M,
            autype=AuType.QFQ,
            max_count=DEFAULT_MAX_COUNT,
            page_req_key=page_key,
            extended_time=extended_time,
        )
        if data is not None and len(data):
            frames.append(data)
        if not page_key:
            break
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_history_1m(
    code: str,
    days: int,
    client=None,
    throttle_sec: float = DEFAULT_THROTTLE_SEC,
    today: date_t | None = None,
    extended_time: bool = True,
) -> pd.DataFrame:
    """直近 days 日分の 1 分足を（キャッシュを尊重して）取得し、連結キャッシュを返す。

    client を渡さない場合は内部で MoomooClient を接続・切断する。渡した場合は接続管理は
    呼び手責任（CLI が全銘柄で 1 接続を共有してレート・オーバーヘッドを抑えるため）。
    キャッシュで desired 窓が完全に賄えていれば OpenD に一切リクエストしない。
    """
    today = today or date_t.today()
    desired_start = today - timedelta(days=int(days))
    cache = load_cache(code)
    ranges = _missing_ranges(cache, desired_start, today)
    if not ranges:
        return cache  # 全期間キャッシュ済み → リクエスト無し

    owns_client = client is None
    if owns_client:
        client = MoomooClient()
        client.connect()
    try:
        frames = [cache] if not cache.empty else []
        for (s, e) in ranges:
            raw = _request_range(client, code, s, e, throttle_sec, extended_time)
            if not raw.empty:
                frames.append(_kline_to_bars1m(raw))
    finally:
        if owns_client:
            client.close()

    if not frames:
        return cache
    combined = pd.concat(frames, ignore_index=True)
    return save_cache(code, combined)


def _load_symbols(config_path: Path) -> tuple[list[str], int]:
    """config/screen.yaml から symbols と days を読む。"""
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    symbols = list(cfg.get("symbols", []))
    days = int(cfg.get("days", 21))
    return symbols, days


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="config/screen.yaml の銘柄の過去1分足を一括取得（キャッシュ付き）")
    ap.add_argument("--days", type=int, default=None, help="取得日数（省略時は config の days）")
    ap.add_argument("--config", default=None, help="設定 yaml（既定 config/screen.yaml）")
    ap.add_argument("--throttle", type=float, default=DEFAULT_THROTTLE_SEC, help="1 リクエスト毎のスリープ秒")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg_path = Path(args.config) if args.config else SCREEN_CONFIG_PATH
    symbols, cfg_days = _load_symbols(cfg_path)
    days = args.days if args.days is not None else cfg_days
    if not symbols:
        raise SystemExit(f"symbols が空です: {cfg_path}")

    log.info("過去1分足の一括取得を開始: %d 銘柄 × %d 日（throttle=%.1fs/req）", len(symbols), days, args.throttle)
    client = MoomooClient()
    client.connect()
    started = _time.time()
    total_rows = 0
    ok = 0
    try:
        for i, code in enumerate(symbols, 1):
            t0 = _time.time()
            try:
                bars = fetch_history_1m(code, days, client=client, throttle_sec=args.throttle)
                total_rows += len(bars)
                ok += 1
                log.info(
                    "[%d/%d] %s: 取得完了 キャッシュ計 %d 本（この銘柄 %.1fs / 累計 %.0fs 経過）",
                    i, len(symbols), code, len(bars), _time.time() - t0, _time.time() - started,
                )
            except NoPermissionError as e:
                log.warning("[%d/%d] %s: 権限なしでスキップ - %s", i, len(symbols), code, e)
            except Exception as e:  # noqa: BLE001 - 1 銘柄の失敗で全体を止めない
                log.warning("[%d/%d] %s: 取得失敗でスキップ - %s", i, len(symbols), code, e)
    finally:
        client.close()

    log.info("完了: %d/%d 銘柄, 合計 %d 本, 所要 %.0fs", ok, len(symbols), total_rows, _time.time() - started)
    print(f"fetched symbols={ok}/{len(symbols)} rows={total_rows} elapsed={_time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
