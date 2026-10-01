"""チャンピオンパラメータの自動ウォークフォワード検証。

なぜ必要か: config/best-params.yaml のチャンピオンは、選定に使ったセッション自体では
インサンプル（過学習の疑い）。毎晩の新しい録画に対して同じパラメータをそのまま流し、
純粋なアウトオブサンプルの成績を積み上げ記録することで、実力を継続的に測る。

呼ばれ方:
  - 自動: record.py の US セッション録画が finalize した直後（`_walkforward_after_finalize`）。
  - 手動/バックフィル: `python -m scalplab.backtest.walkforward --date YYYY-MM-DD [--force]`。

冪等性: 同じ (date, params_hash) が既に reports/walkforward.csv にあれば再実行しない
（recorder のクラッシュ再起動で二重集計されないため）。--force で無視して上書きする。

永続化:
  - reports/walkforward.csv … 1 行 = 1 銘柄 × 1 セッション日（全体行は code=ALL）。
  - reports/walkforward.md  … CSV から毎回再生成（セッション×銘柄の表＋累計サマリー）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from scalplab.backtest import regime
from scalplab.backtest.engine import DEFAULT_HORIZONS, get_loader, run_backtest
from scalplab.backtest.metrics import valid_entries
from scalplab.backtest.screen import filter_jp_session, iter_jp_sessions
from scalplab.strategy.rci_impulse import merge_params
from scalplab.strategy.rci_impulse_bar import DEFAULT_EVAL_BARS, bar_signals, merge_bar_params

# CSV の列順（空セッションでも同スキーマを保つため固定）。
# win60/exp60/med60 の "60" は主評価窓 +60s を表す（config の horizon は既定 60）。
WF_COLUMNS = [
    "date", "code", "n", "win60", "exp60_net_bp", "med60_net_bp",
    "mfe_med_bp", "mae_med_bp", "gross_mean_bp", "params_hash",
]


def _project_root() -> Path:
    """絶対パス埋め込みを避けるため、このファイル位置からルートを相対解決する。"""
    return Path(__file__).resolve().parents[2]


def _config_path() -> Path:
    return _project_root() / "config" / "best-params.yaml"


def _reports_dir() -> Path:
    return _project_root() / "reports"


def params_hash(params: dict) -> str:
    """チャンピオンパラメータの安定ハッシュ（sha1 先頭 8 桁）。

    キー順・数値表現に依存しないよう、sort_keys で正準化した JSON をハッシュする。
    パラメータが変われば別ハッシュ＝別の検証系列として区別できる。
    """
    payload = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]


def _agg_one(df: pd.DataFrame, horizon: int) -> dict:
    """有効エントリー部分集合を 1 指標行に集計する（勝率・期待値・中央値・MFE/MAE・gross平均）。"""
    net_col = f"ret_{horizon}s_net"
    gross_col = f"ret_{horizon}s_gross"
    n = int(len(df))
    net = df[net_col].dropna() if net_col in df.columns else pd.Series(dtype=float)
    gross = df[gross_col].dropna() if gross_col in df.columns else pd.Series(dtype=float)
    return {
        "n": n,
        "win60": float((net > 0).mean()) if len(net) else np.nan,
        "exp60_net_bp": float(net.mean()) if len(net) else np.nan,
        "med60_net_bp": float(net.median()) if len(net) else np.nan,
        "mfe_med_bp": float(df["mfe_bp"].median()) if ("mfe_bp" in df.columns and n) else np.nan,
        "mae_med_bp": float(df["mae_bp"].median()) if ("mae_bp" in df.columns and n) else np.nan,
        "gross_mean_bp": float(gross.mean()) if len(gross) else np.nan,
    }


def aggregate(entries: pd.DataFrame, horizon: int, codes: list[str]) -> dict:
    """entries を全体（overall）と銘柄別（by_code）に集計する（評価打切りは除外）。

    codes に渡した銘柄は、シグナル 0 件でも n=0 の行として必ず含める（録画があった証跡）。
    """
    valid = valid_entries(entries)
    by_code: dict[str, dict] = {}
    for c in codes:
        sub = valid[valid["code"] == c] if len(valid) else valid
        by_code[c] = _agg_one(sub, horizon)
    return {"overall": _agg_one(valid, horizon), "by_code": by_code}


def _load_champion(config_path: Path) -> dict | None:
    """best-params.yaml を読み、params/codes/horizon/hash を返す。欠損時は None。"""
    if not config_path.exists():
        return None
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    params = raw.get("params")
    if not params:
        return None
    codes = [str(c) for c in (raw.get("codes") or [])]
    horizon = int(raw.get("horizon", 60))
    return {
        "params": params,
        "codes": codes,
        "horizon": horizon,
        # 評価条件（horizon）もハッシュに入れる。params だけだと horizon 変更後も同一
        # (date, hash) と判定されて再計算されず、旧条件の数値が「既検証」として残るため。
        "hash": params_hash({**params, "_horizon": horizon}),
    }


def _read_csv(csv_path: Path) -> pd.DataFrame:
    """既存の walkforward.csv を読む。無ければ空（列固定）を返す。"""
    if csv_path.exists():
        return pd.read_csv(csv_path)
    return pd.DataFrame(columns=WF_COLUMNS)


def _already_done(existing: pd.DataFrame, date: str, phash: str) -> bool:
    """同一 (date, params_hash) の集計が既にあるか（冪等判定）。"""
    if not len(existing):
        return False
    m = (existing["date"].astype(str) == date) & (existing["params_hash"].astype(str) == phash)
    return bool(m.any())


def _rows_for_session(date: str, phash: str, agg: dict, codes: list[str]) -> list[dict]:
    """1 セッション分の CSV 行（銘柄別 + ALL）を組み立てる。"""
    rows: list[dict] = []
    for c in codes:
        r = dict(agg["by_code"][c])
        r.update(date=date, code=c, params_hash=phash)
        rows.append(r)
    r = dict(agg["overall"])
    r.update(date=date, code="ALL", params_hash=phash)
    rows.append(r)
    return [{k: row.get(k) for k in WF_COLUMNS} for row in rows]


def _fmt(v, nd=2, pct=False) -> str:
    """md 用の数値整形（NaN/None は "-"）。"""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    if pct:
        return f"{float(v) * 100:.1f}%"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def render_markdown(df: pd.DataFrame) -> str:
    """CSV 全体から walkforward.md を再生成する（セッション×銘柄表＋累計サマリー）。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# ウォークフォワード検証（チャンピオンパラメータ・アウトオブサンプル）",
        "",
        f"最終更新: {ts}",
        "",
        "config/best-params.yaml のチャンピオンを、選定に使っていない各セッションへそのまま適用した"
        "純アウトオブサンプルの成績。勝率は +60s ネット>0 の割合、期待値は +60s ネット平均(bp)。",
        "",
    ]

    # 累計サマリー（code=ALL 行の n 加重で合算）
    all_rows = df[df["code"].astype(str) == "ALL"].copy() if len(df) else df
    lines += ["## 累計サマリー", ""]
    if len(all_rows):
        n = pd.to_numeric(all_rows["n"], errors="coerce").fillna(0.0)
        win = pd.to_numeric(all_rows["win60"], errors="coerce")
        exp = pd.to_numeric(all_rows["exp60_net_bp"], errors="coerce")
        n_tot = float(n.sum())
        sessions = int(all_rows["date"].astype(str).nunique())
        comb_win = float((win * n).sum() / n_tot) if n_tot > 0 else np.nan
        comb_exp = float((exp * n).sum() / n_tot) if n_tot > 0 else np.nan
        lines += [
            f"- セッション数: {sessions}",
            f"- 総シグナル数: {int(n_tot)}",
            f"- 合算勝率(+60s net>0): {_fmt(comb_win, pct=True)}",
            f"- 平均期待値(+60s net, n加重): {_fmt(comb_exp)} bp",
            "",
        ]
    else:
        lines += ["- (まだ記録なし)", ""]

    # セッション×銘柄の表（date 昇順、各 date 内は銘柄→ALL の順）
    lines += [
        "## セッション×銘柄",
        "",
        "| date | code | n | win60 | exp60_net_bp | med60_net_bp | mfe_med_bp | mae_med_bp | gross_mean_bp | hash |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    if len(df):
        d = df.copy()
        d["_all"] = (d["code"].astype(str) == "ALL").astype(int)
        d = d.sort_values(["date", "_all", "code"], kind="stable")
        for _, r in d.iterrows():
            lines.append(
                "| " + " | ".join([
                    str(r["date"]), str(r["code"]),
                    _fmt(r["n"], 0) if not pd.isna(r["n"]) else "-",
                    _fmt(r["win60"], pct=True),
                    _fmt(r["exp60_net_bp"]),
                    _fmt(r["med60_net_bp"]),
                    _fmt(r["mfe_med_bp"]),
                    _fmt(r["mae_med_bp"]),
                    _fmt(r["gross_mean_bp"]),
                    str(r["params_hash"]),
                ]) + " |"
            )
    else:
        lines.append("| - | - | - | - | - | - | - | - | - | - |")

    return "\n".join(lines) + "\n"


def run_walkforward(date: str, *, config_path: Path | None = None,
                    reports_dir: Path | None = None, loader=None,
                    force: bool = False) -> dict | None:
    """指定セッション日をチャンピオンパラメータで検証し、結果を積み上げる。

    戻り値は集計 dict（overall/by_code を含む）。次の場合は None を返す（graceful skip）:
      - best-params.yaml が無い／params 空
      - 対象銘柄の録画が 1 件も無い
      - 同一 (date, params_hash) が既にあり force=False（冪等スキップ）
    """
    config_path = config_path or _config_path()
    reports_dir = reports_dir or _reports_dir()

    champ = _load_champion(config_path)
    if champ is None:
        return None

    loader = loader or get_loader()
    recorded = set(loader.list_recorded(date)) if hasattr(loader, "list_recorded") else set()
    codes = [c for c in champ["codes"] if c in recorded]
    if not codes:
        return None

    csv_path = Path(reports_dir) / "walkforward.csv"
    md_path = Path(reports_dir) / "walkforward.md"
    phash = champ["hash"]

    existing = _read_csv(csv_path)
    if not force and _already_done(existing, date, phash):
        return None

    horizon = int(champ["horizon"])
    params = merge_params(champ["params"])
    # horizon 列を確実に生成するため主評価窓を horizons に含める。
    horizons = tuple(sorted(set(DEFAULT_HORIZONS) | {horizon}))
    entries = run_backtest(date, codes, params=params, loader=loader,
                           horizons=horizons, slippage_ticks=params.get("slippage_ticks", 0))
    agg = aggregate(entries, horizon, codes)
    new_rows = _rows_for_session(date, phash, agg, codes)

    # force 再実行時は同一 (date, hash) を差し替え（二重計上を避ける）。
    if len(existing):
        keep = ~((existing["date"].astype(str) == date)
                 & (existing["params_hash"].astype(str) == phash))
        existing = existing[keep]
    merged = pd.concat([existing, pd.DataFrame(new_rows, columns=WF_COLUMNS)], ignore_index=True)

    Path(reports_dir).mkdir(parents=True, exist_ok=True)
    merged.to_csv(csv_path, index=False)
    md_path.write_text(render_markdown(merged), encoding="utf-8")

    return {"date": date, "params_hash": phash, **agg}


# ============================================================================
# 日本株（bars1m バー近似版）ウォークフォワード
# ============================================================================
# 米国版（ティック・run_backtest・+60s ネット）とは別系列。日本株はティックが取れないため、
# 確定 1 分足のバー近似（rci_impulse_bar.bar_signals）にチャンピオン（config/best-params-jp.yaml）を
# 適用し、前場/後場を独立計算（screen.iter_jp_sessions で昼休み跨ぎを構造的に除外）して銘柄別 + ALL
# 行を reports/walkforward-jp.csv へ積み上げる。冪等・出自規約・銘柄別行の思想は米国版と同じ。


def _config_path_jp() -> Path:
    return _project_root() / "config" / "best-params-jp.yaml"


def wf_jp_columns(eval_bars) -> list[str]:
    """walkforward-jp.csv の列順（eval_bars から生成。空セッションでも同スキーマを保つ）。"""
    cols = ["date", "code", "n"]
    for k in eval_bars:
        cols += [f"win{k}", f"exp{k}_net_bp", f"med{k}_net_bp"]
    cols += ["gross_mean_bp", "params_hash"]
    return cols


def _agg_one_jp(df: pd.DataFrame, eval_bars) -> dict:
    """有効エントリー（打切り除外済み前提）を 1 指標行に集計する（各 +k バーの勝率/期待値/中央値）。

    gross 平均は先頭ホライズン（eval_bars[0]、既定 +1 バー）を代表として出す。列が無い/空でも NaN。
    """
    out = {"n": int(len(df))}
    for k in eval_bars:
        col = f"ret_{k}bar_net"
        net = df[col].dropna() if col in df.columns else pd.Series(dtype=float)
        out[f"win{k}"] = float((net > 0).mean()) if len(net) else np.nan
        out[f"exp{k}_net_bp"] = float(net.mean()) if len(net) else np.nan
        out[f"med{k}_net_bp"] = float(net.median()) if len(net) else np.nan
    gcol = f"ret_{eval_bars[0]}bar_gross"
    gross = df[gcol].dropna() if gcol in df.columns else pd.Series(dtype=float)
    out["gross_mean_bp"] = float(gross.mean()) if len(gross) else np.nan
    return out


def aggregate_jp(entries: pd.DataFrame, eval_bars, codes: list[str]) -> dict:
    """entries を全体（overall）と銘柄別（by_code）に集計する（評価打切りは valid_entries で除外）。

    codes に渡した銘柄は、シグナル 0 件でも n=0 の行として必ず含める（録画があった証跡）。
    """
    valid = valid_entries(entries)
    by_code: dict[str, dict] = {}
    for c in codes:
        sub = valid[valid["code"] == c] if len(valid) else valid
        by_code[c] = _agg_one_jp(sub, eval_bars)
    return {"overall": _agg_one_jp(valid, eval_bars), "by_code": by_code}


def _load_champion_jp(config_path: Path) -> dict | None:
    """best-params-jp.yaml を読み、params/codes/session/cost_bp/eval_bars/hash を返す。欠損時 None。"""
    if not config_path.exists():
        return None
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    params = raw.get("params")
    if not params:
        return None
    raw_codes = raw.get("codes")
    if isinstance(raw_codes, str):
        codes: str | list[str] = raw_codes.strip().lower()  # 'auto'
    else:
        codes = [str(c) for c in (raw_codes or [])]
    eval_bars = tuple(int(k) for k in (raw.get("eval_bars") or DEFAULT_EVAL_BARS))
    cost_bp = float(raw.get("cost_bp", 0.0))
    return {
        "params": params,
        "codes": codes,
        "session": str(raw.get("session", "jp")),
        "cost_bp": cost_bp,
        "eval_bars": eval_bars,
        # 評価条件（cost_bp / eval_bars）もハッシュに入れる。params だけだと往復コストや評価
        # ホライズンを変えても同一 (date, hash) と判定されて再計算されず、旧コスト前提の数値が
        # 「既検証」として CSV に残り続ける（チャンピオン差し替え判断の根拠が静かに陳腐化する）。
        "hash": params_hash({**params, "_cost_bp": cost_bp, "_eval_bars": list(eval_bars)}),
    }


def _rows_for_session_jp(date: str, phash: str, agg: dict, codes: list[str], eval_bars) -> list[dict]:
    """1 セッション分の CSV 行（銘柄別 + ALL）を組み立てる。"""
    cols = wf_jp_columns(eval_bars)
    rows: list[dict] = []
    for c in codes:
        r = dict(agg["by_code"][c])
        r.update(date=date, code=c, params_hash=phash)
        rows.append(r)
    r = dict(agg["overall"])
    r.update(date=date, code="ALL", params_hash=phash)
    rows.append(r)
    return [{k: row.get(k) for k in cols} for row in rows]


def render_markdown_jp(df: pd.DataFrame, eval_bars) -> str:
    """CSV 全体から walkforward-jp.md を再生成する（セッション×銘柄表＋累計サマリー）。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    first = eval_bars[0]
    lines = [
        "# ウォークフォワード検証（日本株チャンピオン・バー近似・アウトオブサンプル）",
        "",
        f"最終更新: {ts}",
        "",
        "config/best-params-jp.yaml のチャンピオン（トゲ反転単独モード）を、各セッションの yfinance 1 分足"
        "（bars1m）へそのまま適用した純アウトオブサンプルの成績。前場/後場を独立計算し昼休み跨ぎは除外。"
        f"勝率は +{first} バー ネット>0 の割合、期待値は +{first} バー ネット平均(bp)。",
        "",
        "注意: この手法は銘柄依存が強く、全銘柄一律では中央値マイナスになりやすい（ALL 行はあくまで参考）。"
        "銘柄別行から「効く銘柄」を継続的に見極めること。",
        "",
    ]

    all_rows = df[df["code"].astype(str) == "ALL"].copy() if len(df) else df
    lines += ["## 累計サマリー（ALL 行の n 加重）", ""]
    if len(all_rows):
        n = pd.to_numeric(all_rows["n"], errors="coerce").fillna(0.0)
        win = pd.to_numeric(all_rows[f"win{first}"], errors="coerce")
        exp = pd.to_numeric(all_rows[f"exp{first}_net_bp"], errors="coerce")
        n_tot = float(n.sum())
        sessions = int(all_rows["date"].astype(str).nunique())
        comb_win = float((win * n).sum() / n_tot) if n_tot > 0 else np.nan
        comb_exp = float((exp * n).sum() / n_tot) if n_tot > 0 else np.nan
        lines += [
            f"- セッション数: {sessions}",
            f"- 総シグナル数: {int(n_tot)}",
            f"- 合算勝率(+{first}bar net>0): {_fmt(comb_win, pct=True)}",
            f"- 平均期待値(+{first}bar net, n加重): {_fmt(comb_exp)} bp",
            "",
        ]
    else:
        lines += ["- (まだ記録なし)", ""]

    metric_cols = []
    for k in eval_bars:
        metric_cols += [f"win{k}", f"exp{k}_net_bp", f"med{k}_net_bp"]
    header = ["date", "code", "n"] + metric_cols + ["gross_mean_bp", "hash"]
    lines += [
        "## セッション×銘柄",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(header)) + "|",
    ]
    if len(df):
        d = df.copy()
        d["_all"] = (d["code"].astype(str) == "ALL").astype(int)
        d = d.sort_values(["date", "_all", "code"], kind="stable")
        for _, r in d.iterrows():
            cells = [str(r["date"]), str(r["code"]),
                     _fmt(r["n"], 0) if not pd.isna(r["n"]) else "-"]
            for k in eval_bars:
                cells += [_fmt(r[f"win{k}"], pct=True), _fmt(r[f"exp{k}_net_bp"]),
                          _fmt(r[f"med{k}_net_bp"])]
            cells += [_fmt(r["gross_mean_bp"]), str(r["params_hash"])]
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append("| " + " | ".join(["-"] * len(header)) + " |")

    return "\n".join(lines) + "\n"


def run_walkforward_jp(date: str, *, config_path: Path | None = None,
                       reports_dir: Path | None = None, loader=None,
                       force: bool = False) -> dict | None:
    """指定セッション日を日本株チャンピオンで検証し、reports/walkforward-jp.* に積み上げる。

    戻り値は集計 dict（overall/by_code/eval_bars を含む）。次の場合は None（graceful skip）:
      - best-params-jp.yaml が無い／params 空
      - 対象 JP 銘柄の bars1m が 1 件も無い
      - 同一 (date, params_hash) が既にあり force=False（冪等スキップ）
    """
    config_path = config_path or _config_path_jp()
    reports_dir = reports_dir or _reports_dir()

    champ = _load_champion_jp(config_path)
    if champ is None:
        return None

    loader = loader or get_loader()
    recorded = set(loader.list_recorded(date, kind="bars1m")) if hasattr(loader, "list_recorded") else set()
    if champ["codes"] == "auto":
        codes = sorted(c for c in recorded if c.startswith("JP."))
    else:
        codes = [c for c in champ["codes"] if c in recorded]
    if not codes:
        return None

    eval_bars = champ["eval_bars"]
    cols = wf_jp_columns(eval_bars)
    csv_path = Path(reports_dir) / "walkforward-jp.csv"
    md_path = Path(reports_dir) / "walkforward-jp.md"
    phash = champ["hash"]

    existing = pd.read_csv(csv_path) if csv_path.exists() else pd.DataFrame(columns=cols)
    if not force and _already_done(existing, date, phash):
        return None

    # eval_bars を縮める変更（例 (1,2,3)→(1,2)）をしても過去日の win3/exp3_net_bp 等を落とさない
    # よう、既存 CSV にしか無い列を末尾に足して union で保存する（積み上げ型 OOS 記録の履歴保護）。
    cols = cols + [c for c in existing.columns if c not in cols]

    params = merge_bar_params(champ["params"])
    cost_bp = champ["cost_bp"]
    frames = []
    for c in codes:
        try:
            bars = loader.load_bars1m(date, c)
        except FileNotFoundError:
            continue
        # 前場/後場を独立計算（昼休み・オーバーナイト混入を回避）。前場引けバーは次バーが無く
        # 構造的にエントリー不成立になるため、昼休み跨ぎのエントリー・評価が発生しない。
        sess = filter_jp_session(bars)
        parts = [bar_signals(day, params, code=c, cost_bp=cost_bp, eval_bars=eval_bars)
                 for day in iter_jp_sessions(sess)]
        parts = [p for p in parts if len(p)]
        if parts:
            frames.append(pd.concat(parts, ignore_index=True))
    entries = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    agg = aggregate_jp(entries, eval_bars, codes)
    new_rows = _rows_for_session_jp(date, phash, agg, codes, eval_bars)

    # META 行（レジーム選択に従った当日成績）。直前3日で「順張り/逆張り」と◎銘柄を決め、その系を当日へ
    # 適用した成績を code="META" で1行足す。冪等キーは champion と同じ (date, phash)＝この walkforward 実行の
    # バージョンに束ねる（force 削除・_already_done 判定が META も一緒に扱う）。META 計算の失敗は本体集計を壊さない。
    try:
        meta_entries, _pick = regime.meta_entries_for_date(date, loader=loader, cost_bp=cost_bp)
        meta_agg = _agg_one_jp(valid_entries(meta_entries), eval_bars)
        meta_row = dict(meta_agg)
        meta_row.update(date=date, code="META", params_hash=phash)
        new_rows.append({k: meta_row.get(k) for k in cols})
    except Exception as e:  # noqa: BLE001 - META 失敗でも champion 行は残す（独立性を保つ）
        print(f"walkforward-jp META 計算失敗（champion 行は保存）: {e}")

    # force 再実行時は同一 (date, hash) を差し替え（二重計上を避ける）。
    if len(existing):
        existing = existing.reindex(columns=cols)
        keep = ~((existing["date"].astype(str) == date)
                 & (existing["params_hash"].astype(str) == phash))
        existing = existing[keep]
    merged = pd.concat([existing, pd.DataFrame(new_rows, columns=cols)], ignore_index=True)

    Path(reports_dir).mkdir(parents=True, exist_ok=True)
    merged.to_csv(csv_path, index=False)
    md_path.write_text(render_markdown_jp(merged, eval_bars), encoding="utf-8")

    return {"date": date, "params_hash": phash, "eval_bars": eval_bars, **agg}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="チャンピオンパラメータのウォークフォワード検証")
    ap.add_argument("--date", required=True, help="対象セッション日 YYYY-MM-DD")
    ap.add_argument("--force", action="store_true", help="冪等スキップを無視して上書き再実行")
    ap.add_argument("--jp", action="store_true",
                    help="日本株 bars1m バー近似版を回す（config/best-params-jp.yaml）")
    args = ap.parse_args(argv)

    if args.jp:
        res = run_walkforward_jp(args.date, force=args.force)
        if res is None:
            print(f"walkforward-jp: スキップ（対象なし/冪等/設定欠損）date={args.date}")
            return 0
        ov = res["overall"]
        first = res["eval_bars"][0]
        print(f"=== walkforward-jp {args.date} (hash={res['params_hash']}) ===")
        print(f"ALL n={ov['n']} win{first}={_fmt(ov[f'win{first}'], pct=True)} "
              f"exp{first}_net={_fmt(ov[f'exp{first}_net_bp'])}bp "
              f"med{first}_net={_fmt(ov[f'med{first}_net_bp'])}bp "
              f"gross_mean={_fmt(ov['gross_mean_bp'])}bp")
        for code, r in res["by_code"].items():
            print(f"  {code}: n={r['n']} win{first}={_fmt(r[f'win{first}'], pct=True)} "
                  f"exp{first}_net={_fmt(r[f'exp{first}_net_bp'])}bp")
        print(f"saved: {_reports_dir()}/walkforward-jp.csv, walkforward-jp.md")
        return 0

    res = run_walkforward(args.date, force=args.force)
    if res is None:
        print(f"walkforward: スキップ（対象なし/冪等/設定欠損）date={args.date}")
        return 0
    ov = res["overall"]
    print(f"=== walkforward {args.date} (hash={res['params_hash']}) ===")
    print(f"ALL n={ov['n']} win60={_fmt(ov['win60'], pct=True)} "
          f"exp60_net={_fmt(ov['exp60_net_bp'])}bp med60_net={_fmt(ov['med60_net_bp'])}bp "
          f"gross_mean={_fmt(ov['gross_mean_bp'])}bp")
    for code, r in res["by_code"].items():
        print(f"  {code}: n={r['n']} win60={_fmt(r['win60'], pct=True)} "
              f"exp60_net={_fmt(r['exp60_net_bp'])}bp")
    print(f"saved: {_reports_dir()}/walkforward.csv, walkforward.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
