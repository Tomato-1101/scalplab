"""夜間自動チューナー（日本株・bars1m バー近似版）。

目的（要件 2026-07-19）: 毎晩データ全体で「学習=最新日以外 / 検証=最新日」の分割探索を
自動実行し、チャンピオン（config/best-params-jp.yaml）を上回る挑戦者を継続追跡する。自動差し替えは
**しない**（人間/指揮が判断する材料を積むだけ）。

呼ばれ方:
  - 自動: record.py の JP セッション録画 finalize → yfinance 1分足保存 → walkforward-jp の直後
    （`_walkforward_jp_after_finalize` 内で walkforward→autotune の順、同じ非干渉 try/except）。
  - 手動/バックフィル: `python -m scalplab.backtest.autotune --date YYYY-MM-DD [--force]`。

分割: 引数 date（検証日＝最新日）より前の全 bars1m 日を学習、date を検証にする。主指標は
**検証日の全 JP 銘柄・全組の合計 ret_2bar_net**（bp）。前場/後場は screen.iter_jp_sessions で
独立計算（昼休み跨ぎを構造的に除外）。

グリッド（合計 312 組。全直積は大きすぎるので下記の合理的部分集合に絞る＝上限 500 内）:
  主グリッド（256 組）= 符号ゲート・fade 系すべて無効固定で、トリガー/フィルタの形を決める 7 軸の直積:
    spike_depth[0,30,50,80] × spike_turn[15,25] × dual_rci_slope[0,30] × eff[0,2] ×
    cooldown[10,30] × skip_open[0,30] × sar_filter[1,0] = 4·2·2·2·2·2·2 = 256。
  符号ゲート拡張（20 組）= 「基本形（チャンピオン相当）＋1軸だけ変えた 10 構成」に
    符号ゲート (gate,level)=(1,0) と (1,50) を重ねる（10×2）。符号ゲートは主グリッドと直交するため
    別建てにし、全直積の肥大を避けつつ「符号ゲート＋各軸」の効きを代表的に見る。
  σフェード拡張（18 組・2026-07-20）= 「基本形＋1軸だけ変えた 9 構成」に第3トリガー σフェード
    fade_sigma_k=3.0/4.0 を重ねる（9×2）。fade は既存トリガーと OR（トゲ＋fade の組）で、SAR 非依存の
    逆張り。◎選抜でトゲ順張りが負ける日にプラスの補完関係が実測されたため、同じ OOS 規律で挑戦させる。
    fade_sigma_k=0（無効）は主グリッドが担うので拡張は 3.0/4.0 のみ（σ=2.5 は実測で無エッジ）。
  ボリンジャーフェード拡張（18 組・2026-07-20）= 同じ 9 構成に第4トリガー fade_boll_z=2.5/3.0 を重ねる（9×2）。
    z=(close−SMA20)/std20 の逆張り。総当たり検証で σフェードより学習/検証の整合が良かった最良の逆張り手法。
    fade_boll_z=0（無効）は主グリッドが担うので拡張は 2.5/3.0 のみ。
  → チャンピオン（基本形・符号無効・fade 系無効）は主グリッドに含まれるので、その test_tot も毎回記録される。

冪等性: 同一 (date, params_hash) は再計算しない（--force で無視して該当日を作り直す）。
永続化:
  - reports/autotune-jp.csv … 1 行 = 1 組 × 1 検証日。
  - reports/autotune-jp.md  … CSV から再生成。3 セッション連続でチャンピオンの同日 test_tot を上回った
    組を「差し替え候補」として先頭に明記（自動差し替えはしない）。
"""
from __future__ import annotations

import argparse
import itertools
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from scalplab.backtest.engine import get_loader
from scalplab.backtest.metrics import valid_entries
from scalplab.backtest.screen import filter_jp_session, iter_jp_sessions
from scalplab.backtest.walkforward import _config_path_jp, _load_champion_jp, params_hash
from scalplab.strategy.rci_impulse_bar import (
    DEFAULT_BAR_PARAMS,
    bar_signals_from_arrays,
    merge_bar_params,
    precompute_bar_arrays,
)

