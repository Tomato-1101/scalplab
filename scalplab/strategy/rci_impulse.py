"""RCI インパルス戦略（RCI × Parabolic SAR）。

手法（要件の定式化）:
  - 方向の絶対条件: 現在価格 > SAR ならロングのみ / < SAR ならショートのみ。
  - トリガー(角度): 直近 W 秒の intrabar RCI 変化 ΔRCI が閾値以上（ロングは +、ショートは −）。
  - トリガー(直前差 prev_diff): 「一個前の値との差が大きい尖った RCI」（要件の核心）を捉えるため、
    直前に確定した 1 分足の RCI 確定値と、現在の intrabar RCI との差 prev_diff = RCI_intrabar − RCI_confirmed を
    見る。prev_diff_min（0 で無効）を満たすことを角度ゲートとは独立の AND 条件として課す。角度（W 秒窓の
    持続的な傾き）が同じでも、直近の確定足から一気にジャンプした「尖った」瞬間だけを残せる。
  - RCI 符号ゲート rci_sign_gate（要件 2026-07-18。0 で無効）: なぜ入れるか＝「RCI がプラス圏で
    生えた上向きトゲの根元でだけショート／マイナス圏の下向きトゲの根元でだけロング」したい。有効時、
    シグナル時点の短期 intrabar RCI が > 0 ならショートのみ・< 0 ならロングのみ許可（ちょうど 0 は両不許可）。
    上の角度ゲート（ロングは ΔRCI>0＝上昇、ショートは ΔRCI<0＝下降を要求）と AND することで、合成挙動は
    「プラス圏で RCI が鋭く下折れした瞬間＝上向きトゲの根元でショート」「マイナス圏で鋭く上折れした瞬間＝
    下向きトゲの根元でロング」になる。SAR 方向・角度・直前差・効率・クールダウンは一切変えない追加 AND。
  - トゲ反転ゲート spike_depth_min / spike_turn_min（要件 2026-07-18。ともに 0 で無効）: 参考画像の
    ような「短期 RCI が数本で +80 付近から −100 近くまで垂直に落ち、V 字で即切り返す」トゲの反転点を直接
    捉える。角度ゲートが取れない本質的理由＝V 底の切り返し瞬間は窓端点間 ΔRCI がまだ大きく負で、角度条件
    （ロング⟺ΔRCI>0）と同時には満たせない。そこで W 秒窓（slope_window_sec を再利用）内の RCI 列から、
    ロングは「トラフ(窓内最小)までの落差 = 先行高値 − トラフ ≥ spike_depth_min」かつ「トラフからの戻り =
    現在RCI − トラフ ≥ spike_turn_min」を課す（V 底の切り返し）。ショートは鏡像（逆 V 天井: ピーク − 先行安値、
    ピーク − 現在RCI）。方向はトゲの反転方向で決まるが SAR 絶対条件と AND なので、上昇トレンド中の V 底＝ロング・
    下降トレンド中の逆 V＝ショートに自然に一致する。合成方針: 他ゲートと同じ独立 AND。よって角度・直前差と
    同時 ON は上記の理由で両立しにくい（＝トゲ単独で使う想定。角度 OFF・直前差 OFF ＋ トゲ ON で反転点発火）。
  - 効率フィルタ: 「価格が動かないのに RCI が大きく動く」場面を狙い、その逆を除外。
      price_range_pct = (直近W秒のティック高値−安値) / 現在価格
      atr_pct        = ATR(atr_period, 1分足) / 現在価格    ← ボラ正規化
      norm_range     = price_range_pct / atr_pct
      efficiency     = |ΔRCI| / max(norm_range, floor)      ← floor でゼロ割回避
      efficiency ≥ eff_min のときだけ通過。
  - クールダウン: 同一方向の再シグナルを cooldown 秒抑制。
  - ATR 未確定・RCI ウォームアップ中・SAR 未確定の間はシグナル無効。

パラメータはすべて外出し（既定は仮置き。掃引で探す前提）。判断メモ:
  - atr_pct と price_range_pct の分母「現在価格」は、より live な「現在ティック価格」を用いる
    （確定足 close ではなく）。方向判定・効率判定を最新価格基準で一貫させるため。
"""
from __future__ import annotations

from collections import deque
from typing import Optional

import numpy as np

from scalplab.indicators.atr import AtrState
from scalplab.indicators.parabolic import SarState
from scalplab.indicators.rci import IntrabarRci, rci_series
from scalplab.strategy.base import Strategy

