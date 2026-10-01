"""クロス銘柄スクリーニング（RCI×パラボリック手法のバー近似版・過去1分足ふるい）。

  python -m scalplab.backtest.screen [--config config/screen.yaml]

目的:
  過去1分足を読み、銘柄 × パラメータグリッドでバー近似シグナル（strategy.rci_impulse_bar）を評価する。
  「どの銘柄・商品で手法が効きそうか」を横断的に粗くランキングし、本命のティック精密検証に
  進む銘柄を絞る前段のふるい。

入力ソース（config の `source`）:
  - `hist1m`（既定・米国）: moomoo の過去1分足（data/hist1m/{code}.parquet, `python -m
    scalplab.datafeed.history` で取得）。銘柄1ファイル。通常取引時間フィルタは米国東部 RTH。
  - `bars1m`（日本株）: 毎日自動蓄積する yfinance 1分足（data/bars1m/YYYY-MM-DD/{code}.parquet、
    docs/data-contract.md の契約レイアウト）を loader 契約 API 経由で日付横断に連結する。セッション
    フィルタは JP 立会（前場 09:00〜11:30 / 後場 12:30〜15:30、JST）。日本株はティックが取れないため、
    このバー近似が当面の主戦場の検証手段になる（intrabar 情報は無い＝感度は本命ティック版に劣る）。

過学習ガード（本スキャンの肝）:
  銘柄の並び順は「グリッド中の **ベスト値**」ではなく「グリッド中央値」で決める。ベスト値は
  たまたま噛み合った 1 組を拾いやすく、少数データでは過大評価になる。中央値は多数のパラメータ組で
  安定して効くかを見るため、ふるいとして過学習に強い。

前処理の判断メモ:
  - RTH フィルタ: rth_only なら 09:31〜16:00（米国東部）の通常取引時間だけに絞る。時間外は
    スプレッドが広く値動きの性質が別物なので除外する。
  - 指標は **セッション日ごとに** 計算する（日をまたいで連結しない）。オーバーナイトギャップが
    SAR/ATR/RCI に混入して偽シグナルを生むのを避けるため。各日の先頭ウォームアップ本は無効。
  - コスト: net = gross - cost_bp（片道概算）。エントリー品質の相対比較用途。
"""
from __future__ import annotations

import argparse
import itertools
from datetime import date as date_t
from datetime import datetime
from datetime import timedelta
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from scalplab.datafeed import history
from scalplab.store import loader
from scalplab.strategy.rci_impulse_bar import DEFAULT_EVAL_BARS, bar_signals

# 米国東部の通常取引時間（分刻み）。バーの開始時刻がこの範囲に入るものだけ残す。
RTH_START_MIN = 9 * 60 + 31   # 09:31
RTH_END_MIN = 16 * 60         # 16:00（未満）

# JP 立会時間（分刻み・JST）。前場 09:00〜11:30 / 後場 12:30〜15:30。境界は両端含む（前場引け・
# 大引けのバーを残す）。データは契約どおり JST naive。
JP_MORNING = (9 * 60, 11 * 60 + 30)       # 09:00〜11:30
JP_AFTERNOON = (12 * 60 + 30, 15 * 60 + 30)  # 12:30〜15:30


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def filter_rth(bars: pd.DataFrame) -> pd.DataFrame:
    """バー開始時刻が 09:31〜16:00（米国東部）に入る行だけ残す。"""
    if bars is None or len(bars) == 0:
        return bars
    dt = pd.to_datetime(bars["time"])
    minutes = dt.dt.hour * 60 + dt.dt.minute
    mask = (minutes >= RTH_START_MIN) & (minutes < RTH_END_MIN)
    return bars[mask].reset_index(drop=True)


def filter_jp_session(bars: pd.DataFrame) -> pd.DataFrame:
    """バー開始時刻が JP 立会（前場 09:00〜11:30 / 後場 12:30〜15:30、JST）に入る行だけ残す。

    場外・昼休み（11:30〜12:30）のバーを落とす。境界は両端含む（前場引け・大引けのバーを残す）。
    """
    if bars is None or len(bars) == 0:
        return bars
    dt = pd.to_datetime(bars["time"])
    minutes = dt.dt.hour * 60 + dt.dt.minute
    in_am = (minutes >= JP_MORNING[0]) & (minutes <= JP_MORNING[1])
    in_pm = (minutes >= JP_AFTERNOON[0]) & (minutes <= JP_AFTERNOON[1])
    return bars[in_am | in_pm].reset_index(drop=True)


