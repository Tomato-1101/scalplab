"""RciImpulseStrategy のゲートロジック検証。

各テストはパラメータを注入し、確定足で SAR/ATR/RCI をウォームアップしてから
形成足のティックを流し、(a) 方向ゲート (b) 角度ゲート (c) 効率フィルタ (d) クールダウン
が期待どおり効くことを確認する。RCI の絶対値ではなくゲートの真偽を検証する。
"""
import numpy as np

from scalplab.strategy.rci_impulse import RciImpulseStrategy

RCI_P = 5
ATR_P = 3
SEC = 1_000_000_000  # 1 秒 = 1e9 ns

# 上昇ウォームアップ: 直近4本(=period-1)の close を [100.00..100.03] に密集させ、
# SAR をロング側かつ 99.99 より十分下に置く（後で assert で確認）。
UP_BARS = [
    (98.20, 97.80, 98.00),
    (98.70, 98.30, 98.50),
    (99.20, 98.80, 99.00),
    (99.70, 99.30, 99.50),
    (100.05, 99.95, 100.00),
    (100.06, 99.96, 100.01),
    (100.07, 99.97, 100.02),
    (100.08, 99.98, 100.03),
]

# 下降ウォームアップ: SAR をショート側かつ現在価格より上に置く。
DOWN_BARS = [
    (102.20, 101.80, 102.00),
    (101.70, 101.30, 101.50),
    (101.20, 100.80, 101.00),
    (100.70, 100.30, 100.50),
    (100.05, 99.95, 100.00),
    (100.04, 99.94, 99.99),
    (100.03, 99.93, 99.98),
    (100.02, 99.92, 99.97),
]


def _warm(strat, bars):
    for h, l, c in bars:
        strat.on_bar_confirm({"high": h, "low": l, "close": c})


def _drive(strat, ticks):
    sigs = []
    for t_ns, p in ticks:
        s = strat.on_tick(int(t_ns), float(p))
        if s is not None:
            sigs.append(s)
    return sigs


def _sweep_ticks(start_s, end_s, p0, p1, step_s=2):
    """[start_s, end_s] を step_s 刻みで p0→p1 に線形掃引するティック列。"""
    ts = list(range(start_s, end_s + 1, step_s))
    ps = np.linspace(p0, p1, len(ts))
    return [(t * SEC, float(p)) for t, p in zip(ts, ps)]


def _flat_ticks(start_s, end_s, price, step_s=2):
    return [(t * SEC, float(price)) for t in range(start_s, end_s + 1, step_s)]


def _base_params(**over):
    p = {
        "rci_period": RCI_P,
        "atr_period": ATR_P,
        "slope_window_sec": 30,
        "slope_threshold": 15.0,
        "eff_min": 3.0,
        "cooldown_sec": 60,
        "floor": 0.1,
    }
    p.update(over)
    return p


def test_no_long_when_price_below_sar():
    """(a) 価格が SAR より下（下降トレンド）では、上向き RCI でもロングは出ない。"""
    strat = RciImpulseStrategy(_base_params(), code="T")
    _warm(strat, DOWN_BARS)
    assert strat._sar_val > 100.10  # 現在価格より上にあることを確認
    # 形成価格を上向きに掃引（本来ならロング角度）。しかし価格<SAR なので short 領域、
    # short は負の傾きを要求するため何も出ない。
    ticks = _sweep_ticks(0, 40, 99.99, 100.10)
    sigs = _drive(strat, ticks)
    assert sigs == []


def test_no_signal_when_slope_below_threshold():
    """(b) ΔRCI が閾値未満なら出ない（価格ほぼ一定で RCI が動かない）。"""
    strat = RciImpulseStrategy(_base_params(slope_threshold=15.0), code="T")
    _warm(strat, UP_BARS)
    assert strat._sar_val < 99.99  # ロング可の位置
    ticks = _flat_ticks(0, 40, 100.02)  # RCI 変化ほぼゼロ
    sigs = _drive(strat, ticks)
    assert sigs == []


def test_efficiency_filter_rejects_large_price_small_rci():
    """(c) 「価格が大きく動くのに RCI 変化が小さい」場面を効率フィルタで棄却する。

    密集した確定足に対し形成価格を 100.02→108 と大きく掃引すると、RCI は途中で 100 に飽和し
    ΔRCI は頭打ちだが価格レンジ(norm_range)は巨大 → efficiency が小さくなる。
    eff_min を高くすると棄却、eff_min=0 だと（角度ゲートは通るので）発火することで、
    棄却要因が効率フィルタであることを示す。
    """
    ticks = _sweep_ticks(0, 40, 100.02, 108.0)

    reject = RciImpulseStrategy(_base_params(slope_threshold=5.0, eff_min=20.0), code="T")
    _warm(reject, UP_BARS)
    assert reject._sar_val < 100.02
    assert _drive(reject, ticks) == []

    accept = RciImpulseStrategy(_base_params(slope_threshold=5.0, eff_min=0.0), code="T")
    _warm(accept, UP_BARS)
    assert len(_drive(accept, ticks)) >= 1  # 効率フィルタを外せば発火する