# 主指標＝検証日の合計 +2 バー ネット。bar_signals には +1/+2 を出させる（champion と同じ eval_bars）。
MAIN_EVAL_BAR = 2
EVAL_BARS = (1, 2)
COST_BP = 3.0
CHALLENGER_STREAK = 3  # チャンピオンの同日 test_tot をこの本数だけ連続で上回ったら差し替え候補

# 基本形（チャンピオン相当。best-params-jp.yaml の固定値と一致させる）。グリッドはこの上に軸を上書きする。
_BASE = {
    "rci_period": 9, "spike_window_bars": 10, "slope_threshold": 0, "prev_diff_min": 0,
    "af_init": 0.02, "af_step": 0.02, "af_max": 0.2, "atr_period": 14, "floor": 0.1,
    "spike_depth_min": 80, "spike_turn_min": 25, "dual_rci_slope_min": 0,
    "rci_sign_gate": 0, "rci_sign_level": 0, "eff_min": 0, "cooldown_bars": 30,
    "skip_open_bars": 0, "sar_filter": 1,
}
# 主グリッドの 7 軸
_DEPTH = [0, 30, 50, 80]
_TURN = [15, 25]
_DUAL = [0, 30]
_EFF = [0, 2]
_CD = [10, 30]
_SKIP = [0, 30]
_SAR = [1, 0]
# 符号ゲート拡張の「1 軸変化」候補（基本形の値以外へ 1 つずつ振る）
_SIGN_VARIATIONS = [
    {}, {"spike_depth_min": 0}, {"spike_depth_min": 30}, {"spike_depth_min": 50},
    {"spike_turn_min": 15}, {"dual_rci_slope_min": 30}, {"eff_min": 2},
    {"cooldown_bars": 10}, {"skip_open_bars": 30}, {"sar_filter": 0},
]
_SIGN_SETTINGS = [(1, 0), (1, 50)]

# σフェード拡張の「代表組」（基本形＋1軸変化。既存の代表組に fade を掛ける＝要件 2026-07-20）。
# 既存トリガーとは OR なので、これらは spike/turn を残したまま fade を重ねた「トゲ＋fade」組になる
# （fade は SAR 非依存の逆張り。トゲ順張りが負ける日にプラスで補完する仮説を OOS で検証する）。
_FADE_VARIATIONS = [
    {}, {"spike_depth_min": 30}, {"spike_depth_min": 50}, {"spike_turn_min": 15},
    {"dual_rci_slope_min": 30}, {"eff_min": 2}, {"cooldown_bars": 10},
    {"skip_open_bars": 30}, {"sar_filter": 0},
]
# fade_sigma_k の掃引。0 は主グリッド（fade 無効）が担うので拡張は 3.0/4.0 のみ（σ=2.5 は実測で無エッジ）。
_FADE_K = [3.0, 4.0]
# ボリンジャーフェード fade_boll_z の掃引（追加 2026-07-20・総当たり検証の最良逆張り）。0 は主グリッドが担う。
# σフェードと同じ代表組（_FADE_VARIATIONS）に z=2.5/3.0 を重ねる（既存トリガーと OR・SAR 非依存）。
_FADE_BOLL_Z = [2.5, 3.0]

# ハッシュは型（int/float）差でぶれないよう全キーを float 正規化してから取る（DEFAULT の全キー）。
_HASH_KEYS = sorted(DEFAULT_BAR_PARAMS)

# 主要パラメータ列（CSV に残す軸）。
_PARAM_COLS = ["spike_depth_min", "spike_turn_min", "dual_rci_slope_min", "rci_sign_gate",
               "rci_sign_level", "eff_min", "cooldown_bars", "skip_open_bars", "sar_filter",
               "fade_sigma_k", "fade_boll_z"]
AT_COLUMNS = ["date", "params_hash"] + _PARAM_COLS + ["test_tot_bp", "test_n", "test_win", "train_tot_bp"]


def _combo_hash(full: dict) -> str:
    """組の安定ハッシュ（全 bar パラメータを float 正規化して sha1）。int/float 差でぶれない。"""
    return params_hash({k: float(full[k]) for k in _HASH_KEYS})