def iter_jp_sessions(bars: pd.DataFrame):
    """JP バーを (立会日, 前場/後場) ごとに分割して yield する（filter_jp_session 済みを前提）。

    なぜ前場・後場を割るか:
      - 昼休み（約1時間）を挟んで連結すると SAR/ATR/RCI が昼休みギャップを1分の急変と誤認して
        偽シグナルを生む（オーバーナイトと同じ混入問題）。半場ごとに独立計算して回避する。
      - 前場引けバーで発火しても次バー始値は後場始値＝昼休み跨ぎになり、保有30秒〜1分の本手法の
        想定外。半場に区切れば前場引けバーは「次バーが無い」ため構造的にエントリー不成立となり、
        昼休みを跨ぐエントリー・評価が発生しない（別途の除外ロジックを持たずに済む）。
    """
    if bars is None or len(bars) == 0:
        return
    b = bars.reset_index(drop=True)
    dt = pd.to_datetime(b["time"])
    dates = dt.dt.date
    minutes = dt.dt.hour * 60 + dt.dt.minute
    # フィルタ済みなら昼休みバーは無いので、前場引け(11:30)を超えるか否かで前場/後場を分けられる。
    half = (minutes > JP_MORNING[1]).astype(int)  # 0=前場 / 1=後場
    for _key, g in b.groupby([dates, half], sort=True):
        yield g.reset_index(drop=True)


def expand_grid(grid: dict) -> list[dict]:
    """{key: [values]} のデカルト積を dict のリストに展開する。"""
    keys = sorted(grid)
    value_lists = [grid[k] if isinstance(grid[k], list) else [grid[k]] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*value_lists)]


def iter_session_days(bars: pd.DataFrame):
    """バーをセッション日（米国東部の日付）ごとに分割して yield する。"""
    if bars is None or len(bars) == 0:
        return
    dates = pd.to_datetime(bars["time"]).dt.date
    for _d, g in bars.groupby(dates, sort=True):
        yield g.reset_index(drop=True)


def recent_bars1m_dates(days: int) -> list[str]:
    """data/bars1m 配下の日付ディレクトリを新しい順に最大 days 件選び、昇順で返す（直近 N 日分）。

    米国 hist1m の「today - days 日」カレンダー起点と違い、bars1m は録画済みの日付ディレクトリを
    そのまま数える（土日祝や欠損日をまたいでも「直近 N 営業日分」を安定して拾える）。
    """
    d = loader.DATA_DIR / "bars1m"
    if not d.exists():
        return []
    all_dates = sorted((p.name for p in d.iterdir() if p.is_dir()), reverse=True)
    return sorted(all_dates[: int(days)])


def auto_symbols_bars1m(dates: list[str], session: str | None = None) -> list[str]:
    """選択した日付範囲の bars1m に録画済みの銘柄コードを列挙する（symbols: auto の実体）。

    session が与えられればその市場（jp→'JP.' / us→'US.'）の銘柄だけに絞る。日本株スクリーニングは
    録画済みの全 JP 銘柄を自動対象化したい（固定列挙を保守したくない）ため、session=jp で JP.* を拾う。
    """
    codes: set[str] = set()
    for d in dates:
        codes.update(loader.list_recorded(d, kind="bars1m"))
    prefix = {"jp": "JP.", "us": "US."}.get(session or "")
    if prefix:
        codes = {c for c in codes if c.startswith(prefix)}
    return sorted(codes)


