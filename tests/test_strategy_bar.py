"""バー近似シグナル（rci_impulse_bar.bar_signals）のゲート検証。

確定 1 分足のみで動くバー版について、(a) 方向ゲート (b) 角度閾値 (c) 効率フィルタ
(d) クールダウン、および (e) シグナルバー終値エントリー・+n バー評価の算術が正しいことを
合成バーで確認する。指標の絶対値ではなくゲートの真偽とエントリー/評価の一致を見る。
"""
import numpy as np
import pandas as pd

from scalplab.strategy.rci_impulse_bar import (
    SPIKE_MAX_LAG,
    _spike_entry,
    bar_attention,
    bar_signals,
)

RCI_P = 5
ATR_P = 3
START = "2099-01-05 09:35:00"

# 上昇ウォームアップ→もみ合い（RCI が下がる）→急伸（drci1 が大きく + に振れる）。
# t=10 で drci1=+60、t=13 で drci1=+40（proto で実測）。SAR は価格より十分下。
LONG_CLOSES = [98.0, 98.4, 98.8, 99.2, 99.6, 100.0,
               100.02, 99.98, 100.03, 99.99, 100.04, 100.00, 100.30,
               100.42, 100.55, 100.60, 100.7]

# 下降で SAR を価格より上に置き、t=10 で drci1=-60 のショート角度を作る。
SHORT_CLOSES = [102.0, 101.6, 101.2, 100.8, 100.4, 100.0,
                99.98, 100.02, 99.97, 100.01, 99.96, 100.00, 99.70,
                99.58, 99.45, 99.40, 99.3]


def _bars(closes, start=START, wide_at=None, wide_high=None):
    """close 列から bars1m 契約スキーマの合成バーを作る（open=前バー close, 高安は close±0.03）。"""
    t0 = pd.Timestamp(start)
    n = len(closes)
    ohlc = []
    for i, cl in enumerate(closes):
        op = closes[i - 1] if i > 0 else cl - 0.02
        hi = max(op, cl) + 0.03
        lo = min(op, cl) - 0.03
        if wide_at is not None and i == wide_at:  # 効率フィルタ用に高安レンジを広げる
            hi = wide_high
        ohlc.append((op, hi, lo, cl))
    o, h, l, c = zip(*ohlc)
    return pd.DataFrame({
        "time": [t0 + pd.Timedelta(minutes=i) for i in range(n)],
        "open": np.array(o, float), "high": np.array(h, float),
        "low": np.array(l, float), "close": np.array(c, float),
        "volume": np.full(n, 100, np.int64), "turnover": np.array(c, float) * 100.0,
        "source": "moomoo_hist",
    })


def _params(**over):
    p = {"rci_period": RCI_P, "atr_period": ATR_P, "slope_threshold": 15.0,
         "eff_min": 1.0, "cooldown_bars": 10, "floor": 0.1}
    p.update(over)
    return p


def test_long_fires_with_correct_entry_and_eval():
    """(e) ロングが1本発火し、エントリー=シグナルバー終値 close[t]・+n バー評価・net=gross-cost が正しい。

    2026-07-19 早期化でエントリー基準を「翌バー始値 open[t+1]」→「シグナルバー終値 close[t]」に変更。
    評価は close[t+k] − close[t]。entry_price == signal_close(=close[t]) を明示的に固定し、旧定義に
    戻したら落ちるようにする（合成バーは open[t+1]==close[t] なので、旧定義でも通ってしまわないよう
    signal_close との一致で新定義をピン留めする）。"""
    bars = _bars(LONG_CLOSES)
    cost_bp = 3.0
    e = bar_signals(bars, _params(), code="T", cost_bp=cost_bp, eval_bars=(1, 2, 3))
    assert len(e) == 1
    row = e.iloc[0]
    assert row["direction"] == "long"
    assert row["sar"] < row["signal_close"]  # 価格より下＝ロング可
    assert not bool(row["eval_truncated"])

    # シグナルバー index を時刻から逆算し、エントリー＝当該バー終値・評価＝close[t+k]-close[t] を再計算して一致確認
    t0 = pd.Timestamp(START)
    t = int(round((row["time"] - t0).total_seconds() / 60))
    c = bars["close"].to_numpy()
    assert row["entry_price"] == c[t]              # 新定義: エントリー=シグナルバー終値
    assert row["entry_price"] == row["signal_close"]  # signal_close も close[t] なので一致（新定義の固定）
    entry = c[t]
    for k in (1, 2, 3):
        gross = (c[t + k] - entry) / entry * 1e4
        assert row[f"ret_{k}bar_gross"] == np.float64(gross) or abs(row[f"ret_{k}bar_gross"] - gross) < 1e-9
        assert abs(row[f"ret_{k}bar_net"] - (gross - cost_bp)) < 1e-9


def test_no_signal_when_slope_below_threshold():
    """(b) 角度閾値を drci1 の最大より高くすると発火しない。"""
    e = bar_signals(_bars(LONG_CLOSES), _params(slope_threshold=100.0), code="T")
    assert len(e) == 0


def test_prev_diff_min_gates_1bar_jump():
    """(f) 直前差ゲート prev_diff（=drci1）: 1 本前の確定 RCI からのジャンプが閾値未満なら棄却。

    バー版では prev_diff = drci1。発火バー t=10 の drci1=+60（proto 実測）。prev_diff_min を
    50 に上げると通過、70 に上げると（角度は緩いままでも）棄却されることで、直前差が
    角度 slope_threshold とは独立の AND ゲートであることを示す。"""
    bars = _bars(LONG_CLOSES)
    ok = bar_signals(bars, _params(prev_diff_min=50.0), code="T")
    assert len(ok) == 1
    assert abs(ok.iloc[0]["prev_diff"] - 60.0) < 1e-9   # 明示化された「尖り」値
    assert ok.iloc[0]["prev_diff"] == ok.iloc[0]["drci1"]
    ng = bar_signals(bars, _params(prev_diff_min=70.0), code="T")
    assert len(ng) == 0


