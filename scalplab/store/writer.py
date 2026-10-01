"""録画データの書き出しユーティリティ（live CSV シンク + 契約 parquet 書き出し）。

なぜ2段構え（live CSV → parquet）か:
録画中はプロセスが落ちても1ティックも失わないよう、行ごとに追記できるプレーン CSV を
「一次記録」として貯める。セッション終了時（または後追いの finalize）で契約 parquet に変換する。
こうしておけばクラッシュしても data/.live/ が残り、finalize を再実行すれば救済できる。

スキーマ（列名・dtype）は docs/data-contract.md が正。ここで一元管理し、finalize/loader が
同じ強制関数を使うことで recorder と backtest のスキーマ不一致を防ぐ。
"""
from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

# プロジェクトルートは相対解決（絶対パス埋め込み禁止）。
# このファイルは scalplab/store/writer.py なので parents[2] がリポジトリルート。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
# 録画中の一次記録置き場。ドット始まりにして通常データと混ざらないようにする。
LIVE_DIR = DATA_DIR / ".live"

# --- 契約スキーマ定義（docs/data-contract.md） ---
# time 列は全テーブル共通で JST naive datetime64[ns]。dtype は enforce_* で強制する。

TICKS_COLUMNS = ["time", "price", "volume", "turnover", "direction", "seq", "tick_type"]
_TICKS_NUMERIC = {"price": "float64", "turnover": "float64"}
_TICKS_INT = {"volume": "int64", "seq": "int64"}
_TICKS_STR = ("direction", "tick_type")

QUOTES_COLUMNS = ["time", "bid", "ask", "bid_vol", "ask_vol"]
_QUOTES_NUMERIC = {"bid": "float64", "ask": "float64"}
_QUOTES_INT = {"bid_vol": "int64", "ask_vol": "int64"}

BARS1M_COLUMNS = ["time", "open", "high", "low", "close", "volume", "turnover", "source"]
_BARS_NUMERIC = {"open": "float64", "high": "float64", "low": "float64", "close": "float64", "turnover": "float64"}
_BARS_INT = {"volume": "int64"}
_BARS_STR = ("source",)


def _coerce_time(series: pd.Series) -> pd.Series:
    """time 列を JST naive datetime64[ns] に正規化する。

    moomoo の返却は既に市場ローカル時刻の文字列なので、tz は付けない。
    万一 tz-aware が紛れ込んでも naive に落として契約（tz 情報なし）を守る。
    moomoo の time は小数秒が有る行（"...:18.955"）と無い行（"...:18"）が混在するため、
    固定フォーマット推論では失敗する。ISO8601 パーサで両方を受ける（pandas 3.0）。
    """
    ts = pd.to_datetime(series, format="ISO8601")
    if getattr(ts.dtype, "tz", None) is not None:
        ts = ts.dt.tz_localize(None)
    return ts.astype("datetime64[ns]")


def _to_int(series: pd.Series) -> pd.Series:
    """出来高・株数系を int64 に。欠損は 0、小数は四捨五入（moomoo volume は float 返し）。"""
    return series.fillna(0).round().astype("int64")


def enforce_ticks(df: pd.DataFrame) -> pd.DataFrame:
    """ticks を契約スキーマ・dtype・列順に強制して返す。"""
    out = df.copy()
    out["time"] = _coerce_time(out["time"])
    for col, dt in _TICKS_NUMERIC.items():
        out[col] = out[col].astype(dt)
    for col in _TICKS_INT:
        out[col] = _to_int(out[col])
    for col in _TICKS_STR:
        out[col] = out[col].astype("string")
    return out[TICKS_COLUMNS]


def enforce_quotes(df: pd.DataFrame) -> pd.DataFrame:
    """quotes（L1）を契約スキーマ・dtype・列順に強制して返す。"""
    out = df.copy()
    out["time"] = _coerce_time(out["time"])
    for col, dt in _QUOTES_NUMERIC.items():
        out[col] = out[col].astype(dt)
    for col in _QUOTES_INT:
        out[col] = _to_int(out[col])
    return out[QUOTES_COLUMNS]


def enforce_bars1m(df: pd.DataFrame) -> pd.DataFrame:
    """bars1m を契約スキーマ・dtype・列順に強制して返す。"""
    out = df.copy()
    out["time"] = _coerce_time(out["time"])
    for col, dt in _BARS_NUMERIC.items():
        out[col] = out[col].astype(dt)
    for col in _BARS_INT:
        out[col] = _to_int(out[col])
    for col in _BARS_STR:
        out[col] = out[col].astype("string")
    return out[BARS1M_COLUMNS]


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    """親ディレクトリを作って parquet 出力する（pyarrow バックエンド）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


class LiveCsvSink:
    """録画中の一次記録シンク。(code, kind) ごとに CSV を追記する。

    push ハンドラは futu の別スレッドで走るため、本シンクは「単一の書き込みスレッド」
    からのみ呼ぶ前提（record.py が queue を1本のドレインスレッドで捌く）。よってロック不要。
    flush をこまめに呼ぶことでクラッシュ時の取りこぼしを最小化する。
    """

    def __init__(self, date: str, root: Path = LIVE_DIR) -> None:
        self.dir = root / date
        self.dir.mkdir(parents=True, exist_ok=True)
        # (code, kind) -> (file handle, DictWriter)
        self._files: dict[tuple[str, str], tuple] = {}

    def _writer_for(self, code: str, kind: str, columns: list[str]):
        key = (code, kind)
        w = self._files.get(key)
        if w is None:
            path = self.dir / f"{code}.{kind}.csv"
            need_header = (not path.exists()) or path.stat().st_size == 0
            fh = open(path, "a", newline="", encoding="utf-8")
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            if need_header:
                writer.writeheader()
            self._files[key] = (fh, writer)
            return writer
        return w[1]

    def write(self, code: str, kind: str, columns: list[str], row: dict) -> None:
        self._writer_for(code, kind, columns).writerow(row)

    def flush(self) -> None:
        for fh, _ in self._files.values():
            fh.flush()

    def close(self) -> None:
        self.flush()
        for fh, _ in self._files.values():
            fh.close()
        self._files.clear()
