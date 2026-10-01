"""録画データの読み手 API（docs/data-contract.md の loader API を実装）。

backtest 側は本モジュールと契約スキーマだけに依存し、recorder の内部実装（CSV シンク等）に
一切依存しない。パスは全てプロジェクトルート相対で解決する。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .writer import (
    BARS1M_COLUMNS,
    DATA_DIR,
    enforce_bars1m,
    enforce_quotes,
    enforce_ticks,
)


# --- パス規約（docs/data-contract.md「パス規約」） ---

def ticks_path(date: str, code: str, root: Path | None = None) -> Path:
    return (root if root is not None else DATA_DIR) / "ticks" / date / f"{code}.parquet"


def bars1m_path(date: str, code: str, root: Path | None = None) -> Path:
    return (root if root is not None else DATA_DIR) / "bars1m" / date / f"{code}.parquet"


def quotes_path(date: str, code: str, root: Path | None = None) -> Path:
    return (root if root is not None else DATA_DIR) / "quotes" / date / f"{code}.parquet"


def universe_path(date: str, root: Path | None = None) -> Path:
    return (root if root is not None else DATA_DIR) / "universe" / f"{date}.yaml"


# --- loader API ---

def load_ticks(date: str, code: str) -> pd.DataFrame:
    """その日・その銘柄のティックを契約スキーマで返す。無ければ FileNotFoundError。"""
    p = ticks_path(date, code)
    if not p.exists():
        raise FileNotFoundError(f"ticks not found: {p}")
    return enforce_ticks(pd.read_parquet(p))


def load_bars1m(date: str, code: str) -> pd.DataFrame:
    """その日・その銘柄の1分足を契約スキーマで返す。無ければ FileNotFoundError。"""
    p = bars1m_path(date, code)
    if not p.exists():
        raise FileNotFoundError(f"bars1m not found: {p}")
    return enforce_bars1m(pd.read_parquet(p))


def load_quotes(date: str, code: str) -> pd.DataFrame:
    """その日・その銘柄の L1 気配を契約スキーマで返す。無ければ FileNotFoundError。"""
    p = quotes_path(date, code)
    if not p.exists():
        raise FileNotFoundError(f"quotes not found: {p}")
    return enforce_quotes(pd.read_parquet(p))


def list_recorded(date: str, kind: str = "ticks") -> list[str]:
    """その日に録画済みの code 一覧を返す（kind ∈ {ticks, bars1m, quotes}）。"""
    d = DATA_DIR / kind / date
    if not d.exists():
        return []
    # code はドットを含む（例 US.AAPL）が Path.stem は最後の .parquet だけ剥がす。
    return sorted(p.stem for p in d.glob("*.parquet"))


def resample_ticks_to_bars(ticks: pd.DataFrame, seconds: int) -> pd.DataFrame:
    """ティックから任意秒数の OHLCV バーを合成する（bars1m と同スキーマ、source='ticks'）。

    time は各ビンの開始時刻（epoch 起点で floor するので 60s なら分境界、15s なら :00/:15/…）。
    空ビンは生成しない（実約定のあった区間だけをバーにする）。
    """
    if ticks.empty:
        return enforce_bars1m(pd.DataFrame({c: [] for c in BARS1M_COLUMNS}))

    t = enforce_ticks(ticks).sort_values("time")
    binned = t["time"].dt.floor(f"{seconds}s")
    grouped = t.assign(_bin=binned).groupby("_bin", sort=True)
    bars = grouped.agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume=("volume", "sum"),
        turnover=("turnover", "sum"),
    ).reset_index().rename(columns={"_bin": "time"})
    bars["source"] = "ticks"
    return enforce_bars1m(bars)
