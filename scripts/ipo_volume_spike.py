"""IPO 後の出来高枯れからの急増が、その後の収益を伴うか時系列で検証する。"""

from __future__ import annotations

import argparse
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd


DAILY_PATH = Path("data/.cache/ipo-daily.parquet")
TRADES_PATH = Path("data/.cache/ipo-trades.parquet")
SPLIT_DATE = pd.Timestamp("2025-01-01")
MIN_AGE = 15
MAX_AGE = 250
DRY_FRACS = (0.15, 0.3, 0.5)
LULL_MODES = ("none", "below_day0", "dd20")
SPIKE_MULTS = (3, 5, 10)
UP_MODES = ("any", "up")
MIN_TURNOVERS = (0.0, 1e8)
HOLDS = (1, 2, 3, 5, 10, 20)
CONFIG_COLUMNS = ["dry_frac", "lull", "spike_mult", "up_mode", "min_turnover"]
TRADE_COLUMNS = CONFIG_COLUMNS + [
    "hold",
    "code",
    "signal_date",
    "entry_date",
    "exit_date",
    "signal_age",
    "signal_return",
    "entry",
    "exit",
    "net_bp",
]


def parse_args() -> argparse.Namespace:
    """往復コストだけを外から変え、構成探索の比較軸を固定する。"""
    parser = argparse.ArgumentParser(description="IPO 出来高スパイクのイベントスタディ")
    parser.add_argument("--cost", type=float, default=10.0, help="1トレードの往復コスト (bp、既定: 10)")
    return parser.parse_args()


def configurations() -> list[tuple[float, str, int, str, float]]:
    """探索漏れや重複を避けるため、直積として108構成を明示する。"""
    configs = list(product(DRY_FRACS, LULL_MODES, SPIKE_MULTS, UP_MODES, MIN_TURNOVERS))
    if len(configs) != 108:
        raise RuntimeError(f"構成数が108ではありません: {len(configs)}")
    return configs


def prepare_daily(path: Path) -> pd.DataFrame:
    """バー順を銘柄内で確定し、t=0 を yfinance の初回バーとして一貫させる。"""
    daily = pd.read_parquet(path)
    required = {"code", "date", "open", "high", "low", "close", "volume"}
    missing = required.difference(daily.columns)
    if missing:
        raise ValueError(f"日足に必要な列がありません: {sorted(missing)}")
    daily = daily.copy()
    daily["code"] = daily["code"].astype(str)
    daily["date"] = pd.to_datetime(daily["date"]).dt.tz_localize(None)
    daily = daily.sort_values(["code", "date"]).reset_index(drop=True)
    # 母集団は 2019 年以降の IPO。初回バーがそれ以前にある銘柄はテクニカル上場・
    # コード再利用・誤マッチであり、t=0 起点の窓が別時代を指すため除外する
    first_bar = daily.groupby("code")["date"].transform("min")
    cutoff = pd.Timestamp("2018-12-01")
    dropped = daily.loc[first_bar < cutoff, "code"].nunique()
    if dropped:
        print(f"除外: 初回バーが {cutoff.date()} 以前の {dropped} 銘柄（テクニカル上場・コード再利用の疑い）")
    return daily[first_bar >= cutoff].reset_index(drop=True)


def add_signal_features(group: pd.DataFrame) -> pd.DataFrame:
    """当日引けまでに確定した窓だけでシグナル特徴量を作り、先読みを防ぐ。"""
    frame = group.reset_index(drop=True).copy()
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    frame["age"] = np.arange(len(frame))
    frame["dry_volume"] = volume.rolling(window=10, min_periods=10).median()
    frame["pre_spike_volume"] = volume.shift(1).rolling(window=10, min_periods=10).median()
    frame["base_volume"] = volume.iloc[:5].median() if len(volume) >= 5 else np.nan
    frame["day0_close"] = close.iloc[0] if len(close) else np.nan
    frame["running_max_close"] = close.expanding(min_periods=1).max()
    frame["signal_return"] = close.div(close.shift(1)).sub(1.0)
    frame["turnover"] = volume * close
    return frame