def test_prev_diff_min_zero_is_disabled():
    """prev_diff_min=0（既定）は無効で、従来どおり slope_threshold だけで発火する（後方互換）。"""
    bars = _bars(LONG_CLOSES)
    base = bar_signals(bars, _params(), code="T")               # prev_diff_min 未指定＝0
    same = bar_signals(bars, _params(prev_diff_min=0.0), code="T")
    assert len(base) == len(same) == 1


def test_direction_gate_short_only_below_sar():
    """(a) 価格が SAR より下ではショートのみ（下向き RCI でショートが1本）。"""
    bars = _bars(SHORT_CLOSES)
    e = bar_signals(bars, _params(), code="T", cost_bp=3.0)
    assert len(e) == 1
    row = e.iloc[0]
    assert row["direction"] == "short"
    assert row["sar"] > row["signal_close"]  # 価格より上＝ショート可
    assert row["drci1"] <= -15.0  # 下向き角度


def test_efficiency_filter_rejects_wide_range_bar():
    """(c) 同じ角度でも高安レンジが広い（効率が低い）バーは eff_min で棄却される。"""
    # シグナルバー t=10 の高値を広げて norm_range を増やし efficiency を下げる（proto: eff≈22）。
    bars = _bars(LONG_CLOSES, wide_at=10, wide_high=103.0)
    t0 = pd.Timestamp(START)
    sig_t10 = t0 + pd.Timedelta(minutes=10)

    accept = bar_signals(bars, _params(eff_min=1.0), code="T")
    assert sig_t10 in set(accept["time"])  # 効率が緩ければ t=10 で発火

    reject = bar_signals(bars, _params(eff_min=30.0), code="T")
    assert sig_t10 not in set(reject["time"])  # 効率が厳しいと t=10 は棄却される


def test_cooldown_suppresses_repeat_signals():
    """(d) 同一方向の連続シグナルを cooldown_bars 本抑制する。"""
    bars = _bars(LONG_CLOSES)
    few = bar_signals(bars, _params(cooldown_bars=10), code="T")
    many = bar_signals(bars, _params(cooldown_bars=3), code="T")
    assert len(few) == 1
    assert len(many) == 2  # cooldown を短くすると t=10, t=13 の2本
    assert all(s == "long" for s in list(few["direction"]) + list(many["direction"]))


def test_rci_sign_gate_blocks_wrong_side():
    """(g) RCI 符号ゲート: RCI がプラス圏のロング／マイナス圏のショートは抑制される。

    LONG_CLOSES は RCI>0（プラス圏）でロング、SHORT_CLOSES は RCI<0（マイナス圏）でショートが発火する。
    どちらもゲート ON（+ はショートのみ・− はロングのみ）では「符号と逆側」なので全抑制される。
    要件「RCI がプラスの時はロング不可・マイナスの時はショート不可」の直接検証。"""
    for closes in (LONG_CLOSES, SHORT_CLOSES):
        off = bar_signals(_bars(closes), _params(cooldown_bars=3), code="T")
        assert len(off) >= 1                                              # ゲート無しなら発火する
        on = bar_signals(_bars(closes), _params(cooldown_bars=3, rci_sign_gate=1), code="T")
        assert len(on) == 0                                              # 符号と逆側なので全抑制


def test_rci_sign_gate_zero_is_disabled():
    """rci_sign_gate=0（既定）は無効で従来どおり発火する（後方互換）。"""
    assert len(bar_signals(_bars(LONG_CLOSES), _params(rci_sign_gate=0), code="T")) == 1


# トゲ反転（V字）用の合成: 強い上昇で SAR を遠く下へ→価格~100 の微小・単調な下げで RCI が +100→−100 へ
# 垂直に落ち V 字で切り返す。価格変動が小さいので SAR は下(long)を維持し、V 底からの戻りでロングが出る。
SPIKE_V_CLOSES = [90.0, 92.0, 94.0, 96.0, 98.0, 100.0, 100.5,
                  100.40, 100.30, 100.20, 100.10, 100.00,   # 微小・単調な下げ → RCI 急落（トラフ）
                  100.15, 100.30, 100.50, 100.70]           # 微小回復 → RCI 反転上げ


def test_spike_gate_fires_at_v_bottom():
    """(h) トゲ反転ゲート: 深い V字 RCI の切り返し点でロングが発火する（角度・符号 OFF で単独検証）。

    SPIKE_V_CLOSES は RCI が +100→−100→+100 の V を描き、SAR は下（long）を維持する。トラフ(−100)から
    戻ったバーで depth=先行高値−トラフ≈200・turn=当該RCI−トラフ が閾値以上になりロングが発火する。
    到達不能な depth（RCI レンジ ±100 で落差の上限 200）を超える 250 では発火しない＝ゲートが実効。"""
    bars = _bars(SPIKE_V_CLOSES)
    common = dict(slope_threshold=0, eff_min=0, cooldown_bars=1, prev_diff_min=0, spike_window_bars=8)
    fire = bar_signals(bars, _params(spike_depth_min=150, spike_turn_min=100, **common), code="T")
    assert len(fire) >= 1
    assert all(d == "long" for d in fire["direction"])
    none = bar_signals(bars, _params(spike_depth_min=250, spike_turn_min=100, **common), code="T")
    assert len(none) == 0