def load_bars1m_history(code: str, dates: list[str]) -> pd.DataFrame:
    """指定日付の bars1m を loader 契約 API 経由で読み、time 昇順に連結して返す。

    欠損日（その銘柄がその日録画されていない）はスキップする。1件も無ければ空 DataFrame。
    契約スキーマ（loader.load_bars1m が enforce 済み）で返るので下流はそのまま扱える。
    """
    parts = []
    for d in dates:
        try:
            parts.append(loader.load_bars1m(d, code))
        except FileNotFoundError:
            continue
    if not parts:
        return pd.DataFrame(columns=loader.BARS1M_COLUMNS)
    return pd.concat(parts, ignore_index=True).sort_values("time").reset_index(drop=True)


def _combo_metrics(entries: pd.DataFrame, eval_bars) -> dict:
    """1 パラメータ組の entries を集計する（シグナル数・ホライズン別 net 期待値と勝率）。"""
    n = int(len(entries))
    out = {
        "n": n,
        "n_long": int((entries["direction"] == "long").sum()) if n else 0,
        "n_short": int((entries["direction"] == "short").sum()) if n else 0,
    }
    for k in eval_bars:
        col = f"ret_{k}bar_net"
        net = entries[col].dropna() if (n and col in entries.columns) else pd.Series([], dtype=float)
        out[f"exp{k}_net"] = float(net.mean()) if len(net) else float("nan")
        out[f"win{k}"] = float((net > 0).mean()) if len(net) else float("nan")
    return out


def _aggregate_symbol(code: str, combo_stats: list[dict], eval_bars, min_signals: int) -> dict:
    """銘柄内の全パラメータ組を 1 行にまとめる（中央値ランキング + ベスト組の抜粋）。"""
    ns = np.array([c["n"] for c in combo_stats], dtype=float)
    row = {
        "code": code,
        "n_signals_total": int(ns.sum()) if len(ns) else 0,
        "n_signals_median": float(np.median(ns)) if len(ns) else 0.0,
        "n_long_total": int(sum(c["n_long"] for c in combo_stats)),
        "n_short_total": int(sum(c["n_short"] for c in combo_stats)),
    }
    for k in eval_bars:
        exps = np.array([c[f"exp{k}_net"] for c in combo_stats], dtype=float)
        wins = np.array([c[f"win{k}"] for c in combo_stats], dtype=float)
        row[f"median_exp{k}_net_bp"] = float(np.nanmedian(exps)) if np.isfinite(exps).any() else float("nan")
        row[f"median_win{k}"] = float(np.nanmedian(wins)) if np.isfinite(wins).any() else float("nan")

    # ベスト組: +1bar net 期待値が最大。min_signals を満たす組を優先し、無ければ全組から選ぶ。
    def _e1(c):
        e = c["exp1_net"]
        return e if np.isfinite(e) else float("-inf")

    eligible = [c for c in combo_stats if c["n"] >= min_signals and np.isfinite(c["exp1_net"])]
    pool = eligible if eligible else combo_stats
    best = max(pool, key=_e1) if pool else None
    if best is not None:
        row["best_rci_period"] = best["combo"].get("rci_period")
        row["best_slope_threshold"] = best["combo"].get("slope_threshold")
        row["best_eff_min"] = best["combo"].get("eff_min")
        row["best_n"] = int(best["n"])
        row["best_exp1_net_bp"] = float(best["exp1_net"]) if np.isfinite(best["exp1_net"]) else float("nan")
        row["best_win1"] = float(best["win1"]) if np.isfinite(best["win1"]) else float("nan")
        row["best_n_long"] = int(best["n_long"])
        row["best_n_short"] = int(best["n_short"])
    return row


def screen_symbol(
    code: str,
    bars: pd.DataFrame,
    combos: list[dict],
    fixed: dict,
    cost_bp: float,
    eval_bars=DEFAULT_EVAL_BARS,
    min_signals: int = 30,
    rth_only: bool = True,
    session: str | None = None,
) -> dict:
    """1 銘柄のバーを全パラメータ組で評価し、ランキング 1 行（dict）を返す。

    session='jp' のときは JP 立会フィルタ + 前場/後場ごとの分割（昼休み混入・跨ぎを回避）を使う。
    それ以外は従来どおり（rth_only なら米国東部 RTH フィルタ + 日次分割）。
    """
    eval_bars = tuple(int(k) for k in eval_bars)
    if session == "jp":
        bars = filter_jp_session(bars)
        day_frames = list(iter_jp_sessions(bars))
    else:
        if rth_only:
            bars = filter_rth(bars)
        day_frames = list(iter_session_days(bars))

    combo_stats = []
    for combo in combos:
        params = {**fixed, **combo}
        parts = [
            bar_signals(day, params, code=code, cost_bp=cost_bp, eval_bars=eval_bars)
            for day in day_frames
        ]
        parts = [p for p in parts if len(p)]
        entries = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        m = _combo_metrics(entries, eval_bars)
        m["combo"] = combo
        combo_stats.append(m)

    return _aggregate_symbol(code, combo_stats, eval_bars, min_signals)