def signal_mask(
    frame: pd.DataFrame,
    dry_frac: float,
    lull: str,
    spike_mult: int,
    up_mode: str,
    min_turnover: float,
) -> pd.Series:
    """同じ特徴量から条件だけを替え、構成間で情報集合を揃える。"""
    base_volume = frame["base_volume"]
    mask = (
        frame["age"].between(MIN_AGE, MAX_AGE)
        & base_volume.gt(0)
        & frame["dry_volume"].le(dry_frac * base_volume)
        & frame["volume"].ge(spike_mult * frame["pre_spike_volume"])
        & frame["turnover"].ge(min_turnover)
    )
    if lull == "below_day0":
        mask &= frame["close"].lt(frame["day0_close"])
    elif lull == "dd20":
        mask &= frame["close"].le(0.8 * frame["running_max_close"])
    elif lull != "none":
        raise ValueError(f"未知の lull モードです: {lull}")

    if up_mode == "up":
        mask &= frame["close"].gt(frame["close"].shift(1))
    elif up_mode != "any":
        raise ValueError(f"未知の陽線モードです: {up_mode}")
    return mask.fillna(False)


def trades_for_configuration(
    frame: pd.DataFrame,
    config: tuple[float, str, int, str, float],
    cost: float,
) -> list[dict[str, object]]:
    """hold ごとに保有期間を管理し、解消前の再シグナルだけを除く。"""
    dry_frac, lull, spike_mult, up_mode, min_turnover = config
    mask = signal_mask(frame, dry_frac, lull, spike_mult, up_mode, min_turnover)
    rows: list[dict[str, object]] = []
    signal_indices = np.flatnonzero(mask.to_numpy())

    for hold in HOLDS:
        occupied_through = -1
        for signal_index in signal_indices:
            if signal_index <= occupied_through:
                continue
            entry_index = signal_index + 1
            exit_index = entry_index + hold - 1
            if entry_index >= len(frame) or exit_index >= len(frame):
                continue
            entry = frame.at[entry_index, "open"]
            exit_price = frame.at[exit_index, "close"]
            if pd.isna(entry) or entry == 0 or pd.isna(exit_price):
                continue
            occupied_through = exit_index
            rows.append(
                {
                    "dry_frac": dry_frac,
                    "lull": lull,
                    "spike_mult": spike_mult,
                    "up_mode": up_mode,
                    "min_turnover": min_turnover,
                    "hold": hold,
                    "code": frame.at[signal_index, "code"],
                    "signal_date": frame.at[signal_index, "date"],
                    "entry_date": frame.at[entry_index, "date"],
                    "exit_date": frame.at[exit_index, "date"],
                    "signal_age": int(frame.at[signal_index, "age"]),
                    "signal_return": frame.at[signal_index, "signal_return"],
                    "entry": float(entry),
                    "exit": float(exit_price),
                    "net_bp": (float(exit_price) / float(entry) - 1.0) * 1e4 - cost,
                }
            )
    return rows


def build_trades(daily: pd.DataFrame, cost: float) -> pd.DataFrame:
    """銘柄単位の時系列を閉じたまま処理し、別銘柄の窓が混ざるのを防ぐ。"""
    configs = configurations()
    all_rows: list[dict[str, object]] = []
    for _, group in daily.groupby("code", sort=False):
        frame = add_signal_features(group)
        for config in configs:
            all_rows.extend(trades_for_configuration(frame, config, cost))
    trades = pd.DataFrame(all_rows, columns=TRADE_COLUMNS)
    for column in ("signal_date", "entry_date", "exit_date"):
        trades[column] = pd.to_datetime(trades[column])
    for column in ("dry_frac", "spike_mult", "min_turnover", "hold", "signal_age", "signal_return", "entry", "exit", "net_bp"):
        trades[column] = pd.to_numeric(trades[column])
    if not trades.empty:
        trades["period"] = np.where(trades["signal_date"].lt(SPLIT_DATE), "学習", "検証")
    else:
        trades["period"] = pd.Series(dtype="object")
    return trades


def format_float(value: object, digits: int = 2) -> str:
    """欠損を誤ってゼロと見せず、出力表の解釈を保つ。"""
    return "NaN" if pd.isna(value) else f"{float(value):.{digits}f}"


def print_top_configs(trades: pd.DataFrame) -> None:
    """学習期間だけで順位を決め、検証期間を選抜に使わない。"""
    print("\n=== hold=5: 学習平均 net bp 上位10と検証成績 ===")
    hold5 = trades[trades["hold"].eq(5)]
    train = hold5[hold5["period"].eq("学習")]
    valid = hold5[hold5["period"].eq("検証")]
    train_summary = train.groupby(CONFIG_COLUMNS, dropna=False)["net_bp"].agg(train_n="size", train_mean="mean")
    eligible = train_summary[train_summary["train_n"].ge(30)].sort_values("train_mean", ascending=False).head(10)
    valid_summary = valid.groupby(CONFIG_COLUMNS, dropna=False)["net_bp"].agg(
        valid_n="size", valid_mean="mean", valid_win=lambda series: series.gt(0).mean()
    )
    table = eligible.join(valid_summary, how="left").reset_index()
    if table.empty:
        print("該当構成なし")
    else:
        print(table.to_string(index=False, float_format=lambda value: f"{value:.2f}"))