def test_spike_gate_zero_is_disabled():
    """spike_*_min=0（既定）はトゲゲートを掛けない（従来挙動）。トゲ ON で発火が絞られることで対照確認。"""
    bars = _bars(SPIKE_V_CLOSES)
    base = dict(slope_threshold=0, eff_min=0, cooldown_bars=1, prev_diff_min=0)
    off = bar_signals(bars, _params(spike_depth_min=0, spike_turn_min=0, **base), code="T")
    on = bar_signals(bars, _params(spike_depth_min=150, spike_turn_min=100, spike_window_bars=8, **base), code="T")
    assert len(off) > len(on) >= 1


def test_spike_intrabar_low_fires_on_v_bottom_bar():
    """(i) 早期化(2026-07-19): 足内 low 差し替え RCI により「V 底を付けたバーそのもの」で発火する。

    close は単調上昇（close-only では RCI 一定＝トラフが出ずトゲ発火しない）だが、1 本だけ足内で
    深い安値を付けて終値は戻す「ピン足」を作ると、その足の rci_at_low がトラフになり depth/turn を
    満たしてそのバーで発火する。同じ close でも安値が浅い（＝足内 dip 無し）と発火しないことを対照
    確認し、早期検出が足内近似に由来することを固定する。"""
    START2 = "2099-01-05 09:35:00"
    closes = [90, 92, 94, 96, 98, 100, 100.2, 100.3, 100.35, 100.4, 100.42, 100.5, 100.7]
    PIN = 8

    def _pin_bars(pin_low):
        t0 = pd.Timestamp(START2)
        n = len(closes)
        rows = []
        for i, cl in enumerate(closes):
            op = closes[i - 1] if i > 0 else cl - 0.02
            hi = max(op, cl) + 0.03
            lo = min(op, cl) - 0.03
            if pin_low is not None and i == PIN:
                lo = pin_low  # ピン足＝足内で深い安値
            rows.append((op, hi, lo, cl))
        o, h, l, c = zip(*rows)
        return pd.DataFrame({
            "time": [t0 + pd.Timedelta(minutes=i) for i in range(n)],
            "open": np.array(o, float), "high": np.array(h, float),
            "low": np.array(l, float), "close": np.array(c, float),
            "volume": np.full(n, 100, np.int64), "turnover": np.array(c, float) * 100.0,
            "source": "moomoo_hist",
        })

    # spike_early=0 で「標準終値パスの足内 low 検出」だけを isolate する（早期化＝寄り値/ピン足即断の
    # 追加発火を除いて、この test 本来の意図＝足内 low がトラフになりその足で発火する、を固定）。
    common = dict(rci_period=5, atr_period=3, slope_threshold=0, eff_min=0,
                  cooldown_bars=1, floor=0.1, prev_diff_min=0, spike_window_bars=8,
                  spike_depth_min=50, spike_turn_min=50, spike_early=0)
    # 足内 dip なし（安値も終値近傍）→ トラフが出ず発火しない
    shallow = bar_signals(_pin_bars(None), _params(**common), code="T")
    assert len(shallow) == 0
    # ピン足（足内で深い安値 98.0）→ その足で V 底検出→発火
    deep = bar_signals(_pin_bars(98.0), _params(**common), code="T")
    assert len(deep) == 1
    row = deep.iloc[0]
    assert row["direction"] == "long"
    fired_idx = int(round((row["time"] - pd.Timestamp(START2)).total_seconds() / 60))
    assert fired_idx == PIN  # V 底を付けたバーそのもの（早期化の核心）


# ── トゲ早期化: 寄り値リカバリ・ピン足即断・鮮度ガード（要件 2026-07-21） ──
# 「順張りでトゲが出てるのに1本後にしか入らない・遅い」への対応。谷から遠い（lag大）遅いエントリーを
# 排除しつつ、寄りギャップで戻りが確定した場面は open[t] で 1 本早く取る。

# 上昇でウォームアップ→急落（RCI トラフ）→バー GAP の寄りでギャップアップ（戻り確定）。
_EARLY_CLOSES = [95, 96, 97, 98, 99, 100, 100.05, 99.9, 99.7, 99.5, 99.3, 100.2, 100.4, 100.6]
_EARLY_GAP = 11
_EARLY_GAP_OPEN = 100.1   # close[10]=99.3 に対しギャップアップして始まる
_EARLY_PARAMS = dict(rci_period=5, atr_period=3, slope_threshold=0, eff_min=0, cooldown_bars=1,
                     floor=0.1, prev_diff_min=0, spike_window_bars=8,
                     spike_depth_min=60, spike_turn_min=30)


def _gap_bars():
    """寄りギャップ付きの合成バー（GAP バーだけ open を持ち上げる）。"""
    closes = _EARLY_CLOSES
    n = len(closes)
    rows = []
    for i, cl in enumerate(closes):
        op = closes[i - 1] if i > 0 else cl - 0.02
        if i == _EARLY_GAP:
            op = _EARLY_GAP_OPEN
        hi = max(op, cl) + 0.03
        lo = min(op, cl) - 0.03
        rows.append((op, hi, lo, cl))
    o, h, l, c = zip(*rows)
    return pd.DataFrame({
        "time": [pd.Timestamp(START) + pd.Timedelta(minutes=i) for i in range(n)],
        "open": np.array(o, float), "high": np.array(h, float),
        "low": np.array(l, float), "close": np.array(c, float),
        "volume": np.full(n, 100, np.int64), "turnover": np.array(c, float) * 100.0,
        "source": "x",
    })


def _idx_of(row):
    return int(round((row["time"] - pd.Timestamp(START)).total_seconds() / 60))


