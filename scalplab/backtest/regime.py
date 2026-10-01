"""レジーム判定（今日は「順張りの日」か「逆張りの日」か）。

なぜ必要か（実測・2026-07-20）:
  日本株バー近似では、順張り系（トゲ反転チャンピオン）と逆張り系（ボリンジャーフェード）が
  相互補完の関係にある（トゲが大負けした急変日に逆張りがプラス）。15 営業日・完全先読みなしの
  検証で「直前3営業日の◎銘柄について両系の合計 net を比べ、勝っている方を当日に使う」メタ選択が
  どちらの単独運用も上回った（サンプルが小さく、探索に使った期間でもあるため参考値）。この
  「どちらの日か」を毎朝ひと目で分かるようにするのが本モジュール（WebUI ヘッダのレジームバッジ）。

判定の定義（完全先読みなし）:
  - 基準時点（date=None なら最新保存日、指定日ならその日）に対する「直前3営業日」を
    data/bars1m の日付ディレクトリから取る（date=None は最新3日、date 指定はその日より前の3日）。
  - その3日で ◎銘柄（3日レンジ中央値 >= 25bp = screen.APTITUDE_RANGE_BP の◎閾値）を選ぶ。
  - ◎銘柄群にチャンピオン（トゲ反転・順張り・+2本保有）とボリンジャーフェード（逆張り・z>=3.0・
    +1本保有）をそれぞれ適用し、有効エントリーの net 合計を比べる。勝っている方が当日のレジーム。
  - 計算は screen/walkforward と同じ機構（filter_jp_session → iter_jp_sessions で前場/後場を独立
    計算し昼休み跨ぎを構造的に除外・bar_signals・valid_entries）を再利用する。

キャッシュ:
  同一日付集合なら data/.cache/regime-<最新日>.json を再利用する（毎回数秒→即時）。キャッシュ内の
  dates が現在の直前3日と一致し、かつ その3日の bars1m の版（ファイル数/合計サイズ/最終更新）が
  一致した時だけ使う（finalize 後の追加・置換で自動失効させる）。ずれていれば再計算して上書きする。
  force=True で無視。

順張り（trend）チャンピオンのパラメータは config/best-params-jp.yaml を単一の真実として読む
（yaml を書き換えれば trend 系の定義も追従する）。逆張り（revert）系は本モジュールの定数で持つ。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

from scalplab.backtest.metrics import valid_entries
from scalplab.backtest.screen import (
    APTITUDE_RANGE_BP,
    aptitude_metrics,
    filter_jp_session,
    iter_jp_sessions,
)
from scalplab.store import loader as store_loader
from scalplab.strategy.rci_impulse_bar import (
    bar_entry_columns,
    bar_signals,
    merge_bar_params,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

PRIOR_DAYS = 3                       # 直前3営業日で判定
OMEGA_RANGE_BP = APTITUDE_RANGE_BP[1]  # ◎の下限（レンジ中央値 >= 25bp）
TREND_HOLD_BAR = 2                   # 順張り（チャンピオン）の評価保有＝+2本（15日検証の 2本保有）
REVERT_HOLD_BAR = 1                  # 逆張り（ボリンジャーフェード）の評価保有＝+1本（hold1）
DEFAULT_COST_BP = 3.0               # 片道概算コスト
CACHE_SUBDIR = ".cache"
_EVAL_BARS = (1, 2)                  # entries に +1/+2 バー net を必ず持たせる（両保有を比較可能に）

# 逆張り（ボリンジャーフェード）系のパラメータ。fade_boll_z のみ有効にした純逆張り（SAR 非依存）。
# 重要: slope_threshold は **9999（実質∞）** にして角度トリガーを黙らせる。0 だと角度ゲートが素通りし
# （spike/dual も無効なので）全バーで "angle" エントリーが出て逆張り実験が汚染される（test_strategy_bar の
# _boll_params と同じ規約）。実測最良の z=3.0・window20・30本CD。
REVERT_PARAMS = {
    "rci_period": 9,
    "slope_threshold": 9999.0,
    "prev_diff_min": 0.0,
    "rci_sign_gate": 0,
    "rci_sign_level": 0.0,
    "dual_rci_slope_min": 0.0,
    "spike_depth_min": 0.0,
    "spike_turn_min": 0.0,
    "eff_min": 0.0,
    "fade_sigma_k": 0.0,
    "fade_boll_z": 3.0,
    "fade_boll_window": 20,
    "cooldown_bars": 30,
    "af_init": 0.02,
    "af_step": 0.02,
    "af_max": 0.2,
    "atr_period": 14,
    "floor": 0.1,
}

# best-params-jp.yaml が読めない時の順張りフォールバック（現行チャンピオン=トゲ単独モード）。
_FALLBACK_TREND_PARAMS = {
    "rci_period": 9,
    "slope_threshold": 0.0,
    "prev_diff_min": 0.0,
    "rci_sign_gate": 0,
    "spike_depth_min": 80.0,
    "spike_turn_min": 25.0,
    "spike_window_bars": 10,
    "eff_min": 0.0,
    "cooldown_bars": 30,
    "af_init": 0.02,
    "af_step": 0.02,
    "af_max": 0.2,
    "atr_period": 14,
    "floor": 0.1,
}


def _bars1m_root(loader) -> Optional[Path]:
    """loader から bars1m ルートを解決する（本番=store_loader モジュール、テスト=MiniLoader）。"""
    if hasattr(loader, "DATA_DIR"):
        return Path(loader.DATA_DIR) / "bars1m"
    if hasattr(loader, "root"):
        return Path(loader.root) / "data" / "bars1m"
    return None


def available_dates(loader=None) -> list[str]:
    """data/bars1m 配下で JP 銘柄のバーを持つ日付を昇順で返す（無ければ空）。

    JP.* を1件以上含む日だけを営業日として数える。米国セッション由来の bars1m
    （NY 日付ディレクトリに US.* だけが入る。例: 海の日 2026-07-20）が混ざると、
    直前3日窓が中身の無い日で埋まりレジーム判定の根拠が実質2日以下に痩せるため。
    """
    loader = loader or store_loader
    root = _bars1m_root(loader)
    if root is None or not root.exists():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and any(p.glob("JP.*.parquet")))


def prior_dates(all_dates: list[str], date: Optional[str] = None) -> list[str]:
    """基準日 date に対する直前3営業日（昇順）を返す。

    date=None は「最新時点」とみなし利用可能な最新3日を、date 指定はその日より前の3日を返す。
    """
    cand = [d for d in all_dates if (date is None or d < date)]
    return cand[-PRIOR_DAYS:]


def _champion_config() -> tuple[dict, float]:
    """順張りチャンピオン（config/best-params-jp.yaml）の params と cost_bp を返す。"""
    path = PROJECT_ROOT / "config" / "best-params-jp.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        params = dict(raw.get("params") or {})
        if not params:
            raise ValueError("params 空")
        cost_bp = float(raw.get("cost_bp", DEFAULT_COST_BP))
        return params, cost_bp
    except Exception:  # noqa: BLE001 - yaml 欠損/破損時は現行チャンピオン相当のフォールバックで継続
        return dict(_FALLBACK_TREND_PARAMS), DEFAULT_COST_BP


def _empty_entries() -> pd.DataFrame:
    return pd.DataFrame(columns=bar_entry_columns(_EVAL_BARS))


def select_omega_symbols(dates: list[str], loader=None) -> list[str]:
    """dates 期間の bars1m 全 JP 銘柄のうち、レンジ中央値 >= 25bp（◎）の code を昇順で返す。

    レンジ中央値は screen.aptitude_metrics（(high-low)/close の中央値 bp）を dates 連結バーに適用して測る。
    """
    loader = loader or store_loader
    if not dates:
        return []
    codes: set[str] = set()
    for d in dates:
        codes.update(c for c in loader.list_recorded(d, kind="bars1m") if c.startswith("JP."))
    omega: list[str] = []
    for c in sorted(codes):
        parts = []
        for d in dates:
            try:
                parts.append(loader.load_bars1m(d, c))
            except FileNotFoundError:
                continue
        if not parts:
            continue
        bars = pd.concat(parts, ignore_index=True)
        rng = aptitude_metrics(bars)["range_med_bp"]
        if np.isfinite(rng) and rng >= OMEGA_RANGE_BP:
            omega.append(c)
    return omega


def _system_entries(codes: list[str], dates: list[str], params: dict,
                    cost_bp: float, loader=None) -> pd.DataFrame:
    """codes × dates に system（params）を適用した entries を連結して返す（前場/後場独立計算）。"""
    loader = loader or store_loader
    p = merge_bar_params(params)
    frames = []
    for c in codes:
        for d in dates:
            try:
                bars = loader.load_bars1m(d, c)
            except FileNotFoundError:
                continue
            sess = filter_jp_session(bars)
            for day in iter_jp_sessions(sess):
                e = bar_signals(day, p, code=c, cost_bp=cost_bp, eval_bars=_EVAL_BARS)
                if len(e):
                    frames.append(e)
    return pd.concat(frames, ignore_index=True) if frames else _empty_entries()


def _sum_net(entries: pd.DataFrame, hold_bar: int) -> float:
    """有効エントリー（打切り除外）の ret_{hold}bar_net 合計を返す。"""
    valid = valid_entries(entries)
    col = f"ret_{hold_bar}bar_net"
    net = valid[col].dropna() if col in valid.columns else pd.Series(dtype=float)
    return float(net.sum()) if len(net) else 0.0


def _decide(omega: list[str], dates: list[str], loader) -> dict:
    """◎銘柄群への両系適用から pick と両系 net 合計を決める（キャッシュ非依存の純計算）。"""
    champ_params, champ_cost = _champion_config()
    trend_entries = _system_entries(omega, dates, champ_params, champ_cost, loader)
    revert_entries = _system_entries(omega, dates, REVERT_PARAMS, DEFAULT_COST_BP, loader)
    trend_bp = _sum_net(trend_entries, TREND_HOLD_BAR)
    revert_bp = _sum_net(revert_entries, REVERT_HOLD_BAR)
    # 同点はチャンピオン（順張り）を既定に採る。
    pick = "revert" if revert_bp > trend_bp else "trend"
    return {"pick": pick, "trend_bp": round(trend_bp, 1),
            "revert_bp": round(revert_bp, 1), "dates": list(dates)}


def _cache_path(loader, key: str) -> Optional[Path]:
    root = _bars1m_root(loader)
    if root is None:
        return None
    return root.parent / CACHE_SUBDIR / f"regime-{key}.json"


def _bars1m_stamp(loader, dates: list[str]) -> list:
    """判定に使った bars1m の「版」（日付ごとの ファイル数 / 合計サイズ / 最終更新）を返す。

    日付集合だけを鍵にすると、同じ日の bars1m が finalize 後に追加・置換されても（yfinance →
    J-Quants 由来の差し替え等）再計算されず、UI バッジや META 行が古いデータの判定のまま固定される。
    JSON 往復でタプルがリストになるので比較の型を揃えて list で返す。
    """
    root = _bars1m_root(loader)
    out: list = []
    for d in dates:
        day = (root / d) if root is not None else None
        if day is None or not day.exists():
            out.append([d, 0, 0, 0])
            continue
        stats = [f.stat() for f in day.glob("*.parquet")]
        out.append([d, len(stats), sum(s.st_size for s in stats),
                    max((s.st_mtime_ns for s in stats), default=0)])
    return out


def _read_cache(loader, key: str, dates: list[str], stamp: list) -> Optional[dict]:
    """キャッシュが存在し dates と bars1m の版が一致すれば返す（judge_regime の戻り値と同形）。"""
    path = _cache_path(loader, key)
    if path is None or not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if obj.get("dates") != list(dates) or obj.get("stamp") != stamp:
        return None
    return {k: v for k, v in obj.items() if k != "stamp"}  # stamp は内部用（API 応答に混ぜない）


def _write_cache(loader, key: str, result: dict) -> None:
    path = _cache_path(loader, key)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError:  # キャッシュ書けなくても判定結果は返す
        pass


def judge_regime(date: Optional[str] = None, *, force: bool = False, loader=None) -> dict:
    """レジーム判定を返す。

    戻り値: {"pick": "trend"|"revert", "trend_bp": .., "revert_bp": .., "dates": [...], "n_symbols": ..}。
    pick=trend は「順張りの日」、revert は「逆張りの日」。dates は判定に使った直前3営業日。
    """
    loader = loader or store_loader
    all_dates = available_dates(loader)
    pri = prior_dates(all_dates, date)
    key = pri[-1] if pri else "none"

    stamp = _bars1m_stamp(loader, pri)

    if not force:
        cached = _read_cache(loader, key, pri, stamp)
        if cached is not None:
            return cached

    omega = select_omega_symbols(pri, loader)
    result = _decide(omega, pri, loader)
    result["n_symbols"] = len(omega)
    _write_cache(loader, key, {**result, "stamp": stamp})
    return result


def meta_entries_for_date(date: str, *, loader=None,
                          cost_bp: Optional[float] = None) -> tuple[pd.DataFrame, str]:
    """トレーディング日 date に「直前3日で決めたレジームの系」を「直前3日◎銘柄」へ当日適用した entries を返す。

    walkforward-jp の META 行（レジーム選択に従った当日成績）用。完全先読みなし＝銘柄選抜も系選択も
    直前3日だけで決め、その系を当日 date（1日）の◎銘柄へ適用する。戻り値 (entries, pick)。
    prior/omega が無ければ空 entries と pick を返す。
    """
    loader = loader or store_loader
    all_dates = available_dates(loader)
    pri = prior_dates(all_dates, date)
    omega = select_omega_symbols(pri, loader)
    if not omega:
        return _empty_entries(), "trend"

    decision = _decide(omega, pri, loader)
    pick = decision["pick"]
    if pick == "trend":
        params, cost = _champion_config()
    else:
        params, cost = REVERT_PARAMS, DEFAULT_COST_BP
    if cost_bp is not None:
        cost = cost_bp
    entries = _system_entries(omega, [date], params, cost, loader)
    return entries, pick