def build_grid() -> dict[str, dict]:
    """グリッド（hash -> 完全パラメータ dict）を組む。詳細な構成はモジュール docstring 参照。"""
    combos: dict[str, dict] = {}
    # 主グリッド（符号ゲート無効固定・7 軸直積 = 256）
    for depth, turn, dual, eff, cd, skip, sar in itertools.product(
            _DEPTH, _TURN, _DUAL, _EFF, _CD, _SKIP, _SAR):
        full = merge_bar_params({
            **_BASE, "spike_depth_min": depth, "spike_turn_min": turn, "dual_rci_slope_min": dual,
            "eff_min": eff, "cooldown_bars": cd, "skip_open_bars": skip, "sar_filter": sar,
            "rci_sign_gate": 0, "rci_sign_level": 0,
        })
        combos[_combo_hash(full)] = full
    # 符号ゲート拡張（基本形＋1軸変化 × (gate,level)=(1,0),(1,50) = 20）
    for var in _SIGN_VARIATIONS:
        for gate, level in _SIGN_SETTINGS:
            full = merge_bar_params({**_BASE, **var, "rci_sign_gate": gate, "rci_sign_level": level})
            combos[_combo_hash(full)] = full
    # σフェード拡張（基本形＋1軸変化 × fade_sigma_k=3.0,4.0 = 18。既存トリガーと OR）
    for var in _FADE_VARIATIONS:
        for k in _FADE_K:
            full = merge_bar_params({**_BASE, **var, "fade_sigma_k": k})
            combos[_combo_hash(full)] = full
    # ボリンジャーフェード拡張（基本形＋1軸変化 × fade_boll_z=2.5,3.0 = 18。既存トリガーと OR）
    for var in _FADE_VARIATIONS:
        for z in _FADE_BOLL_Z:
            full = merge_bar_params({**_BASE, **var, "fade_boll_z": z})
            combos[_combo_hash(full)] = full
    # BB 期間の掃引（2026-07-21 追加）: 期間×z の手動比較で 15/z3・25/z3 が 20/z3 を上回る日が
    # 多かったため、基本形（1軸変化なし）に窓 15/25 × z2.5/3.0 を重ねて OOS で決着させる。
    for w in (15, 25):
        for z in _FADE_BOLL_Z:
            full = merge_bar_params({**_BASE, "fade_boll_z": z, "fade_boll_window": w})
            combos[_combo_hash(full)] = full
    # BB 複雑化探索の生き残り（2026-07-21 の 42 バリアント学習/検証で判明）:
    # 確認条件系（ピン足・RCI 極値・反転確認・2本連続）は全て OOS 悪化＝複雑化は不採用。
    # 生き残りは「z を 3.25 に上げる」「クールダウンを縮めて暴落連鎖中の再エントリーを許す」の 2 軸のみ
    # （どちらも利益が 7/15-16 型の高ボラ日に集中する仮説。毎晩の OOS でその再現性を判定する）。
    for w in (15, 20, 25):
        full = merge_bar_params({**_BASE, "fade_boll_z": 3.25, "fade_boll_window": w})
        combos[_combo_hash(full)] = full
    for cd in (5, 10):
        for z in (3.0, 3.25):
            full = merge_bar_params({**_BASE, "fade_boll_z": z, "cooldown_bars": cd})
            combos[_combo_hash(full)] = full
    return combos


def _sum_net(arrs: list[dict], full: dict, code: str, eval_bar: int) -> tuple[float, int, int]:
    """1 銘柄の（事前計算済み指標の）半場群に組を適用し、(合計 net, 件数, 勝ち数) を返す（打切り除外）。"""
    col = f"ret_{eval_bar}bar_net"
    tot = 0.0
    n = 0
    wins = 0
    for arr in arrs:
        e = bar_signals_from_arrays(arr, full, code=code, cost_bp=COST_BP, eval_bars=EVAL_BARS)
        v = valid_entries(e)
        if len(v) and col in v.columns:
            s = v[col].dropna()
            if len(s):
                tot += float(s.sum())
                n += int(len(s))
                wins += int((s > 0).sum())
    return tot, n, wins