# prev_diff テスト用 warmup（period 5）:
#  - 早期上昇で SAR を現在価格より下（ロング可）に置く。
#  - 直近確定足の RCI を moderate（≈0）にし、直近4本を単調増にして
#    形成価格が c4 の下→c7 の上へ掃引すると intrabar RCI が低→+100 へ立ち上がるようにする。
#    これで角度ゲート（W秒窓の ΔRCI）と直前差（intrabar−確定 RCI）を同時に大きくできる。
PREV_DIFF_BARS = [(c + 0.06, c - 0.06, c)
                  for c in [99.5, 99.8, 100.1, 100.65, 100.20, 100.35, 100.50, 100.60]]


def _prev_diff_sigs(prev_diff_min):
    """PREV_DIFF_BARS でウォームアップ後、100.15→100.95 を掃引して発火シグナルを返す。

    角度は slope_threshold=5 で緩め固定、効率フィルタは無効（eff_min=0）。よって発火可否の差は
    prev_diff_min（直前差ゲート）だけに起因する。"""
    strat = RciImpulseStrategy(
        _base_params(slope_threshold=5.0, eff_min=0.0, cooldown_sec=60,
                     prev_diff_min=prev_diff_min), code="T")
    _warm(strat, PREV_DIFF_BARS)
    assert strat._sar_val < 100.15  # 掃引開始価格より下＝ロング可
    return _drive(strat, _sweep_ticks(0, 60, 100.15, 100.95))


def test_prev_diff_min_gates_confirmed_jump():
    """(e) 直前差ゲート prev_diff: 直前確定足 RCI からのジャンプが閾値未満なら棄却する。

    掃引で intrabar RCI が確定 RCI(≈0) から +90 へ跳ねる。prev_diff_min=50 は通過、到達不能な
    130 は（角度ゲートは通るのに）棄却されることで、直前差が角度とは独立の AND 条件だと示す。"""
    fire = _prev_diff_sigs(50.0)
    assert len(fire) == 1
    assert fire[0]["direction"] == "long"
    assert fire[0]["prev_diff"] >= 50.0
    # 角度は通る（slope=5 << drci）のに、直前差が届かないと発火しない
    assert _prev_diff_sigs(130.0) == []


def test_prev_diff_min_zero_is_disabled():
    """prev_diff_min=0（既定）は無効で従来どおり発火する（後方互換）。"""
    assert len(_prev_diff_sigs(0.0)) == 1


def test_rci_sign_gate_blocks_long_in_plus_zone():
    """(f) RCI 符号ゲート: RCI がプラス圏なら（SAR がロング解除でも）ロングは出ない。

    UP_BARS で価格を上向き掃引すると intrabar RCI は +100 近傍（プラス圏）に張り付く。ゲート OFF では
    ロングが発火するが、ゲート ON（+ はショートのみ許可）では同じ場面が丸ごと抑制される。
    要件「RCI がプラスの時に下ヒゲが出てもロングしない」の直接検証。"""
    ticks = _sweep_ticks(0, 40, 99.99, 100.10)

    off = RciImpulseStrategy(_base_params(slope_threshold=5.0, eff_min=0.0), code="T")
    _warm(off, UP_BARS)
    assert off._sar_val < 99.99
    sig_off = _drive(off, ticks)
    assert len(sig_off) >= 1 and all(s["direction"] == "long" for s in sig_off)
    assert all(s["rci"] > 0.0 for s in sig_off)   # 発火はプラス圏で起きている

    on = RciImpulseStrategy(_base_params(slope_threshold=5.0, eff_min=0.0, rci_sign_gate=1), code="T")
    _warm(on, UP_BARS)
    assert _drive(on, ticks) == []                # プラス圏ロングはゲートで全抑制


def test_rci_sign_gate_zero_is_disabled():
    """rci_sign_gate=0（既定）は無効で従来どおり発火する（後方互換）。"""
    ticks = _sweep_ticks(0, 40, 99.99, 100.10)
    strat = RciImpulseStrategy(_base_params(slope_threshold=5.0, eff_min=0.0, rci_sign_gate=0), code="T")
    _warm(strat, UP_BARS)
    assert len(_drive(strat, ticks)) >= 1


def test_cooldown_suppresses_repeat_signals():
    """(d) 同一方向の連続シグナルを cooldown 秒抑制する。"""
    # 0..30s で掃引して発火条件を作り、31..70s で天井を維持 → 多数ティックが条件成立。
    ticks = _sweep_ticks(0, 30, 99.99, 100.10, step_s=1) + _flat_ticks(31, 70, 100.10, step_s=1)

    with_cd = RciImpulseStrategy(_base_params(cooldown_sec=60), code="T")
    _warm(with_cd, UP_BARS)
    sigs_cd = _drive(with_cd, ticks)
    assert len(sigs_cd) == 1  # クールダウンで 1 本に集約

    no_cd = RciImpulseStrategy(_base_params(cooldown_sec=0), code="T")
    _warm(no_cd, UP_BARS)
    sigs_nocd = _drive(no_cd, ticks)
    assert len(sigs_nocd) >= 2  # クールダウン無しなら複数発火

    # 出た方向はすべてロング
    assert all(s["direction"] == "long" for s in sigs_cd + sigs_nocd)
