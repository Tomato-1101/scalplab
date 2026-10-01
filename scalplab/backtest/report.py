"""レポート生成（Markdown / コンソール表）。

追加依存（matplotlib 等）は入れない。表とテキストだけで十分な情報を出す。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _fmt(v, nd=2):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def summary_lines(summary: dict, horizon: int) -> list[str]:
    """summarize() の結果を人間可読な行リストにする（コンソール共用）。"""
    return [
        f"signals={summary['n_signals']} valid={summary['n_valid']} "
        f"truncated={summary['n_truncated']} (long={summary['n_long']} short={summary['n_short']})",
        f"+{horizon}s win_rate={_fmt(summary.get('win_rate'), 3)} "
        f"expectancy={_fmt(summary.get('expectancy_bp'))}bp "
        f"mean_net={_fmt(summary.get('mean_ret_net_bp'))}bp "
        f"median_net={_fmt(summary.get('median_ret_net_bp'))}bp "
        f"mean_gross={_fmt(summary.get('mean_ret_gross_bp'))}bp",
        f"MFE_median={_fmt(summary.get('mfe_median_bp'))}bp "
        f"MAE_median={_fmt(summary.get('mae_median_bp'))}bp",
    ]


def render_run_summary(entries, params, summary, byhour_df, date, codes, horizon) -> str:
    """run.py 用 summary.md を生成する。"""
    lines = [
        f"# バックテスト結果 — {date}",
        "",
        f"- 銘柄: {', '.join(codes) if codes else '(なし)'}",
        f"- 主評価ホライズン: +{horizon}s",
        "",
        "## パラメータ",
        "",
        "| key | value |",
        "|---|---|",
    ]
    for k in sorted(params):
        lines.append(f"| {k} | {params[k]} |")

    lines += ["", "## サマリー", ""]
    for line in summary_lines(summary, horizon):
        lines.append(f"- {line}")

    lines += ["", "## 時間帯別（時単位）", "", "| hour | n | mean_net_bp | win_rate |", "|---|---|---|---|"]
    if len(byhour_df):
        for _, r in byhour_df.iterrows():
            lines.append(f"| {int(r['hour'])} | {int(r['n'])} | {_fmt(r['mean_net_bp'])} | {_fmt(r['win_rate'], 3)} |")
    else:
        lines.append("| - | - | - | - |")

    # 先頭数件のシグナル明細
    lines += ["", "## シグナル明細（先頭20件）", ""]
    if len(entries):
        show_cols = ["time", "code", "direction", "entry_price", "drci", "efficiency",
                     f"ret_{horizon}s_net", "mfe_bp", "mae_bp", "eval_truncated"]
        show_cols = [c for c in show_cols if c in entries.columns]
        head = entries[show_cols].head(20)
        lines.append("| " + " | ".join(show_cols) + " |")
        lines.append("|" + "|".join(["---"] * len(show_cols)) + "|")
        for _, r in head.iterrows():
            cells = [_fmt(r[c]) if isinstance(r[c], float) else str(r[c]) for c in show_cols]
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append("(シグナルなし)")

    return "\n".join(lines) + "\n"


def render_scan_top(results_df: pd.DataFrame, param_keys, top_n=20, min_signals=0, horizon=60) -> str:
    """scan.py 用 top.md を生成する（期待値順トップ N）。"""
    df = results_df.copy()
    if "n_signals" in df.columns and min_signals > 0:
        df = df[df["n_signals"] >= min_signals]
    if "expectancy_bp" in df.columns:
        df = df.sort_values("expectancy_bp", ascending=False)
    top = df.head(top_n)

    metric_cols = ["n_signals", "n_valid", "win_rate", "expectancy_bp",
                   "mean_ret_net_bp", "mfe_median_bp", "mae_median_bp"]
    metric_cols = [c for c in metric_cols if c in top.columns]
    cols = list(param_keys) + metric_cols

    lines = [
        f"# パラメータスキャン トップ{top_n}（期待値順, min_signals={min_signals}, +{horizon}s）",
        "",
        "| " + " | ".join(cols) + " |",
        "|" + "|".join(["---"] * len(cols)) + "|",
    ]
    for _, r in top.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            cells.append(_fmt(v, 3) if isinstance(v, float) else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    if not len(top):
        lines.append("(min_signals を満たす組み合わせなし)")
    return "\n".join(lines) + "\n"