def _load_halves(loader, dates: list[str], code: str) -> list[dict]:
    """指定日群の bars1m を読み、前場/後場の半場ごとに指標を 1 回だけ計算した配列群を返す。

    指標（RCI/SAR/ATR・トゲ足内 low/high・長期 RCI）は rci_period=9 等が全組共通なので半場につき
    1 回計算し、全 276 組で使い回す（組ごとの再計算を避けて桁で速くする）。
    """
    arrs: list[dict] = []
    for d in dates:
        try:
            bars = loader.load_bars1m(d, code)
        except FileNotFoundError:
            continue
        for half in iter_jp_sessions(filter_jp_session(bars)):
            a = precompute_bar_arrays(half, _BASE)
            if a is not None:
                arrs.append(a)
    return arrs


def _available_dates(loader) -> list[str]:
    """bars1m 配下の日付ディレクトリ（昇順）。DI loader でもテストできるよう loader 経由にせず
    store.loader の DATA_DIR を使う（get_loader 相当のフォールバックは screen.recent_bars1m_dates と同じ）。"""
    from scalplab.backtest import screen as screen_mod
    return screen_mod.recent_bars1m_dates(10_000)  # 全日（十分大きな N）


def run_autotune(date: str | None = None, *, reports_dir: Path | None = None,
                 loader=None, dates: list[str] | None = None, force: bool = False) -> dict | None:
    """検証日 date（未指定なら最新の bars1m 日）でグリッド探索し reports/autotune-jp.* に積み上げる。

    dates を渡すと日付集合を明示できる（テスト用の DI）。次の場合 None（graceful skip）:
      - 対象 bars1m が 1 件も無い / 検証日に JP 銘柄が無い
      - 同一 (date, champion_hash) が既にあり force=False（冪等スキップ）
    """
    loader = loader or get_loader()
    reports_dir = Path(reports_dir) if reports_dir else _reports_dir()

    all_dates = sorted(dates) if dates is not None else _available_dates(loader)
    if not all_dates:
        return None
    test_date = date or all_dates[-1]
    if test_date not in all_dates:
        return None
    train_dates = [d for d in all_dates if d < test_date]

    # 対象銘柄＝検証日に bars1m がある JP 銘柄
    recorded = set(loader.list_recorded(test_date, kind="bars1m")) if hasattr(loader, "list_recorded") else set()
    codes = sorted(c for c in recorded if c.startswith("JP."))
    if not codes:
        return None

    combos = build_grid()
    champ = _load_champion_jp(_config_path_jp())
    champion_hash = _combo_hash(merge_bar_params(champ["params"])) if champ else None

    csv_path = reports_dir / "autotune-jp.csv"
    md_path = reports_dir / "autotune-jp.md"
    existing = pd.read_csv(csv_path) if csv_path.exists() else pd.DataFrame(columns=AT_COLUMNS)
    if not force and len(existing):
        done = existing["date"].astype(str) == test_date
        if champion_hash is not None:
            done &= existing["params_hash"].astype(str) == champion_hash
        if bool(done.any()):
            return None

    # 集計: 銘柄ごとに半場を 1 回だけ用意し、全組を適用して合算する。
    acc = {h: {"train_tot": 0.0, "test_tot": 0.0, "test_n": 0, "test_wins": 0} for h in combos}
    for code in codes:
        train_halves = _load_halves(loader, train_dates, code)
        test_halves = _load_halves(loader, [test_date], code)
        for h, full in combos.items():
            if train_halves:
                tr_tot, _, _ = _sum_net(train_halves, full, code, MAIN_EVAL_BAR)
                acc[h]["train_tot"] += tr_tot
            te_tot, te_n, te_w = _sum_net(test_halves, full, code, MAIN_EVAL_BAR)
            acc[h]["test_tot"] += te_tot
            acc[h]["test_n"] += te_n
            acc[h]["test_wins"] += te_w

    new_rows = []
    for h, full in combos.items():
        a = acc[h]
        win = (a["test_wins"] / a["test_n"]) if a["test_n"] else np.nan
        row = {"date": test_date, "params_hash": h,
               "test_tot_bp": a["test_tot"], "test_n": a["test_n"], "test_win": win,
               "train_tot_bp": a["train_tot"]}
        for k in _PARAM_COLS:
            row[k] = full[k]
        new_rows.append({k: row.get(k) for k in AT_COLUMNS})

    # 冪等な差し替え: 同一 (date, これらの hash) を落としてから追加（force は該当日を丸ごと作り直す）。
    if len(existing):
        existing = existing.reindex(columns=AT_COLUMNS)
        if force:
            existing = existing[existing["date"].astype(str) != test_date]
        else:
            new_hashes = set(combos)
            drop = (existing["date"].astype(str) == test_date) & existing["params_hash"].astype(str).isin(new_hashes)
            existing = existing[~drop]
    merged = pd.concat([existing, pd.DataFrame(new_rows, columns=AT_COLUMNS)], ignore_index=True)

    reports_dir.mkdir(parents=True, exist_ok=True)
    merged.to_csv(csv_path, index=False)
    md_path.write_text(render_markdown(merged, champion_hash), encoding="utf-8")

    challengers = find_challengers(merged, champion_hash)
    return {"date": test_date, "champion_hash": champion_hash, "n_combos": len(combos),
            "n_codes": len(codes), "challengers": challengers}


