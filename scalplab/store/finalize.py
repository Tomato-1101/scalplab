"""live CSV → 契約 parquet への変換（セッション終了時 or クラッシュ後の後追い実行）。

recorder は録画中 data/.live/YYYY-MM-DD/{code}.{ticks|quotes}.csv に追記するだけ。
本モジュールが seq 重複排除・time 昇順・スキーマ強制を行い、契約パスへ parquet を書く。
`python -m scalplab.store.finalize --date YYYY-MM-DD` で単独再実行できる（救済用）。
"""
from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

import pandas as pd

from .loader import bars1m_path, quotes_path, resample_ticks_to_bars, ticks_path
from .writer import (
    DATA_DIR,
    LIVE_DIR,
    enforce_quotes,
    enforce_ticks,
    write_parquet,
)

log = logging.getLogger(__name__)


def finalize_date(
    date: str,
    live_root: Path = LIVE_DIR,
    out_root: Path = DATA_DIR,
    make_bars: bool = True,
    cleanup: bool = False,
) -> dict[str, dict[str, int]]:
    """その日の live CSV を契約 parquet に変換する。

    - ticks: (seq) 重複排除 → time 昇順 → data/ticks/。make_bars なら同時に data/bars1m/
      へティック合成1分足（source='ticks'）も書く。
    - quotes: time 昇順 → data/quotes/。
    戻り値は {code: {"ticks": n, "quotes": n, "bars1m": n}} の変換行数サマリー。
    cleanup=True なら変換済み live ディレクトリを削除する。
    """
    live_dir = live_root / date
    results: dict[str, dict[str, int]] = {}
    if not live_dir.exists():
        log.warning("live ディレクトリが無い: %s", live_dir)
        return results

    for csv_path in sorted(live_dir.glob("*.ticks.csv")):
        code = csv_path.name[: -len(".ticks.csv")]
        try:
            raw = pd.read_csv(csv_path)
        except pd.errors.EmptyDataError:
            continue
        if raw.empty:
            continue
        df = enforce_ticks(raw)
        # 契約: 同一 (code, seq) を重複排除。1ファイル1銘柄なので seq のみで一意化。
        df = df.drop_duplicates(subset=["seq"]).sort_values("time").reset_index(drop=True)
        write_parquet(df, ticks_path(date, code, out_root))
        results.setdefault(code, {})["ticks"] = len(df)
        if make_bars:
            bars = resample_ticks_to_bars(df, 60)
            write_parquet(bars, bars1m_path(date, code, out_root))
            results[code]["bars1m"] = len(bars)

    for csv_path in sorted(live_dir.glob("*.quotes.csv")):
        code = csv_path.name[: -len(".quotes.csv")]
        try:
            raw = pd.read_csv(csv_path)
        except pd.errors.EmptyDataError:
            continue
        if raw.empty:
            continue
        df = enforce_quotes(raw).sort_values("time").reset_index(drop=True)
        write_parquet(df, quotes_path(date, code, out_root))
        results.setdefault(code, {})["quotes"] = len(df)

    if cleanup:
        shutil.rmtree(live_dir, ignore_errors=True)
        log.info("live ディレクトリを掃除: %s", live_dir)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="live CSV を契約 parquet に変換")
    parser.add_argument("--date", required=True, help="対象営業日 YYYY-MM-DD（JST）")
    parser.add_argument("--cleanup", action="store_true", help="変換後に live を削除")
    parser.add_argument("--no-bars", action="store_true", help="ティック合成1分足を作らない")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = finalize_date(
        args.date, make_bars=not args.no_bars, cleanup=args.cleanup
    )
    if not results:
        print(f"{args.date}: 変換対象なし（live CSV が見つからない）")
        return
    for code, counts in sorted(results.items()):
        parts = ", ".join(f"{k}={v}" for k, v in counts.items())
        print(f"{code}: {parts}")
    print(f"合計 {len(results)} 銘柄を finalize 完了")


if __name__ == "__main__":
    main()