def ranking_columns(eval_bars) -> list[str]:
    """ranking.csv の列順を返す。"""
    cols = ["code", "n_signals_total", "n_signals_median"]
    for k in eval_bars:
        cols += [f"median_exp{k}_net_bp", f"median_win{k}"]
    cols += [
        "best_exp1_net_bp", "best_win1",
        "best_rci_period", "best_slope_threshold", "best_eff_min", "best_n",
        "best_n_long", "best_n_short", "n_long_total", "n_short_total",
    ]
    return cols


# 手法適性の判定しきい値（1分足レンジ中央値 bp）。根拠（2026-07-20 の JP 51 銘柄×champion 分析）:
#   - 勝てる/勝てない銘柄の差を最もよく説明したのはボラ＝レンジ中央値
#     （売買代金・価格・ジグザグ度は弱い予測子）。
#   - 予測力の検証（学習日レンジだけで銘柄を選ぶ→検証日成績）: レンジ中央値が 15bp 以上の銘柄群と
#     除外群で検証日成績が分離した（サンプルは小さく、再検証が前提）。
#   - 経済的解釈: 片道コスト 3bp に対しレンジが 5 倍（15bp）無いと 2 分保有で往復コストを回収できない。
# しきい値はこの分析に基づく定数。教師データ（reports/walkforward-jp.csv・注釈）が溜まったら見直す。
APTITUDE_RANGE_BP = (15.0, 25.0)   # (○ の下限, ◎ の下限)。< 15bp は ×（ボラ不足で不適）。


def aptitude_badge(range_med_bp) -> str:
    """レンジ中央値(bp)から手法適性バッジを返す（◎: ≥25 / ○: ≥15 / ×: <15）。

    range_med_bp が None/NaN（レンジ未算出＝データ無し）のときは空文字（判定不能）を返す。
    """
    if range_med_bp is None:
        return ""
    v = float(range_med_bp)
    if not np.isfinite(v):
        return ""
    lo, hi = APTITUDE_RANGE_BP
    if v >= hi:
        return "◎"
    if v >= lo:
        return "○"
    return "×"


def aptitude_metrics(bars: pd.DataFrame) -> dict:
    """銘柄の「手法適性」を数値化する（参加者が少ない・ボラが無い銘柄を弾くための実測）。

    この手法は出来高・約定が乏しくボラが小さい銘柄では機能しない（要件）。銘柄選びを
    数字で判断できるよう、成績とは別に以下を各銘柄で出す:
      - avg_turnover_oku: 1日あたり平均売買代金（億円）＝ sum(turnover)/立会日数/1e8。
        turnover 列が無い場合は close×volume で近似する。
      - range_med_bp: 1分足レンジ (high-low)/close の中央値（bp）＝ボラ実測（動きの大きさ）。
      - bars_per_day: 1日あたりバー数（データの厚み・約定の連続性の粗い代理）。
    """
    n = len(bars)
    if n == 0:
        return {"avg_turnover_oku": float("nan"), "range_med_bp": float("nan"), "bars_per_day": 0.0}
    days = max(1, int(pd.to_datetime(bars["time"]).dt.date.nunique()))
    close = bars["close"].to_numpy(dtype=float)
    if "turnover" in bars.columns:
        turnover = bars["turnover"].to_numpy(dtype=float)
    else:
        turnover = close * bars["volume"].to_numpy(dtype=float)
    avg_turnover_oku = float(np.nansum(turnover)) / days / 1e8
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        rng_bp = (high - low) / close * 1e4
    rng_bp = rng_bp[np.isfinite(rng_bp)]
    range_med_bp = float(np.median(rng_bp)) if len(rng_bp) else float("nan")
    return {
        "avg_turnover_oku": avg_turnover_oku,
        "range_med_bp": range_med_bp,
        "bars_per_day": float(n) / days,
    }