def test_spike_early_open_recovery_enters_at_open():
    """(u) 寄り値リカバリ: early=1 は寄りギャップで戻りが確定した GAP バーを **open[t] で** 取る。

    early=0（従来）は同じ GAP バーを **close[t] で** 取り、評価は close[t+1] 起点。early=1 は entry_kind='open'・
    entry=open[GAP] で、評価は「エントリー後 +1本＝close[GAP]」＝1 本早い保有になることを固定する。"""
    bars = _gap_bars()
    o = bars["open"].to_numpy(); c = bars["close"].to_numpy()
    g = _EARLY_GAP

    off = bar_signals(bars, _params(spike_early=0, **_EARLY_PARAMS), code="T")
    row0 = off[[_idx_of(r) == g for _, r in off.iterrows()]].iloc[0]
    assert row0["entry_kind"] == "close"
    assert row0["entry_price"] == c[g]
    assert abs(row0["ret_1bar_gross"] - (c[g + 1] - c[g]) / c[g] * 1e4) < 1e-6   # close 起点＝close[g+1]

    on = bar_signals(bars, _params(spike_early=1, **_EARLY_PARAMS), code="T")
    row1 = on[[_idx_of(r) == g for _, r in on.iterrows()]].iloc[0]
    assert row1["entry_kind"] == "open"
    assert row1["entry_price"] == o[g]                                            # 寄り値でエントリー
    assert abs(row1["ret_1bar_gross"] - (c[g] - o[g]) / o[g] * 1e4) < 1e-6        # open 起点＝+1本は close[g]


def test_spike_early_zero_is_close_only_backward_compat():
    """(v) spike_early=0 は従来の終値トゲと完全一致（全 entry_kind='close'・entry=close[t]）。"""
    bars = _bars(SPIKE_V_CLOSES)
    common = dict(slope_threshold=0, eff_min=0, cooldown_bars=1, prev_diff_min=0, spike_window_bars=8,
                  spike_depth_min=150, spike_turn_min=100)
    e = bar_signals(bars, _params(spike_early=0, **common), code="T")
    assert len(e) >= 1
    assert set(e["entry_kind"]) == {"close"}
    for _, r in e.iterrows():
        t = _idx_of(r)
        assert r["entry_price"] == bars["close"].to_numpy()[t]                    # 終値エントリー


def test_spike_freshness_gate_drops_stale_entries():
    """(w) 鮮度ガード: RCI9 の直近5本極値（トゲの根元）から SPIKE_MAX_LAG 本を超えたバーは early=1 で発火しない。

    谷が index5、以後ゆるやかに回復し index9 は谷から lag4。early=1（鮮度ガード）は None、early=0（ガード無し）は
    発火することで、「谷から遠い遅いエントリーを落とす」挙動を _spike_entry 単体で固定する。"""
    rci = np.array([80, 60, 30, -20, -60, -95, -70, -40, -10, 20, 50], float)
    arr = {"rci": rci, "rci_low": rci.copy(), "rci_high": rci.copy(), "rci_open": rci.copy(),
           "c": np.linspace(100, 101, 11), "o": np.linspace(100, 101, 11),
           "h": np.linspace(100, 101, 11) + 0.1, "l": np.linspace(100, 101, 11) - 0.1}
    ws = max(0, 9 - 8 + 1)
    assert SPIKE_MAX_LAG == 3
    assert _spike_entry(arr, 9, ws, "long", 60, 30, True) is None                 # lag4 → 鮮度で棄却
    assert _spike_entry(arr, 9, ws, "long", 60, 30, False) is not None            # ガード無しなら発火


def test_entry_kind_column_in_schema():
    """(x) entries スキーマに entry_kind 列がある（空入力でも保つ）。"""
    e = bar_signals(pd.DataFrame(), _params(), code="T", eval_bars=(1, 2, 3))
    assert "entry_kind" in e.columns


def test_empty_bars_returns_contract_columns():
    """空入力でも列スキーマを保つ。"""
    e = bar_signals(pd.DataFrame(), _params(), code="T", eval_bars=(1, 2, 3))
    assert len(e) == 0
    for col in ("direction", "entry_price", "ret_1bar_net", "ret_3bar_gross", "eval_truncated"):
        assert col in e.columns


# ── 注目（事前アラート）シグナル bar_attention（要件 2026-07-19） ──
# エントリーとは独立の前段アラート。トゲ落差が閾値の 70% まで来たが戻り未確認の状態で発火する。

_ATT_COMMON = dict(slope_threshold=0, eff_min=0, cooldown_bars=1, prev_diff_min=0,
                   spike_window_bars=8, spike_depth_min=150, spike_turn_min=100)


def test_attention_fires_before_entry_on_v_bottom():
    """(j) 注目はエントリーより前（V 底＝戻り未確認の段階）でロングとして 1 回だけ発火する。

    SPIKE_V_CLOSES では RCI が +100→−100 と落ちきったトラフ足で depth が閾値の 70% に達し戻り未確認
    ＝注目。実際の V 字切り返し（＝エントリー）はその数本後に出る。注目の時刻がエントリー初回より
    早いこと（事前アラートの本質）と、同一フォーメーション内は初回のみ（armed）＝件数 1 を固定する。"""
    bars = _bars(SPIKE_V_CLOSES)
    p = _params(**_ATT_COMMON)
    att = bar_attention(bars, p, code="T")
    assert len(att) == 1                                   # 同一フォーメーションは初回のみ
    assert att.iloc[0]["direction"] == "long"             # 下向きトゲ形成中＝ロング注目
    ent = bar_signals(bars, p, code="T")
    assert len(ent) >= 1
    assert att.iloc[0]["time"] < ent.iloc[0]["time"]      # 注目はエントリーより早い＝事前アラート


