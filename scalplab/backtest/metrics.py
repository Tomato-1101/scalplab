"""entries からのエントリー品質集計。

評価窓が切れたシグナル(eval_truncated)は集計から除外する。既定は +60s ネットリターンを
主指標に、勝率・期待値(bp)・MFE/MAE 分位・時間帯別分布を出す。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _valid(entries: pd.DataFrame) -> pd.DataFrame:
    if len(entries) and "eval_truncated" in entries.columns:
        return entries[~entries["eval_truncated"].astype(bool)]
    return entries


def summarize(entries: pd.DataFrame, horizon: int = 60) -> dict:
    """entries を 1 つの指標辞書に集計する。"""
    total = int(len(entries))
    valid = _valid(entries)
    nv = int(len(valid))
    net_col = f"ret_{horizon}s_net"
    gross_col = f"ret_{horizon}s_gross"

    res = {
        "n_signals": total,
        "n_valid": nv,
        "n_truncated": total - nv,
        "n_long": int((valid["direction"] == "long").sum()) if nv else 0,
        "n_short": int((valid["direction"] == "short").sum()) if nv else 0,
    }

    if nv and net_col in valid.columns:
        net = valid[net_col].dropna()
        gross = valid[gross_col].dropna()
        if len(net):
            res["win_rate"] = float((net > 0).mean())
            res["mean_ret_net_bp"] = float(net.mean())
            res["median_ret_net_bp"] = float(net.median())
            res["expectancy_bp"] = float(net.mean())  # 期待値 = ネット平均
        else:
            res.update(win_rate=np.nan, mean_ret_net_bp=np.nan,
                       median_ret_net_bp=np.nan, expectancy_bp=np.nan)
        res["mean_ret_gross_bp"] = float(gross.mean()) if len(gross) else np.nan
        res["mfe_median_bp"] = float(valid["mfe_bp"].median()) if "mfe_bp" in valid else np.nan
        res["mae_median_bp"] = float(valid["mae_bp"].median()) if "mae_bp" in valid else np.nan
    else:
        for k in ("win_rate", "mean_ret_net_bp", "median_ret_net_bp", "expectancy_bp",
                  "mean_ret_gross_bp", "mfe_median_bp", "mae_median_bp"):
            res[k] = np.nan

    return res


# --- Web UI 統計カード用の追加集計 --------------------------------------------
# 既存の summarize / by_hour は改変しない（シグネチャ固定）。バー系（+Nバー評価）と
# ティック系（+Ns 評価）の両方を、評価窓ごとにまとめて出すための純関数群。

BAR_HORIZONS = (1, 2)          # バー系の勝率表示窓（+1本・+2本）
TICK_HORIZONS = (30, 60, 120)  # ティック系の勝率表示窓（秒）


def valid_entries(entries: pd.DataFrame) -> pd.DataFrame:
    """評価打切り（eval_truncated）を除いた有効エントリーを返す（_valid の公開版）。"""
    return _valid(entries)


def _horizon_row(valid: pd.DataFrame, label: str, net_col: str, gross_col: str) -> dict:
    """1 評価窓の勝率・平均/中央 net・平均 gross・合計 net・件数を返す。"""
    row = {"label": label, "win_rate": np.nan, "mean_net_bp": np.nan,
           "median_net_bp": np.nan, "mean_gross_bp": np.nan, "sum_net_bp": np.nan, "n": 0}
    if net_col in valid.columns:
        net = valid[net_col].dropna()
        if len(net):
            row["win_rate"] = float((net > 0).mean())   # net>0 の割合（コスト控除後）
            row["mean_net_bp"] = float(net.mean())
            row["median_net_bp"] = float(net.median())
            row["sum_net_bp"] = float(net.sum())
            row["n"] = int(len(net))
    if gross_col in valid.columns:
        gross = valid[gross_col].dropna()
        if len(gross):
            row["mean_gross_bp"] = float(gross.mean())
    return row


def horizon_breakdown(entries: pd.DataFrame, kind: str) -> list[dict]:
    """評価窓ごとの成績を返す。kind='bar' → +Nバー、それ以外 → +Ns（ティック）。"""
    valid = _valid(entries)
    if kind == "bar":
        return [_horizon_row(valid, f"+{k}本", f"ret_{k}bar_net", f"ret_{k}bar_gross")
                for k in BAR_HORIZONS]
    return [_horizon_row(valid, f"+{h}s", f"ret_{h}s_net", f"ret_{h}s_gross")
            for h in TICK_HORIZONS]


def max_losing_streak(entries: pd.DataFrame, net_col: str) -> int:
    """time 昇順で net<=0（負け・引き分け）が連続する最大本数を返す。"""
    valid = _valid(entries)
    if not len(valid) or net_col not in valid.columns:
        return 0
    net = valid.sort_values("time")[net_col]
    streak = best = 0
    for x in net:
        if pd.isna(x):
            continue
        if x <= 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return int(best)


def session_split(entries: pd.DataFrame, net_col: str, boundary_min: int = 720,
                  labels: tuple[str, str] = ("前場", "後場")) -> list[dict]:
    """時間帯を境界（既定 720分=12:00）で二分し、件数と勝率を返す。

    前場/後場（日本株）・午前/午後（米国）のどちらでも labels を差し替えて使える。
    """
    valid = _valid(entries)
    if not len(valid) or net_col not in valid.columns:
        return [{"label": labels[0], "n": 0, "win_rate": np.nan},
                {"label": labels[1], "n": 0, "win_rate": np.nan}]
    t = pd.to_datetime(valid["time"])
    minute = t.dt.hour * 60 + t.dt.minute
    out = []
    for lab, mask in ((labels[0], minute < boundary_min), (labels[1], minute >= boundary_min)):
        g = valid[mask.to_numpy()]
        net = g[net_col].dropna()
        out.append({
            "label": lab,
            "n": int(len(g)),
            "win_rate": float((net > 0).mean()) if len(net) else np.nan,
        })
    return out


def by_hour(entries: pd.DataFrame, horizon: int = 60) -> pd.DataFrame:
    """時間帯別（時単位）のシグナル数・平均ネットリターン・勝率。"""
    cols = ["hour", "n", "mean_net_bp", "win_rate"]
    valid = _valid(entries)
    net_col = f"ret_{horizon}s_net"
    if not len(valid) or net_col not in valid.columns:
        return pd.DataFrame(columns=cols)
    v = valid.copy()
    v["hour"] = pd.to_datetime(v["time"]).dt.hour
    out = []
    for hour, g in v.groupby("hour"):
        net = g[net_col].dropna()
        out.append({
            "hour": int(hour),
            "n": int(len(g)),
            "mean_net_bp": float(net.mean()) if len(net) else np.nan,
            "win_rate": float((net > 0).mean()) if len(net) else np.nan,
        })
    return pd.DataFrame(out, columns=cols).sort_values("hour").reset_index(drop=True)