def screen_run_bars1m(
    codes: list[str],
    dates: list[str],
    params: dict,
    cost_bp: float,
    eval_bars=DEFAULT_EVAL_BARS,
    tick_counts: dict[str, float] | None = None,
) -> tuple[pd.DataFrame, int]:
    """単一パラメータ組を全 JP 銘柄の bars1m（指定日付範囲）に適用し、銘柄別ランキングを返す。

    機能B（WebUI「この条件で実行」）の実体。前場/後場を独立計算（session='jp'）し、成績（単一組
    なので中央値＝その組・ベスト＝その組）に加えて銘柄適性（売買代金・ボラ・bars/日・最新Tick回数）を
    各行へ付ける。戻り値は (ranking: DataFrame, n_missing: int)。列は ranking.csv 互換＋適性列。
    """
    eval_bars = tuple(int(k) for k in eval_bars)
    tick_counts = tick_counts or {}
    rows: list[dict] = []
    n_missing = 0
    for code in codes:
        bars = load_bars1m_history(code, dates)
        if bars is None or bars.empty:
            n_missing += 1
            continue
        sess = filter_jp_session(bars)
        day_frames = list(iter_jp_sessions(sess))
        parts = [bar_signals(day, params, code=code, cost_bp=cost_bp, eval_bars=eval_bars)
                 for day in day_frames]
        parts = [p for p in parts if len(p)]
        entries = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        m = _combo_metrics(entries, eval_bars)
        row = {
            "code": code,
            "n_signals_total": m["n"], "n_signals_median": float(m["n"]),
            "n_long_total": m["n_long"], "n_short_total": m["n_short"],
            "best_exp1_net_bp": m["exp1_net"], "best_win1": m["win1"],
            "best_rci_period": params.get("rci_period"),
            "best_slope_threshold": params.get("slope_threshold"),
            "best_eff_min": params.get("eff_min"),
            "best_n": m["n"], "best_n_long": m["n_long"], "best_n_short": m["n_short"],
        }
        for k in eval_bars:
            row[f"median_exp{k}_net_bp"] = m[f"exp{k}_net"]
            row[f"median_win{k}"] = m[f"win{k}"]
        row.update(aptitude_metrics(bars))
        row["aptitude"] = aptitude_badge(row["range_med_bp"])   # レンジ中央値からの手法適性判定
        tc = tick_counts.get(code)
        row["tick_count"] = float(tc) if tc is not None else float("nan")
        rows.append(row)

    cols = ranking_columns(eval_bars) + ["avg_turnover_oku", "range_med_bp", "bars_per_day", "tick_count", "aptitude"]
    if rows:
        ranking = pd.DataFrame(rows).reindex(columns=cols)
        ranking = ranking.sort_values("median_exp1_net_bp", ascending=False, na_position="last").reset_index(drop=True)
    else:
        ranking = pd.DataFrame(columns=cols)
    return ranking, n_missing