def test_attention_short_on_inverse_v_peak():
    """(k) 逆 V ピーク（SAR が価格の上）ではショートの注目が出る。方向が SAR に従うことを固定。"""
    mirror = [200.0 - x for x in SPIKE_V_CLOSES]           # 価格を反転→強い下落＋逆 V ピーク
    att = bar_attention(_bars(mirror), _params(**_ATT_COMMON), code="T")
    assert len(att) == 1
    assert att.iloc[0]["direction"] == "short"


def test_attention_disabled_when_spike_off():
    """(l) トゲ無効（spike_depth_min=0）や戻り閾値なし（spike_turn_min=0）では注目を出さない。

    spike_turn_min=0 だと「戻り未確認」という状態が定義できない（常に確認済み扱い）ため空にする、
    という v1 の設計をピン留めする。"""
    bars = _bars(SPIKE_V_CLOSES)
    off_depth = {**_ATT_COMMON, "spike_depth_min": 0}
    off_turn = {**_ATT_COMMON, "spike_turn_min": 0}
    assert len(bar_attention(bars, _params(**off_depth), code="T")) == 0
    assert len(bar_attention(bars, _params(**off_turn), code="T")) == 0


def test_attention_does_not_change_entries():
    """(m) 注目算出はエントリーに一切影響しない（要件「既存エントリー数値の不変」）。

    bar_attention を挟んでも bar_signals の出力が完全一致することで、両者が独立であることを固定する。"""
    p = _params(**_ATT_COMMON)
    before = bar_signals(_bars(SPIKE_V_CLOSES), p, code="T").copy()
    _ = bar_attention(_bars(SPIKE_V_CLOSES), p, code="T")   # 副作用があれば後段が変わる
    after = bar_signals(_bars(SPIKE_V_CLOSES), p, code="T").copy()
    assert before.equals(after)


def test_attention_empty_bars_returns_columns():
    """(n) 空入力でも ATTENTION_COLUMNS のスキーマを保つ。"""
    e = bar_attention(pd.DataFrame(), _params(**_ATT_COMMON), code="T")
    assert len(e) == 0
    for col in ("time", "code", "direction", "rci", "sar", "depth", "turn"):
        assert col in e.columns


# ── 第2トリガー「RCI 同調」＋符号ゲートのレベル化＋trigger 列（要件 2026-07-19・教師注釈由来） ──
# デュアル用は RCI9/RCI14 の両方を確定させる必要があるため、まず十分な下げで RCI を確定・負にしてから
# 強い上げで両 RCI を同時上昇させる（窓は 5 本に縮めて RCI14 の 14 本ウォームアップ分を確保）。
_DUAL_DOWN = [130 - i for i in range(16)]                     # 16 本の下げ → RCI9/14 を確定・−100 付近へ
_DUAL_UP = [_DUAL_DOWN[-1] + 3 * (i + 1) for i in range(8)]   # 8 本の強い上げ → 両 RCI 同時上昇
DUAL_CLOSES = _DUAL_DOWN + _DUAL_UP
_DUAL_COMMON = dict(rci_period=9, atr_period=3, slope_threshold=0, prev_diff_min=0,
                    rci_sign_gate=0, rci_sign_level=0, spike_depth_min=0, spike_turn_min=0,
                    spike_window_bars=5, eff_min=0, cooldown_bars=1, floor=0.1)


def test_dual_rci_trigger_fires_when_both_rci_rise():
    """(o) RCI 同調: 短期(9)・長期(14)が窓内で揃って上昇するとロングが trigger='dual' で発火する。

    教師注釈「9 と 14 が一緒に右肩上がりだと強い」の実装。dual_rci_slope_min を上げるとゲートが
    実効（発火が減る）ことも確認する。"""
    bars = _bars(DUAL_CLOSES)
    fire = bar_signals(bars, _params(dual_rci_slope_min=20, **_DUAL_COMMON), code="T")
    assert len(fire) >= 1
    assert all(d == "long" for d in fire["direction"])
    assert all(tr == "dual" for tr in fire["trigger"])          # 発火根拠は同調トリガー
    strict = bar_signals(bars, _params(dual_rci_slope_min=200, **_DUAL_COMMON), code="T")
    assert len(strict) < len(fire)                              # 到達不能な傾き閾値ならゲートが実効


def test_dual_disabled_is_noop():
    """(p) dual_rci_slope_min=0（既定）は同調トリガーを一切足さない。

    トゲ OFF・角度 OFF（slope 高値で角度パスは全不発）にすると、dual 0 では 0 件・dual>0 では同調で
    発火することで、同調が dual パラメータ由来だと固定する。"""
    bars = _bars(DUAL_CLOSES)
    base = dict(_DUAL_COMMON)
    base["slope_threshold"] = 500          # 角度パスは事実上全不発
    off = bar_signals(bars, _params(dual_rci_slope_min=0, **base), code="T")
    on = bar_signals(bars, _params(dual_rci_slope_min=20, **base), code="T")
    assert len(off) == 0
    assert len(on) >= 1 and all(tr == "dual" for tr in on["trigger"])


def test_dual_or_spike_composition_trigger_column():
    """(q) トゲ OR 同調: 両方 ON のとき、V が無い上昇局面では同調(dual)で発火し、V 底では トゲ(spike)で発火。

    DUAL_CLOSES（単調上昇＝トゲ V なし）は両 ON でも dual だけが発火。SPIKE_V_CLOSES（RCI が深い V）は
    同調不成立（窓分の RCI14 が揃わない）でも spike が発火。trigger 列で両者を区別できることを固定する。"""
    both = bar_signals(_bars(DUAL_CLOSES),
                       _params(spike_depth_min=150, spike_turn_min=100, dual_rci_slope_min=20, **{k: v for k, v in _DUAL_COMMON.items() if k not in ("spike_depth_min", "spike_turn_min")}),
                       code="T")
    assert len(both) >= 1 and set(both["trigger"]) == {"dual"}   # 上昇局面は同調のみ（トゲ V が無い）
    spike_only = bar_signals(_bars(SPIKE_V_CLOSES),
                             _params(slope_threshold=0, eff_min=0, cooldown_bars=1, prev_diff_min=0,
                                     spike_window_bars=8, spike_depth_min=150, spike_turn_min=100,
                                     dual_rci_slope_min=20), code="T")
    assert len(spike_only) >= 1 and set(spike_only["trigger"]) == {"spike"}


