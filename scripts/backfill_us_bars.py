"""US 1 分足の欠落日バックフィル（yfinance）。

背景（2026-08-15）: 2026-07-28 / 07-29 は OpenD が落ちていて録画自体が走らず、ティックも気配も
残っていない。ティックは過去に遡って取れないが、1 分足なら yfinance の直近およそ 30 日窓から
まだ拾える（この 2 日は 2026-08-27 頃に窓から外れる）。そこで欠落日の US 1 分足だけを
契約スキーマ（bars1m・source='yfinance'）で埋める。

正データ保護: 既にある (date, code) は上書きしない。録画ティック由来（source='ticks'）が正。
時刻基準: 既存の US bars1m はティック由来で **US 東部ローカル naive**（docs/data-contract.md）。
本スクリプトも ET へ変換して naive で入れる（JP 側の JST 基準とは別扱い）。

実行: PYTHONPATH=. .venv/bin/python scripts/backfill_us_bars.py --dates 2026-07-28,2026-07-29
"""
from __future__ import annotations

import argparse
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml
import yfinance as yf

from scalplab.datafeed.universe import moomoo_to_yf
from scalplab.store.loader import bars1m_path
from scalplab.store.writer import enforce_bars1m, write_parquet

PROJECT_ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_DIR = PROJECT_ROOT / "data" / "universe"


def codes_for(day: str) -> list[str]:
    """その日に録画対象だった US 銘柄（data/universe/US-<date>.yaml）を返す。"""
    path = UNIVERSE_DIR / f"US-{day}.yaml"
    if not path.exists():
        return []
    return [c for c in yaml.safe_load(path.read_text(encoding="utf-8"))["codes"] if c.startswith("US.")]


def backfill_day(day: str, throttle_sec: float = 0.3) -> tuple[int, int]:
    """1 日分を銘柄ごとに取得・保存する。返り値 (written, skipped)。"""
    written = skipped = 0
    start = date.fromisoformat(day)
    end = start + timedelta(days=1)  # yfinance の end は排他
    for code in codes_for(day):
        if bars1m_path(day, code).exists():
            skipped += 1
            continue
        hist = yf.Ticker(moomoo_to_yf(code)).history(
            start=start.isoformat(), end=end.isoformat(), interval="1m", auto_adjust=False
        )
        time.sleep(throttle_sec)
        if hist is None or hist.empty:
            continue
        idx = hist.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert("America/New_York").tz_localize(None)
        df = pd.DataFrame({
            "time": idx,
            "open": hist["Open"].to_numpy(),
            "high": hist["High"].to_numpy(),
            "low": hist["Low"].to_numpy(),
            "close": hist["Close"].to_numpy(),
            "volume": hist["Volume"].to_numpy(),
            "turnover": (hist["Close"] * hist["Volume"]).to_numpy(),
            "source": "yfinance",
        })
        df = df[df["time"].dt.strftime("%Y-%m-%d") == day]
        if df.empty:
            continue
        write_parquet(enforce_bars1m(df), bars1m_path(day, code))
        written += 1
    return written, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="US 1分足の欠落日バックフィル")
    ap.add_argument("--dates", required=True, help="対象日をカンマ区切りで（YYYY-MM-DD,...）")
    args = ap.parse_args()
    for day in args.dates.split(","):
        w, s = backfill_day(day.strip())
        print(f"{day.strip()}: 保存 {w} 銘柄 / スキップ（既存）{s} 銘柄", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
