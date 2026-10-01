"""ベクトル版バックテスト vs 逐次版のパリティ回帰テスト。

engine の高速化（compute_tick_features + signals_from_features による全ティック
ベクトル化）が、確定足→intrabar を 1 ティックずつ回す逐次器 RciImpulseStrategy と
**シグナル列で完全一致**することを恒久的に保証する。プロジェクト要件「高速化は数値完全一致が
絶対条件・ハーネスはリポジトリにコミットして回帰化」に対応する。

合成ティックだけで完結し実録画データに依存しない（seed 固定・数千件・数秒で完了）。
カバレッジ: SAR 反転（区分トレンドの符号切替）・タイ値（0.01 丸めで同値頻出）・
疎密の切替（分ごとのティック数を疎/中/密で変える）・重複タイムスタンプ・閾値ぎわ
（slope_threshold に実際に出現する drci を採用）。
"""
import numpy as np
import pytest

from scalplab.backtest.engine import (
    _MIN_NS,
    compute_tick_features,
    signals_from_features,
)
from scalplab.indicators.rci import IntrabarRci, intrabar_rci_batch
from scalplab.strategy.rci_impulse import RciImpulseStrategy, merge_params

SEC = 1_000_000_000
# 比較するシグナルフィールド（時刻・方向・根拠数値）。t_ns は time と同義。
_FIELDS = ("t_ns", "direction", "idx", "rci", "drci", "prev_diff", "efficiency")