def test_rci_sign_level_generalizes_boundary():
    """(r) 符号ゲートのレベル化: 境界を ±level にすると「十分に安値/高値圏でだけ」反転側を取れる。

    DUAL_CLOSES の転換点（下げ切って価格が SAR を上抜けた最初のバー）は「ロング方向だが RCI はまだ深い
    マイナス圏（≈−32）」という反転ロードの根元。角度パス（slope=0＝全バー候補）＋符号ゲートで:
      level=0/20 → rci<−level を満たし発火（rci≈−32 < −20）／level=40 → rci<−40 を満たさず gate。
    という形で境界が ±level に動くことを固定する。level=0 の従来一致は (s) で別途固定。"""
    bars = _bars(DUAL_CLOSES)
    gate = dict(rci_period=9, atr_period=3, slope_threshold=0, prev_diff_min=0,
                spike_depth_min=0, spike_turn_min=0, dual_rci_slope_min=0, spike_window_bars=5,
                eff_min=0, cooldown_bars=1, floor=0.1, rci_sign_gate=1)
    e0 = bar_signals(bars, _params(rci_sign_level=0, **gate), code="T")
    e20 = bar_signals(bars, _params(rci_sign_level=20, **gate), code="T")
    e40 = bar_signals(bars, _params(rci_sign_level=40, **gate), code="T")
    assert len(e0) == 1 and e0.iloc[0]["direction"] == "long" and e0.iloc[0]["rci"] < 0   # 負圏の反転ロング
    assert len(e20) == 1 and e20.iloc[0]["rci"] < -20                                       # rci<-20 は通過
    assert len(e40) == 0                                                                    # rci<-40 は満たさず gate


def test_rci_sign_level_zero_matches_legacy():
    """(s) rci_sign_level=0 は従来（0 境界）と完全一致する（後方互換）。

    符号ゲート ON で level=0 と level 未指定（既定 0）の結果が一致することで、レベル化が既定挙動を
    壊していないことを固定する。"""
    for closes in (LONG_CLOSES, SHORT_CLOSES):
        a = bar_signals(_bars(closes), _params(cooldown_bars=3, rci_sign_gate=1, rci_sign_level=0), code="T")
        b = bar_signals(_bars(closes), _params(cooldown_bars=3, rci_sign_gate=1), code="T")  # level 未指定＝既定 0
        assert a.reset_index(drop=True).equals(b.reset_index(drop=True))


def test_new_params_default_unchanged_and_trigger_column():
    """(t) 新パラメータ既定（dual=0, level=0）で従来と完全一致し、trigger 列が付く。

    既存の各モード（角度型・トゲ単独）で、新パラメータを明示 0 で渡しても渡さなくても entries の
    数値が完全一致することを固定（要件「既存エントリー数値の不変」）。併せて trigger 列の値が
    角度型＝'angle'、トゲ単独＝'spike' になることを確認する。"""
    # 角度型（従来）: dual/level 明示 0 と未指定が一致し、trigger は 'angle'
    ang_default = bar_signals(_bars(LONG_CLOSES), _params(), code="T")
    ang_explicit = bar_signals(_bars(LONG_CLOSES), _params(dual_rci_slope_min=0, rci_sign_level=0), code="T")
    assert ang_default.reset_index(drop=True).equals(ang_explicit.reset_index(drop=True))
    assert len(ang_default) >= 1 and set(ang_default["trigger"]) == {"angle"}
    # トゲ単独（従来）: trigger は 'spike'
    spk = bar_signals(_bars(SPIKE_V_CLOSES),
                      _params(slope_threshold=0, eff_min=0, cooldown_bars=1, prev_diff_min=0,
                              spike_window_bars=8, spike_depth_min=150, spike_turn_min=100), code="T")
    assert len(spk) >= 1 and set(spk["trigger"]) == {"spike"}


def test_sar_filter_default_matches_legacy():
    """(u) sar_filter=1（既定）は従来と完全一致する（後方互換）。

    SAR 方向フィルタは常時 ON が既定。既定・明示 1・未指定のいずれでも entries が完全一致し、
    トグル追加が既存挙動を壊していないことを固定する。"""
    for closes in (LONG_CLOSES, SHORT_CLOSES):
        a = bar_signals(_bars(closes), _params(cooldown_bars=3), code="T")                 # 未指定＝既定 1
        b = bar_signals(_bars(closes), _params(cooldown_bars=3, sar_filter=1), code="T")   # 明示 1
        assert a.reset_index(drop=True).equals(b.reset_index(drop=True))


def test_sar_filter_off_is_superset():
    """(u2) sar_filter=0 は SAR 方向で候補を絞らないため、発火は ON(=1) の上位集合になる。

    フィルタ OFF では各バーで両方向を試すので、ON で出た (time, direction) は全て OFF でも出る
    （方向別クールダウンは反対方向の追加発火に影響されない）。同時に、反対トゲが取れる LONG_CLOSES
    では OFF の方が発火数が増えることを固定する。"""
    e_on = bar_signals(_bars(LONG_CLOSES), _params(cooldown_bars=1, sar_filter=1), code="T")
    e_off = bar_signals(_bars(LONG_CLOSES), _params(cooldown_bars=1, sar_filter=0), code="T")
    assert len(e_off) >= len(e_on)
    on_keys = {(r["time"], r["direction"]) for _, r in e_on.iterrows()}
    off_keys = {(r["time"], r["direction"]) for _, r in e_off.iterrows()}
    assert on_keys <= off_keys, "SAR フィルタ有りの発火は無しの上位集合であるべき"
    assert len(e_off) > len(e_on), "反対トゲが取れる系列では OFF の方が発火が増えるはず"