DEFAULT_PARAMS = {
    "rci_period": 9,
    "slope_window_sec": 30,
    "slope_threshold": 15.0,
    "prev_diff_min": 0.0,   # 直前確定足 RCI との差の下限。0 で無効（既定＝従来挙動）
    "rci_sign_gate": 0,     # RCI 符号ゲート。0 で無効（既定＝従来挙動）。1 で有効（下記 on_tick 参照）
    "spike_depth_min": 0.0,  # トゲ反転: 窓内の先行高値→トラフの落差の下限。0 で無効（下記 on_tick 参照）
    "spike_turn_min": 0.0,   # トゲ反転: トラフからの戻り（現在RCI−トラフ）の下限。0 で無効
    "eff_min": 3.0,
    "af_init": 0.02,
    "af_step": 0.02,
    "af_max": 0.2,
    "cooldown_sec": 60,
    "atr_period": 14,
    "floor": 0.1,
    "slippage_ticks": 0,  # コストモデル用（戦略ロジックでは未使用）
}


def merge_params(params: Optional[dict]) -> dict:
    """既定値に上書きをマージした完全なパラメータ dict を返す。"""
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    return p


def spike_reversal_features(w_rci: list[float], rci_now: float):
    """窓内 RCI 列（時刻昇順・finite のみ）から V字/逆V字トゲの落差・戻りを返す。

    なぜ位置依存か: 単なる「窓内 最大−最小」だと単調上昇（トラフが窓頭）でも大きくなり V 底と区別できない。
    そこで **トラフ（窓内最小）より前** の最大＝先行高値を使い、「高値の後にトラフへ落ちた」形だけを拾う。
      - ロング(V 底): depth_long = 先行高値 − トラフ（先行高値が無い＝トラフが窓頭なら −inf で不成立）、
                       turn_long = 現在RCI − トラフ。
      - ショート(逆 V 天井): 鏡像。depth_short = ピーク − 先行安値（先行安値が無ければ −inf）、
                       turn_short = ピーク − 現在RCI。
    トラフ/ピークの同値タイは最初の出現位置を採る（逐次器・ベクトル器の argmin/argmax と一致させるため）。
    戻り値 (depth_long, turn_long, depth_short, turn_short)。空窓は全て −inf。
    """
    if not w_rci:
        return float("-inf"), float("-inf"), float("-inf"), float("-inf")
    trough = min(w_rci)
    tp = w_rci.index(trough)                       # 最初の最小位置
    pre_high = max(w_rci[:tp]) if tp > 0 else float("-inf")
    peak = max(w_rci)
    pp = w_rci.index(peak)                          # 最初の最大位置
    pre_low = min(w_rci[:pp]) if pp > 0 else float("inf")
    depth_long = pre_high - trough
    turn_long = rci_now - trough
    depth_short = peak - pre_low
    turn_short = peak - rci_now
    return depth_long, turn_long, depth_short, turn_short