def _synth_ticks(seed: int):
    """SAR 反転・タイ値・疎密切替・重複タイムスタンプを含む合成ティックを返す。

    価格は区分トレンド（4 分ごとに傾きを切替）＋ノイズのランダムウォークを 0.01 刻みに
    丸めて生成する（丸めで同値タイが多発し、順位のタイ処理を突く）。分ごとにティック数を
    疎(4)/中(25-50)/密(80-120)で切替え、秒はランダム重複ありに置くことで、intrabar RCI の
    グループ境界・W秒窓 hi/lo・W秒前 ref の各境界を広く踏む。戻り値 (t_ns[int64 昇順], price)。
    """
    rng = np.random.default_rng(seed)
    seg = [+0.06, +0.06, +0.05, -0.07, -0.06, +0.08, +0.05, -0.09, +0.04, -0.05]
    t_list: list[int] = []
    p_list: list[float] = []
    price = 100.0
    n_minutes = 64
    for m in range(n_minutes):
        slope = seg[(m // 4) % len(seg)]  # 4 分ごとにトレンド転換 → SAR 反転を誘発
        if m % 6 == 0:
            k = 4                                  # 疎
        elif m % 3 == 2:
            k = int(rng.integers(80, 120))         # 密
        else:
            k = int(rng.integers(25, 50))          # 中
        ms = m * 60 * SEC
        secs = np.sort(rng.integers(0, 60, size=k))  # 分内の経過秒（重複あり）
        for s in secs:
            price += slope / max(k, 1) + rng.normal(0, 0.02)
            p_list.append(round(price, 2))           # 0.01 刻み → タイ値頻出
            t_list.append(int(ms + int(s) * SEC))    # 同秒は重複タイムスタンプ
    t = np.array(t_list, dtype="int64")
    p = np.array(p_list, dtype=np.float64)
    order = np.argsort(t, kind="mergesort")          # 安定ソートで挿入順を保持
    return t[order], p[order]


def _orig_signals(code, t_ns, px, params):
    """逐次器で 1 ティックずつ流し、旧 engine._replay_one と同一のシグナル列を得る。

    分足の確定タイミング（次の分の最初のティックで直前分を確定）と形成足更新の順序を
    旧実装どおりに再現する（これがパリティの基準）。
    """
    strat = RciImpulseStrategy(params, code=code)
    signals = []
    cur_min = None
    o = h = l = c = None
    for i in range(len(t_ns)):
        ti = int(t_ns[i])
        pi = float(px[i])
        m = ti - (ti % _MIN_NS)
        if cur_min is None:
            cur_min = m
            o = h = l = c = pi
        elif m != cur_min:
            strat.on_bar_confirm({"open": o, "high": h, "low": l, "close": c})
            cur_min = m
            o = h = l = c = pi
        if pi > h:
            h = pi
        if pi < l:
            l = pi
        c = pi
        sig = strat.on_tick(ti, pi)
        if sig is not None:
            sig["idx"] = i
            signals.append(sig)
    return signals


def _vec_signals(code, t_ns, px, params):
    return signals_from_features(compute_tick_features(code, t_ns, px, params), params)


def _assert_signal_parity(t_ns, px, over):
    """1 パラメータ組について逐次==ベクトルを厳密照合し、両者のシグナル数を返す。"""
    p = merge_params(over)
    so = _orig_signals("T", t_ns, px, p)
    sv = _vec_signals("T", t_ns, px, p)
    assert len(so) == len(sv), (
        f"シグナル数不一致 over={over}: 逐次={len(so)} ベクトル={len(sv)}"
    )
    for a, b in zip(so, sv):
        for k in _FIELDS:
            va, vb = a[k], b[k]
            if k == "direction":
                assert va == vb, f"direction 不一致 idx={a['idx']}: {va} vs {vb}"
            elif k in ("t_ns", "idx"):
                assert int(va) == int(vb), f"{k} 不一致: {va} vs {vb}"
            else:
                if np.isnan(va) and np.isnan(vb):
                    continue
                assert abs(va - vb) < 1e-9, (
                    f"{k} 不一致 idx={a['idx']} over={over}: {va} vs {vb} (|Δ|={abs(va-vb):.2e})"
                )
    return len(sv)


def _edge_threshold(t_ns, px, base):
    """slope ゲート無効で特徴量を作り、ロング候補 drci の中央値を返す。

    これを slope_threshold に採用すると「drci がしきい値ちょうど」の境界比較を踏む。
    逐次とベクトルで drci がビット一致していないと採否が食い違うため、閾値ぎわの回帰になる。
    """
    p = merge_params({**base, "slope_threshold": 0.0, "eff_min": 0.0, "prev_diff_min": 0.0})
    f = compute_tick_features("T", t_ns, px, p)
    drci = f["drci"][f["long_m"] & f["base_ok"]]
    drci = drci[np.isfinite(drci) & (drci > 0)]
    return float(np.median(drci)) if len(drci) else 10.0


@pytest.mark.parametrize("seed", [0, 7])
def test_vectorized_signals_match_sequential(seed):
    """代表的な複数パラメータ組で、ベクトル版のシグナル列が逐次版と完全一致する。"""
    t_ns, px = _synth_ticks(seed)
    assert len(t_ns) >= 3000, "数千件の合成ティックを想定"
    assert (len(t_ns) - len(np.unique(t_ns))) > 0, "重複タイムスタンプを含むこと"

    base = dict(rci_period=7, slope_window_sec=20, atr_period=14, floor=0.1)
    edge = _edge_threshold(t_ns, px, base)
    param_sets = [
        # 角度＋効率の通常運用
        dict(base, slope_threshold=10.0, eff_min=1.0, prev_diff_min=0.0, cooldown_sec=30),
        # 直前差ゲート有効・効率無効
        dict(base, slope_threshold=8.0, eff_min=0.0, prev_diff_min=15.0, cooldown_sec=30),
        # 別の RCI 期間・窓
        dict(rci_period=9, slope_window_sec=30, atr_period=14, floor=0.1,
             slope_threshold=12.0, eff_min=2.0, prev_diff_min=0.0, cooldown_sec=45),
        # 全ゲート無効（最多発火。クールダウンと全根拠フィールドを広く突く）
        dict(base, slope_threshold=0.0, eff_min=0.0, prev_diff_min=0.0, cooldown_sec=20),
        # 閾値ぎわ（slope_threshold = 実際に出現する drci）
        dict(base, slope_threshold=edge, eff_min=0.0, prev_diff_min=0.0, cooldown_sec=30),
        # RCI 符号ゲート有効（角度も有効＝トゲの反転側だけ発火）。ゲート込みの発火集合が逐次と一致すること
        dict(base, slope_threshold=8.0, eff_min=0.0, prev_diff_min=0.0,
             rci_sign_gate=1, cooldown_sec=30),
        # 符号ゲート＋角度＋直前差＋効率を全部盛り（全 AND 経路 × ゲートのパリティ）
        dict(base, slope_threshold=10.0, eff_min=1.0, prev_diff_min=10.0,
             rci_sign_gate=1, cooldown_sec=30),
        # トゲ反転ゲート単独（角度/直前差 OFF）。V字/逆V字の反転点集合が逐次==ベクトルで一致すること
        dict(base, slope_threshold=0.0, eff_min=0.0, prev_diff_min=0.0,
             spike_depth_min=50.0, spike_turn_min=15.0, cooldown_sec=10),
        # トゲ反転＋RCI符号＋効率の併用（spike 特徴量 × 他ゲートの AND パリティ）
        dict(base, slope_threshold=0.0, eff_min=1.0, prev_diff_min=0.0,
             spike_depth_min=30.0, spike_turn_min=10.0, rci_sign_gate=1, cooldown_sec=10),
    ]
    for over in param_sets:
        _assert_signal_parity(t_ns, px, over)


@pytest.mark.parametrize("seed", [0, 7])
def test_rci_sign_gate_composite_is_spike_reversal(seed):
    """RCI 符号ゲート ON（+角度 ON）で発火が「トゲの反転側」に限定されることを保証する。

    合成挙動の要件（2026-07-18）:
      - ショートは全て RCI>0（プラス圏）かつ ΔRCI<0（下折れ）＝上向きトゲの根元。
      - ロングは全て RCI<0（マイナス圏）かつ ΔRCI>0（上折れ）＝下向きトゲの根元。
    対照として、同一データ・同一角度でゲート OFF だと上の不変条件を破る発火（プラス圏ロング /
    マイナス圏ショート）が実在することを確認し、ゲートが自明に空でなく実際に効いていると担保する。
    ゲート ON でも逐次==ベクトルの発火集合一致（パリティ）を併せて固定する。
    """
    t_ns, px = _synth_ticks(seed)
    base = dict(rci_period=7, slope_window_sec=20, atr_period=14, floor=0.1,
                slope_threshold=8.0, eff_min=0.0, prev_diff_min=0.0, cooldown_sec=20)

    # ゲート ON でも発火集合が逐次と厳密一致すること
    _assert_signal_parity(t_ns, px, {**base, "rci_sign_gate": 1})

    sig_on = _vec_signals("T", t_ns, px, merge_params({**base, "rci_sign_gate": 1}))
    sig_off = _vec_signals("T", t_ns, px, merge_params({**base, "rci_sign_gate": 0}))

    assert len(sig_on) >= 1, "ゲート ON で 1 件も出ない（テストデータが不適）"
    for s in sig_on:
        if s["direction"] == "short":
            assert s["rci"] > 0.0 and s["drci"] < 0.0, f"ショートが上向きトゲの根元でない: {s}"
        else:
            assert s["rci"] < 0.0 and s["drci"] > 0.0, f"ロングが下向きトゲの根元でない: {s}"
    # ブロックだけでなく「符号一致側は通す」ことも担保（両方向が実際に発火する）
    assert {s["direction"] for s in sig_on} == {"long", "short"}, "片方向しか出ていない"

    # ゲート OFF には不変条件違反（プラス圏ロング / マイナス圏ショート）が実在する＝ゲートは非自明
    violations = [s for s in sig_off
                  if (s["direction"] == "long" and s["rci"] > 0.0)
                  or (s["direction"] == "short" and s["rci"] < 0.0)]
    assert violations, "ゲート OFF でも違反が無い＝ゲートの効果を検証できていない"


@pytest.mark.parametrize("seed", [0, 7])
def test_spike_gate_fires_and_gates(seed):
    """トゲ反転ゲート: 角度/直前差 OFF で V字/逆V字の反転点が発火し、depth ゲートと角度併用の矛盾が効く。

    - トゲ ON・角度 OFF・直前差 OFF で反転点が 1 件以上発火する（要件の最低保証）。
    - 到達不能な depth（RCI レンジ ±100 → 落差の上限 200）では 1 件も出ない（depth ゲートが実効）。
    - 角度 ON を併用すると激減する: V 底の切り返し瞬間は窓端点間 ΔRCI がまだ大きく負で角度条件
      （ロング⟺ΔRCI>0）と両立しにくい＝現行の角度条件がトゲを取れない本質的理由の回帰固定。
    ゲート ON でも逐次==ベクトルの発火集合一致（パリティ）を併せて固定する。
    """
    t_ns, px = _synth_ticks(seed)
    base = dict(rci_period=7, slope_window_sec=20, atr_period=14, floor=0.1,
                prev_diff_min=0.0, eff_min=0.0, cooldown_sec=10)
    fire = len(_vec_signals("T", t_ns, px, merge_params(
        {**base, "slope_threshold": 0.0, "spike_depth_min": 30.0, "spike_turn_min": 10.0})))
    with_angle = len(_vec_signals("T", t_ns, px, merge_params(
        {**base, "slope_threshold": 8.0, "spike_depth_min": 30.0, "spike_turn_min": 10.0})))
    impossible = len(_vec_signals("T", t_ns, px, merge_params(
        {**base, "slope_threshold": 0.0, "spike_depth_min": 300.0, "spike_turn_min": 10.0})))
    assert fire >= 1
    assert impossible == 0
    assert with_angle < fire
    _assert_signal_parity(t_ns, px, {**base, "slope_threshold": 0.0,
                                     "spike_depth_min": 30.0, "spike_turn_min": 10.0})


@pytest.mark.parametrize("seed", [0, 7])
def test_both_directions_fire_and_nontrivial(seed):
    """SAR 反転を踏むため、ゲート無効時にロング・ショートの双方が発火する（回帰の実効性担保）。"""
    t_ns, px = _synth_ticks(seed)
    p = merge_params(dict(rci_period=7, slope_window_sec=20, atr_period=14, floor=0.1,
                          slope_threshold=0.0, eff_min=0.0, prev_diff_min=0.0, cooldown_sec=20))
    sv = _vec_signals("T", t_ns, px, p)
    dirs = {s["direction"] for s in sv}
    assert len(sv) >= 20, f"発火が少なすぎる: {len(sv)}"
    assert dirs == {"long", "short"}, f"両方向が出ていない: {dirs}"


def test_intrabar_rci_batch_matches_sequential_with_ties():
    """intrabar_rci_batch（閉形式ベクトル）が IntrabarRci 逐次と一致（タイ値を含む）。"""
    rng = np.random.default_rng(3)
    period = 7
    base = np.round(rng.normal(100.0, 0.5, size=period - 1), 2)
    base[1] = base[0]   # 確定足内のタイ
    base[3] = base[2]
    prices = np.round(rng.normal(100.0, 0.5, size=64), 2)
    prices[0] = base[-1]  # price == 確定足 close のタイ
    prices[1] = base[0]
    got = intrabar_rci_batch(base, prices, period)
    for j, pr in enumerate(prices):
        r = IntrabarRci(period)
        for c in base:
            r.confirm_bar(float(c))
        exp = r.update_tick(float(pr))
        assert abs(got[j] - exp) < 1e-9, f"j={j} price={pr}: batch={got[j]} seq={exp}"