def find_challengers(df: pd.DataFrame, champion_hash: str | None) -> list[dict]:
    """チャンピオンの同日 test_tot を「最新から連続」で CHALLENGER_STREAK 本以上上回った組を返す。

    連続＝最新セッションを起点に遡り、途切れずに上回り続けた本数。streak>=閾値 の組だけ挙げる。
    """
    if champion_hash is None or not len(df):
        return []
    dates_desc = sorted(df["date"].astype(str).unique(), reverse=True)
    champ_sub = df[df["params_hash"].astype(str) == champion_hash]
    champ_tot = {str(d): float(v) for d, v in zip(champ_sub["date"].astype(str), champ_sub["test_tot_bp"])}
    out = []
    for h in df["params_hash"].astype(str).unique():
        if h == champion_hash:
            continue
        sub = df[df["params_hash"].astype(str) == h]
        tot = {str(d): float(v) for d, v in zip(sub["date"].astype(str), sub["test_tot_bp"])}
        streak = 0
        for d in dates_desc:
            if d in tot and d in champ_tot and tot[d] > champ_tot[d]:
                streak += 1
            else:
                break
        if streak >= CHALLENGER_STREAK:
            last = sub[sub["date"].astype(str) == dates_desc[0]].iloc[0]
            out.append({"params_hash": h, "streak": streak,
                        "params": {k: last[k] for k in _PARAM_COLS},
                        "test_tot_bp": float(last["test_tot_bp"])})
    out.sort(key=lambda r: r["streak"], reverse=True)
    return out


def _fmt(v, nd=2) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _param_brief(row) -> str:
    """1 組の主要パラメータを短い記法で表す（表示用）。"""
    return (f"深{_fmt(row['spike_depth_min'],0)}/戻{_fmt(row['spike_turn_min'],0)}/"
            f"同調{_fmt(row['dual_rci_slope_min'],0)}/符号{_fmt(row['rci_sign_gate'],0)}"
            f"±{_fmt(row['rci_sign_level'],0)}/効{_fmt(row['eff_min'],0)}/"
            f"CD{_fmt(row['cooldown_bars'],0)}/寄{_fmt(row['skip_open_bars'],0)}/SAR{_fmt(row['sar_filter'],0)}/"
            f"σフェード{_fmt(row['fade_sigma_k'],1)}/ボリンジャー{_fmt(row['fade_boll_z'],1)}")