def print_spearman(trades: pd.DataFrame) -> None:
    """各期間で十分な標本がある構成だけを比較し、少数例の順位変動を抑える。"""
    print("\n=== hold=5: 学習・検証の構成別平均 net bp スピアマン相関 ===")
    hold5 = trades[trades["hold"].eq(5)]
    summary = hold5.groupby(CONFIG_COLUMNS + ["period"], dropna=False)["net_bp"].agg(["size", "mean"]).reset_index()
    means = summary[summary["size"].ge(30)].pivot(index=CONFIG_COLUMNS, columns="period", values="mean")
    if not {"学習", "検証"}.issubset(means.columns):
        print("相関計算に必要な構成なし")
        return
    paired = means.dropna(subset=["学習", "検証"])
    correlation = paired["学習"].rank().corr(paired["検証"].rank())
    print(f"構成数: {len(paired)} / rho: {format_float(correlation, 4)}")


def print_baseline(trades: pd.DataFrame) -> None:
    """事前に定めた基準構成を、探索順位とは切り離して多角的に示す。"""
    baseline = trades[
        trades["dry_frac"].eq(0.3)
        & trades["lull"].eq("none")
        & trades["spike_mult"].eq(5)
        & trades["up_mode"].eq("any")
        & trades["min_turnover"].eq(1e8)
    ]
    print("\n=== 基準構成: hold別・学習/検証別 ===")
    by_hold = baseline.groupby(["period", "hold"], sort=False)["net_bp"].agg(
        n="size", mean_net_bp="mean", median_net_bp="median", win_rate=lambda series: series.gt(0).mean()
    )
    print("該当トレードなし" if by_hold.empty else by_hold.to_string(float_format=lambda value: f"{value:.2f}"))

    print("\n=== 基準構成: シグナル年別 ===")
    yearly_source = baseline[baseline["hold"].eq(5)].copy()
    yearly_source["signal_year"] = yearly_source["signal_date"].dt.year
    yearly = yearly_source.groupby("signal_year")["net_bp"].agg(n="size", mean_net_bp="mean")
    print("該当トレードなし" if yearly.empty else yearly.to_string(float_format=lambda value: f"{value:.2f}"))

    print("\n=== 基準構成: hold=5 net 上位5 ===")
    detail_columns = ["code", "signal_date", "signal_return", "net_bp"]
    best = yearly_source.nlargest(5, "net_bp")[detail_columns]
    print("該当トレードなし" if best.empty else best.to_string(index=False, float_format=lambda value: f"{value:.4f}"))
    print("\n=== 基準構成: hold=5 net 下位5 ===")
    worst = yearly_source.nsmallest(5, "net_bp")[detail_columns]
    print("該当トレードなし" if worst.empty else worst.to_string(index=False, float_format=lambda value: f"{value:.4f}"))


def print_basic_stats(trades: pd.DataFrame, daily: pd.DataFrame) -> None:
    """重複する hold 行と実シグナルを区別し、探索規模を誤読させない。"""
    print("\n=== 全構成の基本統計 ===")
    signal_keys = CONFIG_COLUMNS + ["hold", "code", "signal_date", "entry_date"]
    signals = trades.drop_duplicates(signal_keys)
    print(f"構成数: {len(configurations())}")
    print(f"入力銘柄数: {daily['code'].nunique()}")
    print(f"入力日足行数: {len(daily)}")
    print(f"全構成・全hold合計シグナル件数: {len(signals)}")
    print(f"全hold合計トレード明細行数: {len(trades)}")
    print(f"シグナル銘柄数: {signals['code'].nunique() if not signals.empty else 0}")
    if not signals.empty:
        print(f"学習シグナル件数: {(signals['signal_date'] < SPLIT_DATE).sum()}")
        print(f"検証シグナル件数: {(signals['signal_date'] >= SPLIT_DATE).sum()}")


def main() -> None:
    """明細を保存してから同じ集合を集計し、表示結果を後から監査可能にする。"""
    args = parse_args()
    daily = prepare_daily(DAILY_PATH)
    trades = build_trades(daily, args.cost)
    TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
    trades.to_parquet(TRADES_PATH, index=False)
    print(f"保存: {TRADES_PATH} ({len(trades)}行、往復コスト {args.cost:g}bp)")
    print_top_configs(trades)
    print_spearman(trades)
    print_baseline(trades)
    print_basic_stats(trades, daily)


if __name__ == "__main__":
    main()