def test_skip_open_bars_suppresses_morning_open():
    """(v) 前場フレームで先頭 skip_open_bars 本はエントリーを抑制する（評価そのものは不変）。

    START=09:35 の前場フレームで LONG は先頭側（09:45/09:48）で発火する。skip_open_bars=15 を課すと
    フレーム先頭から 15 本未満が抑制され、当該フレームの発火が消えることを固定する。"""
    e_full = bar_signals(_bars(LONG_CLOSES), _params(cooldown_bars=3), code="T")
    assert len(e_full) >= 1
    e_skip = bar_signals(_bars(LONG_CLOSES), _params(cooldown_bars=3, skip_open_bars=15), code="T")
    assert len(e_skip) == 0, "前場先頭 15 本内の発火は寄り抑制で消えるはず"


def test_skip_open_bars_only_morning():
    """(v2) 寄り抑制は前場フレームだけに効く。後場（先頭 12:30）は skip_open_bars を無視する。

    同じ close 列でも、前場（先頭 09:35, hour<12）は抑制され、後場（先頭 12:30, hour>=12）は
    フィルタ無しと同数のまま。前場だけに寄り抑制が適用されることを固定する。"""
    p_skip = _params(cooldown_bars=3, skip_open_bars=15)
    e_am = bar_signals(_bars(LONG_CLOSES, start="2099-01-05 09:35:00"), p_skip, code="T")
    e_pm = bar_signals(_bars(LONG_CLOSES, start="2099-01-05 12:30:00"), p_skip, code="T")
    e_pm_nofilter = bar_signals(_bars(LONG_CLOSES, start="2099-01-05 12:30:00"),
                                _params(cooldown_bars=3), code="T")
    assert len(e_pm) == len(e_pm_nofilter), "後場フレームには寄り抑制が効かないはず"
    assert len(e_am) < len(e_pm), "前場フレームだけ寄り抑制で発火が減るはず"


# --- σフェード（第3トリガー・急変逆張り。要件 2026-07-20） ---
# 静穏（小さなリターン）→ 一発の大きな急変、という系列で急変バーの逆方向に発火することを見る。
# fade_sigma_window はテスト用に小さく（=5）して 60 本の助走を不要にする。角度閾値を極大にして
# 既存の角度パスを黙らせ、fade だけを単離して観察する。

def _fade_params(**over):
    p = _params(slope_threshold=9999.0, eff_min=0.0, cooldown_bars=1,
                fade_sigma_window=5, fade_sigma_k=3.0)
    p.update(over)
    return p


def _quiet_then_spike(direction_up, base=100.0):
    """静穏（±0.05% の交互）11 本 → index 12 で大きな急変（±2%）→ その後 2 本、の close 列。"""
    closes = [base]
    for i in range(1, 12):
        closes.append(round(closes[-1] * (1.0 + (0.0005 if i % 2 else -0.0005)), 4))
    jump = 1.02 if direction_up else 0.98
    closes.append(round(closes[-1] * jump, 4))   # index 12 = 急変
    closes.append(round(closes[-1] * 1.0005, 4))
    closes.append(round(closes[-1] * 0.9995, 4))
    return closes


def _uptrend_then_spike(base=100.0):
    """緩やかな上昇（価格>SAR のロング領域・σ>0 の微変動）→ index 12 で一発の大きな上げ急変。"""
    closes = [base]
    for s in [0.10, 0.12, 0.08, 0.11, 0.09, 0.13, 0.10, 0.12, 0.08, 0.11, 0.09]:
        closes.append(round(closes[-1] + s, 4))
    closes.append(round(closes[-1] + 3.0, 4))    # 大きな上げ急変（通常の ~30 倍）
    closes.append(round(closes[-1] + 0.10, 4))
    closes.append(round(closes[-1] + 0.10, 4))
    return closes


def test_fade_fires_short_on_up_spike():
    """(fade-a) 上げ急変で逆張りショートが trigger='fade' で急変バーに発火する。"""
    bars = _bars(_quiet_then_spike(direction_up=True))
    e = bar_signals(bars, _fade_params(), code="T", eval_bars=(1, 2))
    fade = e[e["trigger"] == "fade"]
    assert len(fade) >= 1
    row = fade.iloc[0]
    assert row["direction"] == "short"   # 上げ急変 → 逆張りショート
    t = int(round((row["time"] - pd.Timestamp(START)).total_seconds() / 60))
    assert t == 12, "急変バー（index 12）で発火するはず"


def test_fade_fires_long_on_down_spike():
    """(fade-b) 下げ急変では逆方向＝ロングに発火する（方向がリターン符号の逆）。"""
    bars = _bars(_quiet_then_spike(direction_up=False))
    e = bar_signals(bars, _fade_params(), code="T", eval_bars=(1, 2))
    fade = e[e["trigger"] == "fade"]
    assert len(fade) >= 1
    assert fade.iloc[0]["direction"] == "long"