def _fmt(v, nd=2):
    if v is None or (isinstance(v, float) and (np.isnan(v))):
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def render_summary(ranking: pd.DataFrame, cfg: dict, eval_bars, n_missing: int, elapsed: float) -> str:
    """summary.md を生成する（中央値ランキング + 総計 + 注意書き）。"""
    min_signals = int(cfg.get("min_signals", 30))
    cost_bp = float(cfg.get("cost_bp", 0.0))
    days = int(cfg.get("days", 21))
    session = cfg.get("session")
    if session == "jp":
        sess_desc = ("セッション: 前場 09:00〜11:30 / 後場 12:30〜15:30（JST）。指標は前場・後場ごとに"
                     "計算し、昼休みを跨ぐエントリー・評価は除外（昼休み・オーバーナイト混入回避）")
    else:
        sess_desc = (f"RTH フィルタ: {cfg.get('rth_only', True)}（09:31〜16:00 米国東部）。"
                     "指標はセッション日ごとに計算（オーバーナイト混入回避）")

    qual = ranking[ranking["n_signals_median"] >= min_signals].copy()
    qual = qual.sort_values("median_exp1_net_bp", ascending=False, na_position="last").reset_index(drop=True)

    total_signals = int(ranking["n_signals_total"].sum()) if len(ranking) else 0
    total_long = int(ranking["n_long_total"].sum()) if len(ranking) else 0
    total_short = int(ranking["n_short_total"].sum()) if len(ranking) else 0

    lines = [
        "# クロス銘柄スクリーニング結果（RCI×パラボリック手法・バー近似版）",
        "",
        "## 読み方（過学習ガード）",
        "",
        "- 並び順は **グリッド中央値の +1bar net 期待値**（降順）。グリッドのベスト値ではなく中央値で",
        "  ランキングするのは、ベスト値が少数データで過大評価になりやすいため。多数のパラメータ組で",
        "  安定して効く銘柄を上に出す。",
        "- これは **バー近似**（確定1分足の RCI 差分）であり、本命のティック駆動版（足内 RCI）より",
        "  感度が落ちる。絶対期待値ではなく銘柄間の相対比較として読み、上位は本命ティック検証に回す。",
        f"- コスト: net = gross - {cost_bp:.1f}bp（片道概算）。{sess_desc}。",
        "",
        "## 総計",
        "",
        f"- 評価銘柄: {len(ranking)} 銘柄（キャッシュ欠損でスキップ: {n_missing} 銘柄）",
        f"- 直近 {days} 日 / グリッド {len(expand_grid(cfg.get('grid', {})))} 組 / min_signals={min_signals}",
        f"- 全グリッド総シグナル数: {total_signals}（ロング {total_long} / ショート {total_short}）",
        f"- 採用（中央値シグナル数 >= {min_signals}）: {len(qual)} 銘柄 / 所要 {elapsed:.0f}s",
        "",
        "## ランキング（中央値 +1bar net 期待値 降順）",
        "",
    ]

    header = ["#", "code", "median_sig", "med_exp1_net_bp", "med_win1", "med_exp2_net_bp", "med_win2",
              "best_exp1_net_bp", "best(rci/slope/eff)", "best_n", "long/short(best)"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    if len(qual):
        for i, r in qual.iterrows():
            best_param = f"{_fmt(r.get('best_rci_period'))}/{_fmt(r.get('best_slope_threshold'))}/{_fmt(r.get('best_eff_min'),1)}"
            ls = f"{_fmt(r.get('best_n_long'))}/{_fmt(r.get('best_n_short'))}"
            cells = [
                str(i + 1), str(r["code"]), _fmt(r["n_signals_median"], 0),
                _fmt(r["median_exp1_net_bp"]), _fmt(r["median_win1"], 3),
                _fmt(r["median_exp2_net_bp"]), _fmt(r["median_win2"], 3),
                _fmt(r["best_exp1_net_bp"]), best_param, _fmt(r.get("best_n"), 0), ls,
            ]
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - |")

    lines += [
        "",
        "## 注意",
        "",
        "- バー近似のため、足内で急伸する RCI は捉えられない。上位銘柄でも本命ティック検証で",
        "  結果が変わりうる。ここでの役割は「明らかに効かない銘柄を落とす」ふるい。",
        "- コスト仮定（片道概算）とスプレッド未考慮のため、net 期待値の絶対値は楽観側に寄る。",
    ]
    return "\n".join(lines) + "\n"


def _screen_worker(task: dict):
    """1 銘柄を評価してランキング行を返す（Pool から呼ばれる）。データ欠損は None。

    task はソース差を吸収した dict。source='bars1m' は日付横断で bars1m を連結し、それ以外は
    hist1m キャッシュを直近 days 日に絞る。
    """
    code = task["code"]
    if task["source"] == "bars1m":
        bars = load_bars1m_history(code, task["dates"])
    else:
        bars = history.load_cache(code)
        if bars.empty:
            return None
        # キャッシュが days より多くを持つ場合に備え、直近 days 日に絞る
        today = date_t.fromisoformat(task["today_iso"])
        start = today - timedelta(days=int(task["days"]))
        bar_dates = pd.to_datetime(bars["time"]).dt.date
        bars = bars[bar_dates >= start].reset_index(drop=True)
    if bars is None or bars.empty:
        return None
    return screen_symbol(
        code, bars, task["combos"], task["fixed"], task["cost_bp"],
        tuple(task["eval_bars"]), task["min_signals"], task["rth_only"],
        session=task["session"],
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="クロス銘柄スクリーニング（バー近似シグナル）")
    ap.add_argument("--config", default=None, help="設定 yaml（既定 config/screen.yaml）")
    ap.add_argument("--processes", type=int, default=None, help="並列プロセス数")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config) if args.config else _project_root() / "config" / "screen.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    grid = cfg.get("grid", {})
    fixed = dict(cfg.get("fixed", {}))
    cost_bp = float(cfg.get("cost_bp", 0.0))
    min_signals = int(cfg.get("min_signals", 30))
    rth_only = bool(cfg.get("rth_only", True))
    days = int(cfg.get("days", 21))
    eval_bars = tuple(int(k) for k in cfg.get("eval_bars", DEFAULT_EVAL_BARS))
    source = str(cfg.get("source", "hist1m"))
    session = cfg.get("session")  # 例: 'jp'。未指定なら従来（US/hist1m）挙動。

    # bars1m は日付横断で連結するため、対象日付を先に確定する。symbols: auto は録画済み銘柄を自動列挙。
    raw_symbols = cfg.get("symbols", [])
    dates: list[str] = []
    if source == "bars1m":
        dates = recent_bars1m_dates(days)
        if isinstance(raw_symbols, str) and raw_symbols.strip().lower() == "auto":
            symbols = auto_symbols_bars1m(dates, session)
        else:
            symbols = list(raw_symbols)
    else:
        symbols = list(raw_symbols)
    if not symbols:
        raise SystemExit(f"symbols が空です: {cfg_path}")

    combos = expand_grid(grid)
    today_iso = date_t.today().isoformat()
    tasks = [
        {
            "code": c, "combos": combos, "fixed": fixed, "cost_bp": cost_bp,
            "eval_bars": eval_bars, "min_signals": min_signals, "rth_only": rth_only,
            "session": session, "source": source, "days": days,
            "today_iso": today_iso, "dates": dates,
        }
        for c in symbols
    ]
    span = f"dates={len(dates)}" if source == "bars1m" else f"days={days}"
    print(f"=== screen source={source} symbols={len(symbols)} combos={len(combos)} {span} ===")

    started = datetime.now()
    if len(tasks) == 1 or (args.processes is not None and args.processes <= 1):
        results = [_screen_worker(t) for t in tasks]
    else:
        with Pool(processes=args.processes) as pool:
            results = pool.map(_screen_worker, tasks)
    elapsed = (datetime.now() - started).total_seconds()

    rows = [r for r in results if r is not None]
    n_missing = len(symbols) - len(rows)
    ranking = pd.DataFrame(rows)
    if len(ranking):
        ranking = ranking.reindex(columns=ranking_columns(eval_bars))
        ranking = ranking.sort_values("median_exp1_net_bp", ascending=False, na_position="last").reset_index(drop=True)
    else:
        ranking = pd.DataFrame(columns=ranking_columns(eval_bars))

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = _project_root() / "reports" / f"screen-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    ranking.to_csv(out_dir / "ranking.csv", index=False)
    md = render_summary(ranking, cfg, eval_bars, n_missing, elapsed)
    (out_dir / "summary.md").write_text(md, encoding="utf-8")

    qual = int((ranking["n_signals_median"] >= min_signals).sum()) if len(ranking) else 0
    print(f"evaluated={len(rows)}/{len(symbols)} missing_cache={n_missing} qualified(min_signals>={min_signals})={qual}")
    print(f"saved: {out_dir}/ranking.csv, summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