def render_markdown(df: pd.DataFrame, champion_hash: str | None) -> str:
    """autotune-jp.md を再生成する（差し替え候補＋最新セッションの上位）。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 夜間自動チューナー（日本株・学習=最新日以外／検証=最新日）",
        "",
        f"最終更新: {ts}",
        "",
        "config/best-params-jp.yaml のチャンピオンを固定基準に、グリッド各組を毎晩の検証日（最新日）へ適用した"
        "**合計 +2 バー ネット（bp）**で追跡する。自動差し替えはしない（人間/指揮が判断）。",
        "",
    ]
    if not len(df):
        return "\n".join(lines + ["（まだ記録なし）", ""]) + "\n"

    dates_desc = sorted(df["date"].astype(str).unique(), reverse=True)
    challengers = find_challengers(df, champion_hash)
    lines += [f"## 差し替え候補（チャンピオンの同日 test を {CHALLENGER_STREAK} セッション連続で上回り）", ""]
    if challengers:
        lines += ["| hash | streak | 主要パラメータ | 最新 test_tot_bp |", "|---|---|---|---|"]
        for ch in challengers:
            lines.append(f"| {ch['params_hash']} | {ch['streak']} | {_param_brief(ch['params'])} | "
                         f"{_fmt(ch['test_tot_bp'])} |")
        lines.append("")
        lines.append("→ 差し替えは複数日 OOS 優位が根拠。best-params-jp.yaml の出自コメントを書き換えて更新すること。")
    else:
        lines.append(f"- 該当なし（{CHALLENGER_STREAK} セッション連続でチャンピオンを上回る組はまだ無い）。")
    lines.append("")

    # 最新セッションの上位（test_tot_bp 降順・上位 15）＋チャンピオン順位
    latest = dates_desc[0]
    sub = df[df["date"].astype(str) == latest].copy()
    sub = sub.sort_values("test_tot_bp", ascending=False, na_position="last").reset_index(drop=True)
    champ_rank = None
    if champion_hash is not None:
        m = sub.index[sub["params_hash"].astype(str) == champion_hash]
        champ_rank = (int(m[0]) + 1) if len(m) else None
    lines += [
        f"## 最新セッション {latest} の上位（全 {len(sub)} 組中）",
        "",
        f"- チャンピオン順位: {champ_rank if champ_rank else '-'} 位"
        + (f" / test_tot={_fmt(float(sub.loc[sub['params_hash'].astype(str)==champion_hash, 'test_tot_bp'].iloc[0]))}bp"
           if champ_rank else ""),
        "",
        "| # | hash | 主要パラメータ | test_tot_bp | test_n | test_win | train_tot_bp |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in sub.head(15).iterrows():
        mark = " ★champ" if (champion_hash is not None and str(r["params_hash"]) == champion_hash) else ""
        lines.append(f"| {i + 1} | {r['params_hash']}{mark} | {_param_brief(r)} | "
                     f"{_fmt(r['test_tot_bp'])} | {_fmt(r['test_n'],0)} | {_fmt(r['test_win'],3)} | "
                     f"{_fmt(r['train_tot_bp'])} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def _reports_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "reports"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="日本株の夜間自動チューナー（学習=最新日以外／検証=最新日）")
    ap.add_argument("--date", default=None, help="検証日 YYYY-MM-DD（未指定なら最新の bars1m 日）")
    ap.add_argument("--force", action="store_true", help="冪等スキップを無視して該当日を作り直す")
    args = ap.parse_args(argv)

    res = run_autotune(args.date, force=args.force)
    if res is None:
        print(f"autotune: スキップ（対象なし/冪等）date={args.date}")
        return 0
    print(f"=== autotune {res['date']} combos={res['n_combos']} codes={res['n_codes']} ===")
    if res["challengers"]:
        print(f"差し替え候補 {len(res['challengers'])} 組（{CHALLENGER_STREAK}連続超え）:")
        for ch in res["challengers"]:
            print(f"  {ch['params_hash']} streak={ch['streak']} test_tot={_fmt(ch['test_tot_bp'])}bp")
    else:
        print(f"差し替え候補なし（{CHALLENGER_STREAK}連続でチャンピオン超えの組は未出現）")
    print(f"saved: {_reports_dir()}/autotune-jp.csv, autotune-jp.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