def test_fade_disabled_matches_legacy():
    """(fade-c) fade_sigma_k=0（既定）は無効。明示 0 と未指定で既存エントリーが完全一致する（後方互換）。"""
    # 既存の角度型（LONG_CLOSES・既定 slope=15）で fade 明示 0 は未指定と完全一致
    la = bar_signals(_bars(LONG_CLOSES), _params(), code="T")
    lb = bar_signals(_bars(LONG_CLOSES), _params(fade_sigma_k=0.0, fade_sigma_window=5), code="T")
    assert la.reset_index(drop=True).equals(lb.reset_index(drop=True))
    assert len(la) >= 1
    # 急変系列でも fade 無効なら（角度は 9999 で黙っているので）1 件も出ない
    bars = _bars(_quiet_then_spike(direction_up=True))
    e_off = bar_signals(bars, _fade_params(fade_sigma_k=0.0), code="T", eval_bars=(1, 2))
    assert len(e_off) == 0


def test_fade_ignores_sar_direction_filter():
    """(fade-d) 上昇トレンド（価格>SAR）でも上げ急変で SAR に逆らってショートを出す＝SAR 非依存。"""
    bars = _bars(_uptrend_then_spike())
    p = _fade_params(sar_filter=1)   # SAR フィルタ ON でも fade は独立
    e = bar_signals(bars, p, code="T", eval_bars=(1, 2))
    fade = e[e["trigger"] == "fade"]
    assert len(fade) >= 1
    row = fade.iloc[0]
    assert row["direction"] == "short"
    assert row["sar"] < row["signal_close"], "価格>SAR（SAR的にはロング領域）でも fade はショート＝SAR非依存"
    # fade を切ると sar_filter=1 の上昇トレンドはロングしか許されず、ショートは 1 件も出ない
    e_off = bar_signals(bars, {**p, "fade_sigma_k": 0.0}, code="T", eval_bars=(1, 2))
    assert int((e_off["direction"] == "short").sum()) == 0


def test_fade_or_composition_with_spike():
    """(fade-e) fade はトゲ等の既存トリガーと OR（fade を足すと発火が減らない＝上位集合）。"""
    bars = _bars(_quiet_then_spike(direction_up=True))
    base = _params(slope_threshold=0.0, eff_min=0.0, cooldown_bars=1,
                   spike_depth_min=150, spike_turn_min=100, spike_window_bars=8)
    e_spike = bar_signals(bars, base, code="T", eval_bars=(1, 2))
    e_both = bar_signals(bars, {**base, "fade_sigma_k": 3.0, "fade_sigma_window": 5},
                         code="T", eval_bars=(1, 2))
    assert len(e_both) >= len(e_spike), "fade を OR で足して発火が減ることはない"
    assert "fade" in set(e_both["trigger"]), "fade 由来の発火が含まれるはず"


# --- ボリンジャーフェード（第4トリガー・逆張り。要件 2026-07-20・σフェードの近縁） ---
# z=(close−SMA20)/std20 が閾値超で逆張り。テストは窓を小さく（=5）して助走を短くする。

def _boll_params(**over):
    # 窓 10（母標準偏差 z の上限 sqrt(9)=3.0）で、急変バーの z≈2.9〜3.0 が閾値 2.0 を明確に超える。
    p = _params(slope_threshold=9999.0, eff_min=0.0, cooldown_bars=1,
                fade_boll_window=10, fade_boll_z=2.0)
    p.update(over)
    return p


def test_fade_boll_fires_short_on_upper_band_break():
    """(boll-a) 上バンド超え（z>0）で逆張りショートが trigger='fadeb' で発火する。"""
    bars = _bars(_quiet_then_spike(direction_up=True))
    e = bar_signals(bars, _boll_params(), code="T", eval_bars=(1, 2))
    fb = e[e["trigger"] == "fadeb"]
    assert len(fb) >= 1
    assert fb.iloc[0]["direction"] == "short"   # 上バンド超え → 逆張りショート


def test_fade_boll_fires_long_on_lower_band_break():
    """(boll-b) 下バンド割れ（z<0）では逆方向＝ロングに発火する。"""
    bars = _bars(_quiet_then_spike(direction_up=False))
    e = bar_signals(bars, _boll_params(), code="T", eval_bars=(1, 2))
    fb = e[e["trigger"] == "fadeb"]
    assert len(fb) >= 1
    assert fb.iloc[0]["direction"] == "long"


def test_fade_boll_disabled_matches_legacy():
    """(boll-c) fade_boll_z=0（既定）は無効。明示 0 と未指定で既存エントリーが完全一致する（後方互換）。"""
    la = bar_signals(_bars(LONG_CLOSES), _params(), code="T")
    lb = bar_signals(_bars(LONG_CLOSES), _params(fade_boll_z=0.0, fade_boll_window=5), code="T")
    assert la.reset_index(drop=True).equals(lb.reset_index(drop=True))
    assert len(la) >= 1
    # 急変系列でも fade_boll 無効なら（角度は 9999 で黙っているので）1 件も出ない
    bars = _bars(_quiet_then_spike(direction_up=True))
    e_off = bar_signals(bars, _boll_params(fade_boll_z=0.0), code="T", eval_bars=(1, 2))
    assert len(e_off) == 0


def test_fade_boll_ignores_sar_direction_filter():
    """(boll-d) 上昇トレンド（価格>SAR）でも上バンド超えで SAR に逆らってショートを出す＝SAR 非依存。"""
    bars = _bars(_uptrend_then_spike())
    p = _boll_params(sar_filter=1)   # SAR フィルタ ON でも fade_boll は独立
    e = bar_signals(bars, p, code="T", eval_bars=(1, 2))
    fb = e[e["trigger"] == "fadeb"]
    assert len(fb) >= 1
    row = fb.iloc[0]
    assert row["direction"] == "short"
    assert row["sar"] < row["signal_close"], "価格>SAR（SAR的にはロング領域）でも fade_boll はショート＝SAR非依存"
    e_off = bar_signals(bars, {**p, "fade_boll_z": 0.0}, code="T", eval_bars=(1, 2))
    assert int((e_off["direction"] == "short").sum()) == 0
