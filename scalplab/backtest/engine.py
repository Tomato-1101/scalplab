"""ティック駆動リプレイ・バックテストエンジン。

なぜティック駆動か: 本手法は「足内(intrabar)で刻々と動く RCI の角度」をトリガーにするため、
確定足だけでは検証できない。録画した全ティックを時刻順に流し、1分足を合成しながら
各ティックで intrabar RCI を再計算してシグナル判定する（リアルタイム検知と同一ロジック）。

loader は DI 可能。store.loader（別エージェント実装）が未提供でも、契約
(docs/data-contract.md)準拠の parquet を読む ParquetLoader へフォールバックして依存を切る。

評価はエントリー品質のみ（出口ルールは作らない）:
  各シグナルのエントリー価格に対する +15s/+30s/+60s/+90s/+120s の順方向リターン(bp,
  コスト前後)、および 120 秒内 MFE/MAE(bp) を、将来ティックから引いて算出する。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from scalplab.indicators.atr import atr_series
from scalplab.indicators.parabolic import sar_series
from scalplab.indicators.rci import intrabar_rci_batch, rci_series
from scalplab.strategy.rci_impulse import merge_params

_MIN_NS = 60_000_000_000  # 1 分 = 60e9 ns
DEFAULT_HORIZONS = (15, 30, 60, 90, 120)


def _project_root() -> Path:
    """絶対パス埋め込みを避けるため、このファイル位置からルートを相対解決する。"""
    return Path(__file__).resolve().parents[2]


class ParquetLoader:
    """契約準拠 parquet を読むフォールバック loader（store.loader 未提供時に使用）。"""

    def __init__(self, root=None):
        self.root = Path(root) if root else _project_root()

    def _path(self, kind, date, code):
        return self.root / "data" / kind / date / f"{code}.parquet"

    def load_ticks(self, date, code):
        p = self._path("ticks", date, code)
        if not p.exists():
            raise FileNotFoundError(p)
        return pd.read_parquet(p)

    def load_bars1m(self, date, code):
        p = self._path("bars1m", date, code)
        if not p.exists():
            raise FileNotFoundError(p)
        return pd.read_parquet(p)

    def load_quotes(self, date, code):
        p = self._path("quotes", date, code)
        if not p.exists():
            raise FileNotFoundError(p)
        return pd.read_parquet(p)

    def list_recorded(self, date, kind="ticks"):
        d = self.root / "data" / kind / date
        if not d.exists():
            return []
        return sorted(f.stem for f in d.glob("*.parquet"))


def get_loader():
    """store.loader があればそれを、無ければ ParquetLoader を返す。"""
    try:
        from scalplab.store import loader as store_loader  # 別エージェントが並行実装中
        if hasattr(store_loader, "load_ticks"):
            return store_loader
    except Exception:
        pass
    return ParquetLoader()


def entry_columns(horizons):
    """entries DataFrame の列順（シグナル0件でも同スキーマを返すため）。"""
    base = [
        "time", "code", "direction", "signal_price", "entry_price",
        "rci", "drci", "prev_diff", "efficiency", "sar", "trend", "atr", "atr_pct",
        "price_range_pct", "norm_range", "slope_elapsed_sec",
        "half_spread_bp", "cost_bp", "mfe_bp", "mae_bp", "eval_truncated",
    ]
    for h in horizons:
        base += [f"ret_{h}s_gross", f"ret_{h}s_net"]
    return base


def _empty_entries(horizons):
    return pd.DataFrame(columns=entry_columns(horizons))


def _estimate_tick_size(px: np.ndarray) -> float:
    """観測ティック価格の最小刻みを推定する（quotes 無し時のコスト単位）。"""
    u = np.unique(px)
    if len(u) < 2:
        base = float(px[0]) if len(px) else 100.0
        return max(base * 1e-4, 1e-6)
    d = np.diff(u)
    d = d[d > 0]
    return float(np.min(d)) if len(d) else max(float(u[0]) * 1e-4, 1e-6)


def _eval_signal(sig, t_ns, px, q_ns, q_bid, q_ask, tick_size, slippage_ticks, horizons):
    """1 シグナルをエントリー品質に換算する（全根拠数値付きの行を返す）。"""
    i = sig["idx"]
    st = sig["t_ns"]
    direction = sig["direction"]
    sign = 1.0 if direction == "long" else -1.0
    n = len(t_ns)
    last_ns = int(t_ns[-1])

    # エントリー価格・コスト
    if q_ns is not None:
        qi = int(np.searchsorted(q_ns, st, side="left"))
        if qi < len(q_ns):
            entry = float(q_ask[qi] if direction == "long" else q_bid[qi])
            half_spread = float((q_ask[qi] - q_bid[qi]) / 2.0)
        else:
            entry = float(px[i])
            half_spread = float(tick_size / 2.0)
        base_cost = half_spread  # スプレッドの半分（片側）
        half_spread_bp = half_spread / entry * 1e4
    else:
        ni = i + 1
        entry = float(px[ni]) if ni < n else float(px[i])
        base_cost = float(tick_size)  # quotes 無しは 1 tick とみなす
        half_spread_bp = float("nan")

    cost_price = base_cost + float(slippage_ticks) * tick_size
    cost_bp = cost_price / entry * 1e4

    row = {
        "time": pd.Timestamp(st),
        "code": sig["code"],
        "direction": direction,
        "signal_price": sig["price"],
        "entry_price": entry,
        "rci": sig["rci"],
        "drci": sig["drci"],
        "prev_diff": sig.get("prev_diff", float("nan")),
        "efficiency": sig["efficiency"],
        "sar": sig["sar"],
        "trend": sig["trend"],
        "atr": sig["atr"],
        "atr_pct": sig["atr_pct"],
        "price_range_pct": sig["price_range_pct"],
        "norm_range": sig["norm_range"],
        "slope_elapsed_sec": sig["slope_elapsed_sec"],
        "half_spread_bp": half_spread_bp,
        "cost_bp": cost_bp,
    }

    # 評価窓がセッション終端で切れるシグナルは除外フラグ
    row["eval_truncated"] = bool(st + int(horizons[-1] * 1e9) > last_ns)

    for hsec in horizons:
        ht = st + int(hsec * 1e9)
        fi = int(np.searchsorted(t_ns, ht, side="left"))
        if fi < n:
            fp = float(px[fi])
            ret = sign * (fp - entry) / entry * 1e4
            row[f"ret_{hsec}s_gross"] = ret
            row[f"ret_{hsec}s_net"] = ret - cost_bp
        else:
            row[f"ret_{hsec}s_gross"] = float("nan")
            row[f"ret_{hsec}s_net"] = float("nan")

    # 120 秒内 MFE/MAE（エントリー価格基準・方向符号込み, bp）
    end = st + int(120 * 1e9)
    a = int(np.searchsorted(t_ns, st, side="right"))
    b = int(np.searchsorted(t_ns, end, side="right"))
    if b > a:
        seg = px[a:b]
        exc = sign * (seg - entry) / entry * 1e4
        row["mfe_bp"] = float(np.max(exc))
        row["mae_bp"] = float(np.min(exc))
    else:
        row["mfe_bp"] = float("nan")
        row["mae_bp"] = float("nan")

    return row


def compute_tick_features(code, t_ns, px, params):
    """全ティック分の「閾値非依存」な特徴量を numpy でまとめて算出する。

    なぜ分離するか: 旧実装はティック毎に IntrabarRci 再計算とリングバッファ線形走査を回す
    Python ループが支配的コストだった（プロファイルで全体の大半）。intrabar RCI・SAR/ATR 方向・
    W秒前基準 ΔRCI・prev_diff・効率フィルタ用の窓レンジは、slope_threshold / eff_min /
    prev_diff_min / cooldown といった「閾値」には依存しない。よってここで一度だけ計算しておけば、
    パラメータスキャンで閾値だけ違う組を安価に回せる（scan の共有前計算）。

    逐次器(on_bar_confirm→on_tick)と数値が厳密一致するよう、確定足の確定タイミング
    （次の分の最初のティックで直前分の足を確定）と ref/窓の境界を保存している。
    """
    n = len(t_ns)
    period = int(params["rci_period"])
    atr_period = int(params["atr_period"])
    w_ns = int(float(params["slope_window_sec"]) * 1e9)
    floor = float(params["floor"])

    # --- 分足グループ（ティックが属する distinct minute。t_ns は昇順前提） ---
    minute = t_ns // _MIN_NS
    change = np.empty(n, dtype=bool)
    change[0] = True
    change[1:] = minute[1:] != minute[:-1]
    group_start = np.flatnonzero(change)
    K = len(group_start)
    k_of_tick = np.cumsum(change) - 1  # 各ティックの distinct-minute インデックス
    group_end = np.empty(K, dtype=np.int64)
    group_end[:-1] = group_start[1:]
    group_end[-1] = n
    closes = px[group_end - 1]                       # 各分の close（最終ティック価格）
    highs = np.maximum.reduceat(px, group_start)     # 各分の high
    lows = np.minimum.reduceat(px, group_start)      # 各分の low

    # --- 確定足指標（bar 0..K-1）。group k のティックは bar 0..k-1 まで確定済み → 1本シフト ---
    sar_arr, trend_arr = sar_series(highs, lows, closes,
                                    params["af_init"], params["af_step"], params["af_max"])
    atr_arr = atr_series(highs, lows, closes, atr_period)
    crci_arr = rci_series(closes, period)
    sar_g = np.full(K, np.nan); sar_g[1:] = sar_arr[:-1]
    trend_g = np.zeros(K, dtype=np.int64); trend_g[1:] = trend_arr[:-1]
    atr_g = np.full(K, np.nan); atr_g[1:] = atr_arr[:-1]
    crci_g = np.full(K, np.nan); crci_g[1:] = crci_arr[:-1]
    sar_val = sar_g[k_of_tick]
    trend_t = trend_g[k_of_tick]
    atr_val = atr_g[k_of_tick]
    last_conf_rci = crci_g[k_of_tick]

    # --- intrabar RCI（ティック毎）。base=直前 period-1 本の確定 close を固定し価格を挿入 ---
    rci = np.full(n, np.nan)
    for k in range(period - 1, K):
        s = group_start[k]
        e = group_end[k]
        base = closes[k - period + 1:k]
        rci[s:e] = intrabar_rci_batch(base, px[s:e], period)

    # --- W秒前の基準 RCI（t<=now−W を満たす最新の finite サンプル、かつ t>=now−2W） ---
    finite_rci = np.isfinite(rci)
    fi = np.flatnonzero(finite_rci)
    ref_rci = np.full(n, np.nan)
    ref_t = np.zeros(n, dtype=np.int64)
    ref_ok = np.zeros(n, dtype=bool)
    if len(fi):
        ft = t_ns[fi]
        frci = rci[fi]
        target = t_ns - w_ns
        pos = np.searchsorted(ft, target, side="right") - 1
        ok = pos >= 0
        posc = np.clip(pos, 0, len(ft) - 1)
        cand_t = ft[posc]
        cand_r = frci[posc]
        ok &= cand_t >= (t_ns - 2 * w_ns)  # 旧リングバッファの 2W 保持窓を再現
        ref_ok = ok
        ref_t = np.where(ok, cand_t, 0)
        ref_rci = np.where(ok, cand_r, np.nan)
    drci = rci - ref_rci
    elapsed = (t_ns - ref_t) / 1e9
    prev_diff = rci - last_conf_rci

    # --- 方向の絶対条件（価格 vs SAR）と有効性 ---
    finite_all = np.isfinite(rci) & np.isfinite(atr_val) & np.isfinite(sar_val)
    atr_pos = atr_val > 0.0
    long_m = finite_all & atr_pos & (px > sar_val)
    short_m = finite_all & atr_pos & (px < sar_val)
    base_ok = (long_m | short_m) & ref_ok

    # --- 効率フィルタ用の窓 hi/lo（[t−W, t]）。pandas 時間窓 rolling は
    #     px[searchsorted(t, t−W, 'left') : i+1] の max/min と厳密一致（検証済み） ---
    ser = pd.Series(px, index=pd.to_datetime(t_ns))
    win = pd.Timedelta(w_ns, unit="ns")
    hi = ser.rolling(win, closed="both").max().to_numpy()
    lo = ser.rolling(win, closed="both").min().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        price_range_pct = (hi - lo) / px
        atr_pct = atr_val / px
        norm_range = price_range_pct / atr_pct
        efficiency = np.abs(drci) / np.maximum(norm_range, floor)

    # --- トゲ反転特徴量（spike_depth_min/spike_turn_min のいずれか>0 のときだけ計算） ---
    # 逐次 on_tick と同一窓 [t−W, t]（tt>=t−W ⇔ searchsorted 'left'）の finite RCI 列から V字/逆V字の
    # 落差・戻りを求める。spike_reversal_features（逐次器）と厳密一致するよう「トラフ(窓内最小)より前の
    # 最大＝先行高値」を argmin/argmax の最初の出現で採る。閾値非依存なのでここで一度だけ算出し、
    # signals_from_features で閾値比較する（無効時は None＝走らせない＝spike=0 の重い経路を汚さない）。
    spike_depth_min = float(params.get("spike_depth_min", 0.0))
    spike_turn_min = float(params.get("spike_turn_min", 0.0))
    spike_depth_long = spike_turn_long = spike_depth_short = spike_turn_short = None
    if spike_depth_min > 0.0 or spike_turn_min > 0.0:
        spike_depth_long = np.full(n, -np.inf)
        spike_turn_long = np.full(n, -np.inf)
        spike_depth_short = np.full(n, -np.inf)
        spike_turn_short = np.full(n, -np.inf)
        lo_arr = np.searchsorted(t_ns, t_ns - w_ns, side="left")
        for i in range(n):
            if not finite_rci[i]:
                continue  # rci[i] が NaN のティックは base_ok で落ちるので不要
            lo_i = int(lo_arr[i])
            seg = rci[lo_i:i + 1]
            w = seg[np.isfinite(seg)]
            if w.size == 0:
                continue
            tp = int(np.argmin(w)); trough = float(w[tp])
            pre_high = float(w[:tp].max()) if tp > 0 else -np.inf
            pp = int(np.argmax(w)); peak = float(w[pp])
            pre_low = float(w[:pp].min()) if pp > 0 else np.inf
            ri = float(rci[i])
            spike_depth_long[i] = pre_high - trough
            spike_turn_long[i] = ri - trough
            spike_depth_short[i] = peak - pre_low
            spike_turn_short[i] = peak - ri

    return {
        "code": code, "t_ns": t_ns, "px": px,
        "rci": rci, "drci": drci, "prev_diff": prev_diff, "efficiency": efficiency,
        "sar_val": sar_val, "trend_t": trend_t, "atr_val": atr_val, "atr_pct": atr_pct,
        "price_range_pct": price_range_pct, "norm_range": norm_range, "elapsed": elapsed,
        "long_m": long_m, "short_m": short_m, "base_ok": base_ok,
        "spike_depth_long": spike_depth_long, "spike_turn_long": spike_turn_long,
        "spike_depth_short": spike_depth_short, "spike_turn_short": spike_turn_short,
    }


def signals_from_features(feat, params):
    """特徴量に「閾値ゲート＋クールダウン」を適用して signals リストを返す。

    slope_threshold / prev_diff_min / rci_sign_gate / eff_min / cooldown_sec のみを参照する
    （残りは特徴量側）。ゲートは全て AND、クールダウンだけが逐次（発火した同方向シグナルのみで抑制）。
    旧 on_tick と同一の発火集合になるよう、ゲート通過後の候補を時系列順にクールダウン処理する。
    """
    t_ns = feat["t_ns"]; px = feat["px"]
    rci = feat["rci"]
    drci = feat["drci"]; prev_diff = feat["prev_diff"]; efficiency = feat["efficiency"]
    long_m = feat["long_m"]; short_m = feat["short_m"]; base_ok = feat["base_ok"]
    code = feat["code"]

    slope_th = float(params["slope_threshold"])
    prev_diff_min = float(params.get("prev_diff_min", 0.0))
    rci_sign_gate = int(params.get("rci_sign_gate", 0))
    spike_depth_min = float(params.get("spike_depth_min", 0.0))
    spike_turn_min = float(params.get("spike_turn_min", 0.0))
    eff_min = float(params["eff_min"])
    cooldown_ns = int(float(params["cooldown_sec"]) * 1e9)

    if slope_th > 0.0:
        slope_pass = np.where(long_m, drci >= slope_th,
                              np.where(short_m, drci <= -slope_th, False))
    else:
        slope_pass = long_m | short_m
    if prev_diff_min > 0.0:
        finite_pd = np.isfinite(prev_diff)
        pd_pass = np.where(long_m, finite_pd & (prev_diff >= prev_diff_min),
                           np.where(short_m, finite_pd & (prev_diff <= -prev_diff_min), False))
    else:
        pd_pass = long_m | short_m
    eff_pass = efficiency >= eff_min
    # RCI 符号ゲート（逐次 on_tick と同一集合）: + はショートのみ・− はロングのみ許可。0/NaN は両不許可。
    # long_m/short_m は方向を内包するので、符号一致する側だけ通す。base_ok が既に finite(rci) を保証する。
    if rci_sign_gate != 0:
        sign_pass = (short_m & (rci > 0.0)) | (long_m & (rci < 0.0))
    else:
        sign_pass = long_m | short_m
    # トゲ反転ゲート（逐次 on_tick と同一集合）。方向に一致する V字/逆V字の落差・戻りが閾値以上のときだけ通す。
    # 各サブ条件は閾値<=0 で無効（0=無効の規約）。depth/turn とも 0 のときはゲート自体を掛けない。
    if spike_depth_min > 0.0 or spike_turn_min > 0.0:
        dl = feat["spike_depth_long"]; tl = feat["spike_turn_long"]
        ds = feat["spike_depth_short"]; ts = feat["spike_turn_short"]
        long_spike = np.ones(len(long_m), dtype=bool)
        short_spike = np.ones(len(long_m), dtype=bool)
        if spike_depth_min > 0.0:
            long_spike &= dl >= spike_depth_min
            short_spike &= ds >= spike_depth_min
        if spike_turn_min > 0.0:
            long_spike &= tl >= spike_turn_min
            short_spike &= ts >= spike_turn_min
        spike_pass = (long_m & long_spike) | (short_m & short_spike)
    else:
        spike_pass = long_m | short_m

    cand_idx = np.flatnonzero(base_ok & slope_pass & pd_pass & eff_pass & sign_pass & spike_pass)

    sar_val = feat["sar_val"]; trend_t = feat["trend_t"]
    atr_val = feat["atr_val"]; atr_pct = feat["atr_pct"]
    price_range_pct = feat["price_range_pct"]; norm_range = feat["norm_range"]
    elapsed = feat["elapsed"]

    last_sig = {"long": None, "short": None}
    signals = []
    for i in cand_idx:
        i = int(i)
        direction = "long" if long_m[i] else "short"
        ti = int(t_ns[i])
        last = last_sig[direction]
        if last is not None and (ti - last) < cooldown_ns:
            continue
        last_sig[direction] = ti
        signals.append({
            "t_ns": ti, "code": code, "direction": direction, "price": float(px[i]),
            "rci": float(rci[i]), "drci": float(drci[i]), "prev_diff": float(prev_diff[i]),
            "efficiency": float(efficiency[i]), "sar": float(sar_val[i]),
            "trend": int(trend_t[i]), "atr": float(atr_val[i]), "atr_pct": float(atr_pct[i]),
            "price_range_pct": float(price_range_pct[i]), "norm_range": float(norm_range[i]),
            "slope_elapsed_sec": float(elapsed[i]), "idx": i,
        })
    return signals


def _replay_one(code, ticks, quotes, params, horizons, slippage_ticks):
    """1 銘柄・1 日のティックをリプレイして entries を返す。"""
    ticks = ticks.sort_values("time").reset_index(drop=True)
    t_ns = ticks["time"].to_numpy().astype("int64")
    px = ticks["price"].to_numpy().astype(np.float64)
    n = len(ticks)
    if n == 0:
        return _empty_entries(horizons)

    tick_size = _estimate_tick_size(px)
    if quotes is not None and len(quotes):
        q = quotes.sort_values("time")
        q_ns = q["time"].to_numpy().astype("int64")
        q_bid = q["bid"].to_numpy().astype(np.float64)
        q_ask = q["ask"].to_numpy().astype(np.float64)
    else:
        q_ns = q_bid = q_ask = None

    feat = compute_tick_features(code, t_ns, px, params)
    signals = signals_from_features(feat, params)

    if not signals:
        return _empty_entries(horizons)

    rows = [
        _eval_signal(s, t_ns, px, q_ns, q_bid, q_ask, tick_size, slippage_ticks, horizons)
        for s in signals
    ]
    return pd.DataFrame(rows, columns=entry_columns(horizons))


def run_backtest(date, codes, params=None, loader=None, horizons=DEFAULT_HORIZONS, slippage_ticks=0):
    """指定日・銘柄群を対象にバックテストを実行し entries DataFrame を返す。

    quotes は無ければ None 扱い（次ティック価格でエントリー・コストは 1 tick）。
    """
    p = merge_params(params)
    slip = p.get("slippage_ticks", slippage_ticks)
    loader = loader or get_loader()
    frames = []
    for code in codes:
        try:
            ticks = loader.load_ticks(date, code)
        except FileNotFoundError:
            continue
        try:
            quotes = loader.load_quotes(date, code)
        except (FileNotFoundError, AttributeError):
            quotes = None
        frames.append(_replay_one(code, ticks, quotes, p, tuple(horizons), slip))

    if frames:
        return pd.concat(frames, ignore_index=True)
    return _empty_entries(tuple(horizons))