class RciImpulseStrategy(Strategy):
    def __init__(self, params: Optional[dict] = None, code: str = "?"):
        self._p = merge_params(params)
        self.code = code
        self._period = int(self._p["rci_period"])
        self._rci = IntrabarRci(self._period)
        self._sar = SarState(self._p["af_init"], self._p["af_step"], self._p["af_max"])
        self._atr = AtrState(int(self._p["atr_period"]))
        self._sar_val = float("nan")
        self._trend = 0
        self._atr_val = float("nan")
        # 直前差ゲート用: 確定足だけで計算した最新の確定 RCI を保持する。
        # IntrabarRci は period-1 本しか持たないため、確定 RCI 算出用に period 本を別途溜める。
        self._prev_diff_min = float(self._p.get("prev_diff_min", 0.0))
        self._conf_closes: deque[float] = deque(maxlen=self._period)
        self._last_conf_rci = float("nan")
        # (t_ns, rci, price) のリングバッファ。ΔRCI と価格レンジの算出に使う
        self._buf: deque[tuple[int, float, float]] = deque()
        self._w_ns = int(float(self._p["slope_window_sec"]) * 1e9)
        self._cooldown_ns = int(float(self._p["cooldown_sec"]) * 1e9)
        self._last_sig_ns = {"long": None, "short": None}

    @property
    def params(self) -> dict:
        return dict(self._p)

    def on_bar_confirm(self, bar: dict) -> None:
        h, l, c = float(bar["high"]), float(bar["low"]), float(bar["close"])
        self._sar_val, self._trend = self._sar.confirm_bar(h, l, c)
        self._atr_val = self._atr.confirm_bar(h, l, c)
        self._rci.confirm_bar(c)
        # 確定足だけの最新 RCI を更新（直前差ゲートの基準）。period 本溜まるまでは NaN。
        self._conf_closes.append(c)
        if len(self._conf_closes) >= self._period:
            arr = np.fromiter(self._conf_closes, dtype=np.float64, count=self._period)
            self._last_conf_rci = float(rci_series(arr, self._period)[-1])

    def on_tick(self, t_ns: int, price: float) -> Optional[dict]:
        t_ns = int(t_ns)
        price = float(price)
        rci = self._rci.update_tick(price)
        self._buf.append((t_ns, rci, price))
        # 2W より古いサンプルは破棄（W秒前の基準参照に少し余裕を持たせる）
        cutoff = t_ns - 2 * self._w_ns
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

        # ウォームアップ / 未確定はシグナル無効
        if not np.isfinite(rci) or not np.isfinite(self._atr_val) or not np.isfinite(self._sar_val):
            return None
        if self._atr_val <= 0.0:
            return None

        # 方向の絶対条件
        if price > self._sar_val:
            direction = "long"
        elif price < self._sar_val:
            direction = "short"
        else:
            return None

        # RCI 符号ゲート（rci_sign_gate!=0 で有効）。短期 RCI が + はショートのみ・− はロングのみ許可
        # （0 は両不許可）。SAR で決めた direction が RCI 符号と食い違う側なら抑制する追加 AND 条件。
        # rci はここまでで finite 保証済み（上のウォームアップ判定を通過している）。
        if int(self._p.get("rci_sign_gate", 0)) != 0:
            if not ((rci > 0.0 and direction == "short") or (rci < 0.0 and direction == "long")):
                return None

        # W 秒前の基準 RCI（t <= now−W を満たす最新サンプル）を探す
        target = t_ns - self._w_ns
        ref = None
        for tt, rr, _pp in self._buf:
            if tt <= target and np.isfinite(rr):
                ref = (tt, rr)
            elif tt > target:
                break
        if ref is None:
            return None  # W 秒分の履歴がまだ無い

        drci = rci - ref[1]
        elapsed_sec = (t_ns - ref[0]) / 1e9
        # 直前差（現在の intrabar RCI と直前確定足 RCI の差）。確定 RCI 未確定なら NaN。
        prev_diff = rci - self._last_conf_rci

        # 角度ゲート。slope_threshold <= 0 は無効（prev_diff_min/eff_min と同じ 0=無効 規約。
        # UI で「角度」トグルを OFF にすると slope_threshold=0 が送られ、このゲートを丸ごとスキップする）。
        slope_th = float(self._p["slope_threshold"])
        if slope_th > 0.0:
            if direction == "long" and drci < slope_th:
                return None
            if direction == "short" and drci > -slope_th:
                return None

        # 直前差ゲート（尖り。prev_diff_min <= 0 で無効。基準の確定 RCI が無い間は評価不能→抑制）
        if self._prev_diff_min > 0.0:
            if not np.isfinite(prev_diff):
                return None
            if direction == "long" and prev_diff < self._prev_diff_min:
                return None
            if direction == "short" and prev_diff > -self._prev_diff_min:
                return None

        # トゲ反転ゲート（spike_depth_min/spike_turn_min ともに 0 で無効）。W 秒窓（ref_ok 通過済み）内の
        # RCI 列から V字/逆V字トゲを検出し、方向に一致するトゲの反転点だけを残す独立 AND 条件。
        spike_depth_min = float(self._p.get("spike_depth_min", 0.0))
        spike_turn_min = float(self._p.get("spike_turn_min", 0.0))
        if spike_depth_min > 0.0 or spike_turn_min > 0.0:
            w_rci = [rr for tt, rr, _pp in self._buf if tt >= target and np.isfinite(rr)]
            depth_l, turn_l, depth_s, turn_s = spike_reversal_features(w_rci, rci)
            if direction == "long":
                if (spike_depth_min > 0.0 and depth_l < spike_depth_min) or \
                   (spike_turn_min > 0.0 and turn_l < spike_turn_min):
                    return None
            else:  # short
                if (spike_depth_min > 0.0 and depth_s < spike_depth_min) or \
                   (spike_turn_min > 0.0 and turn_s < spike_turn_min):
                    return None

        # 効率フィルタ（直近 W 秒のティック高安）
        w_prices = [pp for tt, _rr, pp in self._buf if tt >= target]
        hi = max(w_prices)
        lo = min(w_prices)
        price_range_pct = (hi - lo) / price
        atr_pct = self._atr_val / price
        norm_range = price_range_pct / atr_pct
        efficiency = abs(drci) / max(norm_range, float(self._p["floor"]))
        if efficiency < self._p["eff_min"]:
            return None

        # クールダウン（同一方向）
        last = self._last_sig_ns[direction]
        if last is not None and (t_ns - last) < self._cooldown_ns:
            return None
        self._last_sig_ns[direction] = t_ns

        return {
            "t_ns": t_ns,
            "code": self.code,
            "direction": direction,
            "price": price,
            "rci": rci,
            "drci": drci,
            "prev_diff": prev_diff,
            "efficiency": efficiency,
            "sar": self._sar_val,
            "trend": int(self._trend),
            "atr": self._atr_val,
            "atr_pct": atr_pct,
            "price_range_pct": price_range_pct,
            "norm_range": norm_range,
            "slope_elapsed_sec": elapsed_sec,
        }
