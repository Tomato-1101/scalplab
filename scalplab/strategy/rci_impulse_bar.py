"""RCI インパルス戦略のバー近似版（確定 1 分足のみで動く）。

なぜ別実装か（ティック版 rci_impulse.py との関係）:
  本手法の本命は「足内(intrabar)で刻々と動く RCI の角度」をトリガーにするティック駆動版
  （RciImpulseStrategy）である。しかしクロス銘柄スクリーニングでは、moomoo の過去 1 分足しか
  手に入らない（ティックは自分で録画するまで貯まらない）。そこで **確定 1 分足の終値系列だけ**で
  RCI 変化を近似し、どの銘柄で手法が効きそうかを粗くランキングする前段のふるいを用意する。

近似の限界（docstring に明記する要件）:
  - intrabar RCI が使えないため、1 分足の確定値どうしの差分 drci1 = RCI[t] - RCI[t-1] で
    「角度」を近似する。足内のリアルタイム急変は捉えられず、感度は本命より確実に落ちる。
  - よって本モジュールの結果は「本命ティック検証に進む価値のある銘柄を絞る」用途に限る。
    絶対的な期待値ではなく、銘柄間の相対比較として読むこと。

定式化（要件をバー粒度に写像）:
  各確定バー t について、
  - 方向の絶対条件: close[t] > SAR[t] ならロングのみ / close[t] < SAR[t] ならショートのみ。
  - 角度: drci1 = RCI[t] - RCI[t-1]（参考に drci2 = RCI[t] - RCI[t-2] も算出）。
    ロングは drci1 >= +slope_threshold、ショートは drci1 <= -slope_threshold（符号一致 + 閾値）。
  - 直前差ゲート prev_diff（要件の核心＝「一個前の値との差が大きい尖った RCI」）:
    バー粒度では intrabar が無いため「直前差」は 1 本前の確定 RCI との差 drci1 に等しい。
    これを prev_diff として明示化し、prev_diff_min で「尖り」の下限を独立に課す（角度 slope_threshold
    とは別の AND 条件）。prev_diff_min <= 0 のときは無効（既定）で従来挙動と一致する。
    slope_threshold は従来どおり drci1（バー近似の 1 本スロープ＝角度）に掛けるため、prev_diff_min を
    上げると「角度は緩くてもよいが 1 ステップのジャンプが鋭い」場面だけを残せる。
  - RCI 符号ゲート rci_sign_gate（要件 2026-07-18。0 で無効）: 当該バーの確定 RCI が > 0 なら
    ショートのみ・< 0 ならロングのみ許可（0 は両不許可）。角度ゲート（ロング=drci1>0・ショート=drci1<0）
    と AND することで、合成挙動は「プラス圏で RCI が下折れした所でショート／マイナス圏で上折れした所で
    ロング」（トゲの反転側）になる。ティック版 rci_impulse.py と同一意味の追加 AND 条件（他ゲートは不変）。
    符号レベル rci_sign_level（要件 2026-07-19・第2パターン。既定 0）: 境界を 0 から ±rci_sign_level に
    一般化する。ゲート有効時、ロング許可 = rci < -level・ショート許可 = rci > +level。level=0 は従来（0 境界）と
    完全一致。教師注釈「高値圏（RCI 80 近く）で上トゲ→下げでショート」を 0 境界では拾えないため、±レベルで
    「十分に高値/安値圏」でのみ反転側を取れるようにする。
  - 第2トリガー「RCI 同調（デュアル RCI トレンド）」dual_rci_slope_min（要件 2026-07-19・第2パターン。
    0 で無効）: 教師注釈「短期 RCI(9) と長期 RCI(14) が一緒に右肩上がりだと強い」由来。直近 spike_window_bars 本で
    短期(rci_period)・長期(RCI_LONG_PERIOD=14)の**両系列の window 間Δ（値[t]−値[t-窓+1]）が ≥ dual_rci_slope_min**
    ならロング候補（ショートは鏡像＝両系列とも ≤ −dual_rci_slope_min の同時下降）。**トゲトリガーとは OR**
    （どちらかが発火すれば候補）。SAR 絶対条件・効率・クールダウンは従来どおり AND だが、角度・直前差は課さない
    （トリガー自体が発火根拠）。発火根拠は entries の trigger 列（'spike'|'dual'|'angle'）で区別する。
  - トゲ反転ゲート spike_depth_min / spike_turn_min（要件 2026-07-18。ともに 0 で無効。2026-07-19 早期化）:
    直近 spike_window_bars 本の窓から V字（ロング）/逆V字（ショート）トゲを検出する。**足内近似**: t-1 までは
    確定 close RCI、当該バー t は close を low/high に差し替えた足内 RCI（rci_at_low / rci_at_high）をトラフ/
    ピーク候補にする。ロングは「先行高値（トラフより前の最大）− トラフ ≥ spike_depth_min」かつ「当該バー
    close RCI − トラフ ≥ spike_turn_min」（V 底の切り返し確認）。ショートは鏡像。これで「谷を付けたバー
    そのもの」で検出でき、旧＝確定 close の谷→戻り確認に 1〜2 本かかった遅延を解消する。窓は本数
    （spike_window_bars）。角度は 1 本 ΔRCI に掛かるため V 底とは両立しにくく、トゲ単独運用が想定。
  - 効率フィルタ: bar_range_pct = (high[t]-low[t]) / close[t]、atr_pct = ATR(14)/close[t]、
    norm_range = bar_range_pct / atr_pct、efficiency = |drci1| / max(norm_range, floor)。
    efficiency >= eff_min のときだけ通過（価格が動かないのに RCI が動く場面を狙う）。
  - クールダウン: 同一方向の再シグナルを cooldown_bars 本抑制する。
  - 寄り抑制 skip_open_bars（要件 2026-07-19。0 で無効）: 前場の最初 N 本のバーではエントリーを
    出さない（実測で寄り30分は一貫して成績が悪かった）。**シグナル抑制のみ**で評価（+k バー後の差）は変えない。
    呼び手がセッション半分割で渡す前提のため「バーインデックス t < N かつ 前場（フレーム先頭が正午前）」で判定し、
    後場（先頭 12:30）には適用しない。cooldown より前に抑制するため、抑制されたバーは last_sig を更新しない。
  - SAR 方向フィルタ sar_filter（要件 2026-07-19。1=既定・従来どおり）: 1 は「価格>SAR ならロング／
    <SAR ならショート」の絶対条件で方向を決める（従来と完全一致）。0 で無効化し、SAR で方向を絞らずロング・
    ショート両候補をトリガー側の判定に委ねる（教師注釈 7/16 11:17 のロングは価格が SAR 下＝従来禁止側で
    入っており、ユーザーが手法変更を明示許可）。sar フィールド自体は情報として常に記録する。
  - 第3トリガー「σフェード（急変逆張り）」fade_sigma_k / fade_sigma_window（要件 2026-07-20。
    fade_sigma_k=0 で無効）: 1 分リターン ret[t]=(close[t]−close[t−1])/close[t−1] が、直近 fade_sigma_window 本
    （既定 60・当該バーを含まない手前の窓）のリターン標準偏差 σ の k 倍を超えた瞬間に **逆方向**へ入る
    （|ret[t]| > k·σ かつ ret[t]>0→ショート／ret[t]<0→ロング）。実測で ◎選抜銘柄では「本物の急変」（3σ 超）
    の直後 1〜2 本が逆行しやすく、トゲ順張りが大負けした日（7/15-16）にプラスで**トゲと補完関係**にある。
    **SAR 方向フィルタとは独立**（逆張りなので sar_filter の対象外）: 行き過ぎた急変は SAR トレンドの向きに
    関係なく戻りやすいため、fade は SAR で方向を絞らず自分でリターン符号から逆方向を決める。既存トリガー
    （トゲ/同調/角度）とは **OR**（どちらか成立で候補）。クールダウン（同一方向 last_sig）と寄り抑制
    skip_open_bars は共通適用するが、角度・効率フィルタは fade には課さない（RCI 角度でなくリターンが根拠の
    ため）。発火根拠は trigger 列 'fade' で区別。**既定 fade_sigma_k=0 では発火せず既存エントリーは完全不変**。
    σ 計算・発火は共通ウォームアップ（RCI/SAR/ATR 確定）後に評価する（既定 window=60 なら発火が
    それらの確定より十分後になるため。window を極端に小さくしない前提）。
  - 第4トリガー「ボリンジャーフェード」fade_boll_z / fade_boll_window（要件 2026-07-20・σフェードの近縁で
    総当たり検証の最良手法。fade_boll_z=0 で無効）: 直近 fade_boll_window 本（既定 20・当該バーを含む標準ボリンジャー
    定義）の終値 SMA と標準偏差 std（ddof=0）から z = (close[t]−SMA)/std を求め、|z| ≥ fade_boll_z を満たしたら
    **逆張り**（z>0＝上バンド超え→ショート／z<0＝下バンド割れ→ロング）。実測（◎選抜・学習6日/検証6日）で
    z≥3.0・+1本保有・30本CD・3bp が学習/検証の整合が最も良かった（σフェードより良い。サンプルは小さい）。**σフェードと同一の合成規約**: SAR 非依存の逆張り・既存トリガー（トゲ/同調/角度/σフェード）とは OR・
    クールダウンと寄り抑制のみ共通・角度/効率は課さない。発火根拠は trigger 列 'fadeb'。**既定 fade_boll_z=0 では
    発火せず既存エントリーは完全不変**。評価は共通ウォームアップ後（既定 window=20）。
  - ウォームアップ（RCI・ATR・SAR 未確定）はシグナル無効。
  - エントリー = **シグナルバー t の終値 close[t]**（2026-07-19 変更。旧＝翌バー t+1 始値より 1 本早い。
    リアルタイムでは足内 RCI を見て終値近辺で入る、の近似）。評価 = +1/+2/+3 バー後の終値との差
    （close[t+k] − close[t]、k=1,2,3）の順方向リターン（bp）。gross と片道概算コスト cost_bp 控除後の net を出す。

指標は indicators の確定足バッチ（rci_series / sar_series / atr_series）を再利用する。これらは
ティック版が確定足で使う逐次器と構造的に一致するため、両版の確定足挙動はぶれない。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from scalplab.indicators.atr import atr_series
from scalplab.indicators.parabolic import sar_series
from scalplab.indicators.rci import intrabar_rci_batch, rci_series
from scalplab.strategy.rci_impulse import spike_reversal_features

DEFAULT_BAR_PARAMS = {
    "rci_period": 9,
    "slope_threshold": 15.0,
    "prev_diff_min": 0.0,   # 直前差（drci1）の下限。0 以下で無効（既定＝従来挙動）
    "rci_sign_gate": 0,     # RCI 符号ゲート。0 で無効（既定＝従来挙動）。1 で有効（下記 bar_signals 参照）
    "rci_sign_level": 0.0,  # 符号ゲートの境界レベル。0 で従来（0 境界）。>0 で ±level 境界（下記 bar_signals 参照）
    "dual_rci_slope_min": 0.0,  # 第2トリガー「RCI 同調」: 短期・長期 RCI の窓間Δ下限。0 で無効（下記 bar_signals 参照）
    "spike_depth_min": 0.0,  # トゲ反転: 窓内の先行高値→トラフの落差の下限。0 で無効（下記 bar_signals 参照）
    "spike_turn_min": 0.0,   # トゲ反転: トラフからの戻り（当該バーRCI−トラフ）の下限。0 で無効
    "spike_early": 1,        # トゲの早期化（寄り値リカバリ＋ピン足即断）。1=有効（既定）/0=従来の終値のみ（下記 _spike_entry 参照）
    "spike_window_bars": 10,  # トゲ反転／RCI 同調の窓（本数）。ティック版 slope_window_sec 対応。JP 掃引で 6 より捕捉良好
    "eff_min": 2.0,
    "af_init": 0.02,
    "af_step": 0.02,
    "af_max": 0.2,
    "atr_period": 14,
    "cooldown_bars": 3,
    "floor": 0.1,
    "skip_open_bars": 0,    # 前場の寄り N 本はエントリー抑制。0 で無効（下記 bar_signals 参照）
    "sar_filter": 1,        # SAR 方向フィルタ。1=従来（SAR で方向を絶対条件に）/ 0=無効（下記 bar_signals 参照）
    "fade_sigma_k": 0.0,    # 第3トリガー「σフェード」（急変逆張り）。0 で無効（下記 bar_signals 参照）
    "fade_sigma_window": 60,  # σフェードの σ を測る直近リターン本数（既定 60）
    "fade_boll_z": 0.0,     # 第4トリガー「ボリンジャーフェード」。0 で無効（下記 bar_signals 参照）
    "fade_boll_window": 20,  # ボリンジャー z の窓（本数・既定 20。SMA/std とも当該バーを含む標準定義）
}

DEFAULT_EVAL_BARS = (1, 2, 3)

# 第2トリガー「RCI 同調」の長期 RCI 期間（短期は rci_period=9）。表示側 server.RCI_LONG と一致させる
# （教師注釈「9 と 14 が一緒に上がる」の 14）。戦略は webui に依存しないため定数として持つ。
RCI_LONG_PERIOD = 14

# トゲ早期化「ピン足即断」の閾値（定数開始・受け入れ判定でデータ調整可）。
#   SPIKE_PIN_BODY_FRAC: 反転形の下限＝(close−low)/(high−low)（ロング。ショートは (high−close)/range）。
#     0.6 なら「引けがレンジ上位 60%＝深い下ヒゲを付けて戻した」形をピン足とみなす。
#   SPIKE_PIN_TURN_FRAC: ピン足即断で許す戻り turn の緩和率（標準の turn_min のこの割合まで許容）。
SPIKE_PIN_BODY_FRAC = 0.6
SPIKE_PIN_TURN_FRAC = 0.5

# トゲ早期化の「鮮度ガード」（early のみ）。RCI9 の直近 SPIKE_LAG_WIN 本の極値（＝トゲの根元）から
# SPIKE_MAX_LAG 本を超えて離れたバーでは発火しない。谷から遠い所でクールダウン明けに入る「遅い・
# 根元から外れた」エントリー（実測で成績が悪い）を落とし、マーカーを必ずトゲの根元付近に乗せる。
SPIKE_LAG_WIN = 5
SPIKE_MAX_LAG = 3

# 注目（事前アラート）シグナルの閾値（割合）。学習データ（UI 注釈）が溜まったら調整する前提で
# いまは定数で開始する（パラメータは増やさない＝要件）。
ATTENTION_DEPTH_FRAC = 0.70   # 落差が spike_depth_min のこの割合以上に達したら「注目」候補
ATTENTION_RESET_FRAC = 0.50   # 落差がこの割合未満に戻ったらフォーメーション解消とみなしリセット
ATTENTION_COLUMNS = ["time", "code", "direction", "rci", "sar", "depth", "turn"]


def merge_bar_params(params: Optional[dict]) -> dict:
    """既定値に上書きをマージした完全なパラメータ dict を返す。"""
    p = dict(DEFAULT_BAR_PARAMS)
    if params:
        p.update(params)
    return p


def _rci_low_high(c: np.ndarray, low: np.ndarray, high: np.ndarray, period: int):
    """各バー t の「足内トゲ推定 RCI」(rci_at_low, rci_at_high) を返す。

    なぜ必要か（トゲ検出の早期化）: 確定足 close だけで V 底を判定すると、谷を付けたバーの close が
    そのまま谷 RCI になり、戻りが確認できるのは 1〜2 本後＝エントリーが遅れる（ユーザー不満）。そこで
    形成中バーの close を **low に差し替えた RCI(rci_at_low)＝足内トラフ推定** と **high に差し替えた
    RCI(rci_at_high)＝足内ピーク推定** を使い、V 底/逆V 天井をそのバー内で検出できるようにする。
    intrabar_rci_batch（確定足 period-1 本を base に価格を挿入）で計算するので確定 close 版 rci_series と
    数値整合する。前方 period-1 本は NaN。
    """
    c = np.asarray(c, np.float64)
    low = np.asarray(low, np.float64)
    high = np.asarray(high, np.float64)
    n = len(c)
    rlow = np.full(n, np.nan, dtype=np.float64)
    rhigh = np.full(n, np.nan, dtype=np.float64)
    if period < 2 or n < period:
        return rlow, rhigh
    for t in range(period - 1, n):
        base = c[t - period + 1:t]  # period-1 本の直前 close（古→新）
        r = intrabar_rci_batch(base, np.array([low[t], high[t]], dtype=np.float64), period)
        rlow[t] = r[0]
        rhigh[t] = r[1]
    return rlow, rhigh


def _rci_at_open(c: np.ndarray, o: np.ndarray, period: int) -> np.ndarray:
    """各バー t の「寄り値時点の足内 RCI」rci_at_open（形成バー close を open[t] に差し替え）を返す。

    早期化（寄り値リカバリ・エントリー）で使う。バー t の**始値時点**で分かるのは「直前バーまでの確定
    close（period-1 本）＋当該バーの open」だけなので、それらから intrabar_rci_batch で RCI を求める。
    前バー引け〜寄りのギャップで戻りが確定していれば、当該バーの終値を待たず寄りで反転を検出できる。
    前方 period-1 本は NaN。
    """
    c = np.asarray(c, np.float64)
    o = np.asarray(o, np.float64)
    n = len(c)
    ropen = np.full(n, np.nan, dtype=np.float64)
    if period < 2 or n < period:
        return ropen
    for t in range(period - 1, n):
        base = c[t - period + 1:t]
        ropen[t] = intrabar_rci_batch(base, np.array([o[t]], dtype=np.float64), period)[0]
    return ropen


def bar_entry_columns(eval_bars=DEFAULT_EVAL_BARS) -> list[str]:
    """entries DataFrame の列順（シグナル 0 件でも同スキーマを返すため）。"""
    base = [
        "time", "code", "direction", "trigger", "entry_kind", "signal_close", "entry_price",
        "rci", "drci1", "drci2", "prev_diff", "efficiency", "sar", "trend", "atr",
        "atr_pct", "bar_range_pct", "norm_range", "cost_bp", "eval_truncated",
    ]
    for k in eval_bars:
        base += [f"ret_{k}bar_gross", f"ret_{k}bar_net"]
    return base


def _spike_pass(rci, rci_low, rci_high, t, ws, direction, depth_min, turn_min) -> bool:
    """トゲ反転ゲートを満たすか（bar_signals の従来判定と同一。dual OR 合成でも再利用）。

    窓 [ws, t] のうち t-1 までは確定 close RCI、当該バー t は足内 low/high 差し替え RCI をトラフ/ピーク
    候補にする。ロングは V 底（rci_low）、ショートは逆 V 天井（rci_high）。depth/turn の 0 は各々無効。
    """
    prior = [float(rci[j]) for j in range(ws, t) if np.isfinite(rci[j])]
    if direction == "long":
        w = prior + [float(rci_low[t])]
        depth_x, turn_x, _, _ = spike_reversal_features(w, float(rci[t]))
    else:  # short
        w = prior + [float(rci_high[t])]
        _, _, depth_x, turn_x = spike_reversal_features(w, float(rci[t]))
    return not ((depth_min > 0.0 and depth_x < depth_min) or (turn_min > 0.0 and turn_x < turn_min))


def _spike_entry(arr, t, ws, direction, depth_min, turn_min, early):
    """トゲトリガーのエントリーを決める（早期化 2026-07-21）。

    早期化（early=True）は「実際に目で RCI のトゲを見てエントリーする」の形式化＝以下の優先順で最も早い
    発火を返す:
      1. **寄り値リカバリ**: バー t の**始値時点**で、直前バーまでの窓 + rci_at_open[t] が depth/turn を満たせば
         open[t] でエントリー（前バー引け〜寄りのギャップで戻りが確定した V を 1 本早く取る）。
      2. **標準終値**（従来の _spike_pass 相当）: 当該バーの足内 low/high を谷/山候補にした確定 close 判定で
         depth かつ turn を満たせば close[t] でエントリー。
      3. **ピン足即断**: depth を満たし、かつ**足内で価格が反転した形**（ロング＝深い下ヒゲを付けて高値引け・
         陽線 or 小実体）なら、turn が半分（SPIKE_PIN_TURN_FRAC）でも close[t] でエントリー（谷バー当日に取る）。

    early=False は 2 のみ（＝従来の終値トゲと完全一致）。戻り値は (entry_kind, entry_price, first_close_idx) か None。
      - 'open':  entry=open[t]、評価起点 first_close_idx=t   （+1本=close[t]・+2本=close[t+1]）
      - 'close': entry=close[t]、評価起点 first_close_idx=t+1（+1本=close[t+1]。従来と同じ）
    """
    rci = arr["rci"]
    rci_low = arr["rci_low"]
    rci_high = arr["rci_high"]
    rci_open = arr["rci_open"]
    c = arr["c"]
    o = arr["o"]
    h = arr["h"]
    l = arr["l"]

    # 鮮度ガード（early のみ）: RCI9 直近 SPIKE_LAG_WIN 本の極値（トゲの根元）から SPIKE_MAX_LAG 本を超えて
    # 離れたバーでは発火しない。谷から遠い所（クールダウン明け等）で入る「遅い」エントリーを排除する。
    if early and SPIKE_MAX_LAG >= 0:
        s0 = max(0, t - SPIKE_LAG_WIN + 1)
        win = rci[s0:t + 1]
        if np.any(np.isfinite(win)):
            ext = s0 + (int(np.nanargmin(win)) if direction == "long" else int(np.nanargmax(win)))
            if t - ext > SPIKE_MAX_LAG:
                return None

    # 1) 寄り値リカバリ（early のみ）。始値時点で分かる情報だけ（直前バーまでの確定 close ＋ open[t]）で判定。
    if early and rci_open is not None and t >= 1 and np.isfinite(rci_open[t]):
        prior = [float(rci[j]) for j in range(ws, t) if np.isfinite(rci[j])]
        if direction == "long":
            wprev = prior + ([float(rci_low[t - 1])] if np.isfinite(rci_low[t - 1]) else [])
            d_x, tn_x, _, _ = spike_reversal_features(wprev, float(rci_open[t]))
        else:
            wprev = prior + ([float(rci_high[t - 1])] if np.isfinite(rci_high[t - 1]) else [])
            _, _, d_x, tn_x = spike_reversal_features(wprev, float(rci_open[t]))
        if (depth_min <= 0.0 or d_x >= depth_min) and (turn_min <= 0.0 or tn_x >= turn_min):
            return ("open", float(o[t]), t)

    # 2) 標準終値（従来の close 判定）。当該バー足内 low/high を谷/山候補に。
    prior_c = [float(rci[j]) for j in range(ws, t) if np.isfinite(rci[j])]
    if direction == "long":
        wc = prior_c + [float(rci_low[t])]
        d_c, tn_c, _, _ = spike_reversal_features(wc, float(rci[t]))
    else:
        wc = prior_c + [float(rci_high[t])]
        _, _, d_c, tn_c = spike_reversal_features(wc, float(rci[t]))
    depth_ok = depth_min <= 0.0 or d_c >= depth_min
    if depth_ok and (turn_min <= 0.0 or tn_c >= turn_min):
        return ("close", float(c[t]), t + 1)

    # 3) 早期切り返し（early のみ）: depth 満タン＋ turn 半分（SPIKE_PIN_TURN_FRAC）で、**足内反転形（ピン足）**
    #    または**当該バーで RCI が上向きに切り返した**（ロング rci[t]>rci[t-1] / ショート rci[t]<rci[t-1]）なら
    #    谷バー当日〜谷+1本で取る。turn 満タン（25）は谷の 1〜2 本後にしか満たされず遅れるので、depth が
    #    十分深い（＝本物のトゲ）ときだけ turn を半分に緩めて「トゲの根元」で入る（人が目で見て入る形の形式化）。
    if early and depth_ok:
        rng = h[t] - l[t]
        turn_half = turn_min <= 0.0 or tn_c >= turn_min * SPIKE_PIN_TURN_FRAC
        rci_prev = float(rci[t - 1]) if t >= 1 and np.isfinite(rci[t - 1]) else None
        if rng > 0.0 and turn_half:
            body = abs(c[t] - o[t]) / rng
            if direction == "long":
                pin = (c[t] - l[t]) / rng >= SPIKE_PIN_BODY_FRAC and (c[t] >= o[t] or body <= 0.3)
                turned = rci_prev is not None and float(rci[t]) > rci_prev
            else:
                pin = (h[t] - c[t]) / rng >= SPIKE_PIN_BODY_FRAC and (c[t] <= o[t] or body <= 0.3)
                turned = rci_prev is not None and float(rci[t]) < rci_prev
            if pin or turned:
                return ("close", float(c[t]), t + 1)
    return None


def _dual_trend_pass(rci_short, rci_long, t, window, direction, slope_min) -> bool:
    """第2トリガー「RCI 同調」: 短期・長期 RCI が窓 window 本で揃って傾くか。

    ロングは両系列の window 間Δ（値[t]−値[t-window+1]）が ≥ slope_min（同時上昇）、ショートは
    ≤ −slope_min（同時下降）。窓が足りない/NaN は False。slope_min<=0 は呼び手側で無効化する前提。
    """
    s = t - window + 1
    if s < 0:
        return False
    a0, a1 = rci_short[s], rci_short[t]
    b0, b1 = rci_long[s], rci_long[t]
    if not (np.isfinite(a0) and np.isfinite(a1) and np.isfinite(b0) and np.isfinite(b1)):
        return False
    if direction == "long":
        return (a1 - a0) >= slope_min and (b1 - b0) >= slope_min
    return (a0 - a1) >= slope_min and (b0 - b1) >= slope_min


def _empty_entries(eval_bars) -> pd.DataFrame:
    return pd.DataFrame(columns=bar_entry_columns(eval_bars))


def bar_signals(
    bars: pd.DataFrame,
    params: Optional[dict] = None,
    code: str = "?",
    cost_bp: float = 0.0,
    eval_bars=DEFAULT_EVAL_BARS,
) -> pd.DataFrame:
    """確定 1 分足 DataFrame（bars1m 契約スキーマ）にバー近似シグナルを適用する。

    bars は time 昇順・単一セッション想定（overnight ギャップ混入を避けるため、複数日を
    またぐ場合は呼び手が日次に分割してから渡す）。戻り値は 1 行 = 1 シグナルの entries。
    """
    p = merge_bar_params(params)
    eval_bars = tuple(int(k) for k in eval_bars)
    if bars is None or len(bars) == 0:
        return _empty_entries(eval_bars)
    b = bars.sort_values("time").reset_index(drop=True)
    spike_enabled = float(p.get("spike_depth_min", 0.0)) > 0.0 or float(p.get("spike_turn_min", 0.0)) > 0.0
    dual_enabled = float(p.get("dual_rci_slope_min", 0.0)) > 0.0
    arr = _bar_arrays(b, p, spike_enabled, dual_enabled)
    return _bar_signals_from_arrays(arr, p, code, cost_bp, eval_bars)


def _bar_arrays(b: pd.DataFrame, p: dict, need_low_high: bool, need_long: bool) -> dict:
    """確定足 DataFrame（time 昇順）から必要な指標配列をまとめて計算する。

    autotune は need_low_high/need_long を True にして「全指標を 1 回だけ」計算し、多数の組で
    使い回す（トゲの足内 low/high RCI・長期 RCI は rci_period/atr_period/af が共通なら組に依らず同じ）。
    """
    time = b["time"].to_numpy()
    o = b["open"].to_numpy(dtype=np.float64)
    h = b["high"].to_numpy(dtype=np.float64)
    l = b["low"].to_numpy(dtype=np.float64)
    c = b["close"].to_numpy(dtype=np.float64)
    rci = rci_series(c, int(p["rci_period"]))
    sar, trend = sar_series(h, l, c, p["af_init"], p["af_step"], p["af_max"])
    atr = atr_series(h, l, c, int(p["atr_period"]))
    rci_low = rci_high = rci_open = None
    if need_low_high:
        rci_low, rci_high = _rci_low_high(c, l, h, int(p["rci_period"]))
        rci_open = _rci_at_open(c, o, int(p["rci_period"]))  # 早期化（寄り値リカバリ）用
    rci_long = rci_series(c, RCI_LONG_PERIOD) if need_long else None
    # 1 分リターン（σフェード用。パラメータ非依存なので常に計算＝autotune でも全組で使い回せる）。
    ret = np.full(len(c), np.nan, dtype=np.float64)
    if len(c) >= 2:
        ret[1:] = (c[1:] - c[:-1]) / c[:-1]
    return {"time": time, "o": o, "h": h, "l": l, "c": c, "rci": rci, "sar": sar, "trend": trend,
            "atr": atr, "rci_low": rci_low, "rci_high": rci_high, "rci_open": rci_open,
            "rci_long": rci_long, "ret": ret}


def precompute_bar_arrays(bars: pd.DataFrame, params: Optional[dict] = None) -> Optional[dict]:
    """autotune 用: 全指標（トゲ足内 low/high・長期 RCI 含む）を 1 回だけ計算して返す。空なら None。"""
    p = merge_bar_params(params)
    if bars is None or len(bars) == 0:
        return None
    b = bars.sort_values("time").reset_index(drop=True)
    return _bar_arrays(b, p, True, True)


def bar_signals_from_arrays(arr: Optional[dict], params: Optional[dict], code: str = "?",
                            cost_bp: float = 0.0, eval_bars=DEFAULT_EVAL_BARS) -> pd.DataFrame:
    """precompute_bar_arrays の結果に組（params）を適用して entries を返す（bar_signals と同一挙動）。"""
    eval_bars = tuple(int(k) for k in eval_bars)
    if arr is None:
        return _empty_entries(eval_bars)
    return _bar_signals_from_arrays(arr, merge_bar_params(params), code, cost_bp, eval_bars)


def _bar_signals_from_arrays(arr: dict, p: dict, code: str, cost_bp: float, eval_bars) -> pd.DataFrame:
    """指標配列＋パラメータからゲートを適用して entries を返す（bar_signals の本体）。"""
    time = arr["time"]
    o = arr["o"]
    h = arr["h"]
    l = arr["l"]
    c = arr["c"]
    rci = arr["rci"]
    sar = arr["sar"]
    trend = arr["trend"]
    atr = arr["atr"]
    rci_low = arr["rci_low"]
    rci_high = arr["rci_high"]
    rci_long = arr["rci_long"]
    ret = arr["ret"]
    n = len(c)

    slope = float(p["slope_threshold"])
    prev_diff_min = float(p.get("prev_diff_min", 0.0))
    rci_sign_gate = int(p.get("rci_sign_gate", 0))
    rci_sign_level = float(p.get("rci_sign_level", 0.0))
    dual_rci_slope_min = float(p.get("dual_rci_slope_min", 0.0))
    spike_depth_min = float(p.get("spike_depth_min", 0.0))
    spike_turn_min = float(p.get("spike_turn_min", 0.0))
    spike_early = int(p.get("spike_early", 1)) != 0
    spike_window_bars = int(p.get("spike_window_bars", 6))
    spike_enabled = spike_depth_min > 0.0 or spike_turn_min > 0.0
    dual_enabled = dual_rci_slope_min > 0.0
    eff_min = float(p["eff_min"])
    floor = float(p["floor"])
    cooldown = int(p["cooldown_bars"])
    skip_open_bars = int(p.get("skip_open_bars", 0))
    sar_filter = int(p.get("sar_filter", 1))
    fade_sigma_k = float(p.get("fade_sigma_k", 0.0))
    fade_sigma_window = int(p.get("fade_sigma_window", 60))
    fade_enabled = fade_sigma_k > 0.0 and fade_sigma_window >= 2
    fade_boll_z = float(p.get("fade_boll_z", 0.0))
    fade_boll_window = int(p.get("fade_boll_window", 20))
    fade_boll_enabled = fade_boll_z > 0.0 and fade_boll_window >= 2
    max_eval = max(eval_bars)

    # σフェード用の直近 σ（当該バーを含まない手前 fade_sigma_window 本のリターン標準偏差・ddof=0）。
    # rolling(window) は [t-w+1, t] の σ を返すので shift(1) して [t-w, t-1] にずらす＝当該急変を除いた地の
    # ボラティリティで測る（急変自身が σ を膨らませて発火を鈍らせるのを避ける）。fade 無効時は計算しない。
    fade_sigma = None
    if fade_enabled:
        fade_sigma = (pd.Series(ret).rolling(fade_sigma_window).std(ddof=0).shift(1).to_numpy())

    # ボリンジャーフェード用の z=(close−SMA)/std（標準ボリンジャー＝当該バーを含む rolling(window)。ddof=0）。
    # fade_boll 無効時は計算しない。std=0 の平坦区間は z を出さない（下の発火判定でガード）。
    fade_boll_zscore = None
    if fade_boll_enabled:
        cser = pd.Series(c)
        boll_sma = cser.rolling(fade_boll_window).mean()
        boll_std = cser.rolling(fade_boll_window).std(ddof=0)
        fade_boll_zscore = ((cser - boll_sma) / boll_std).to_numpy()

    # 寄り抑制は前場フレームだけに適用（後場先頭 12:30 は正午以降なので適用外）。呼び手はセッション半で渡す。
    is_morning = bool(skip_open_bars > 0 and n > 0 and pd.Timestamp(time[0]).hour < 12)

    last_sig = {"long": -(10 ** 9), "short": -(10 ** 9)}
    rows: list[dict] = []

    def _emit(t, direction, trigger, drci1, drci2, prev_diff, efficiency,
              atr_pct, bar_range_pct, norm_range,
              entry_price=None, entry_kind="close", first_close_idx=None):
        """1 発火を entries 行に整形して rows へ積む（spike/dual/angle・fade で共通）。

        エントリーは entry_kind により
          - 'close'（既定）: entry=close[t]、first_close_idx=t+1（+1本=close[t+1]。従来と同一）
          - 'open'（トゲ早期化の寄り値リカバリ）: entry=open[t]、first_close_idx=t（+1本=close[t]）
        評価は「エントリー後 n 番目に確定する終値」で統一する（first_close_idx を起点に +k−1 本後）。
        """
        entry = float(c[t]) if entry_price is None else float(entry_price)
        base = (t + 1) if first_close_idx is None else int(first_close_idx)
        sign = 1.0 if direction == "long" else -1.0
        row = {
            "time": pd.Timestamp(time[t]),
            "code": code,
            "direction": direction,
            "trigger": trigger,
            "entry_kind": entry_kind,
            "signal_close": float(c[t]),
            "entry_price": entry,
            "rci": float(rci[t]),
            "drci1": drci1,
            "drci2": drci2,
            "prev_diff": prev_diff,
            "efficiency": float(efficiency),
            "sar": float(sar[t]),
            "trend": int(trend[t]),
            "atr": float(atr[t]),
            "atr_pct": atr_pct,
            "bar_range_pct": bar_range_pct,
            "norm_range": float(norm_range),
            "cost_bp": float(cost_bp),
            "eval_truncated": bool(base + (max_eval - 1) >= n),
        }
        for k in eval_bars:
            ei = base + (k - 1)  # エントリー確定後 +k 本目に確定する終値 close[ei] との順方向リターン
            if ei < n:
                gross = sign * (float(c[ei]) - entry) / entry * 1e4
                row[f"ret_{k}bar_gross"] = gross
                row[f"ret_{k}bar_net"] = gross - float(cost_bp)
            else:
                row[f"ret_{k}bar_gross"] = float("nan")
                row[f"ret_{k}bar_net"] = float("nan")
        rows.append(row)

    for t in range(n):
        # drci2 のため t-2 まで、次バー始値エントリーのため t+1 まで必要
        if t < 2 or t + 1 >= n:
            continue
        if not (np.isfinite(rci[t]) and np.isfinite(rci[t - 1]) and np.isfinite(rci[t - 2])):
            continue
        if not np.isfinite(sar[t]) or not np.isfinite(atr[t]) or atr[t] <= 0.0:
            continue

        # 寄り抑制（シグナル抑制のみ・評価は不変）。cooldown 前に弾くので last_sig は更新しない。
        if is_morning and t < skip_open_bars:
            continue

        # 方向候補。sar_filter=1 は従来どおり SAR 絶対条件で 1 方向に絞る。0 は SAR で絞らず
        # ロング・ショート両方をトリガー側の判定に委ねる（角度/符号/トゲ/dual が方向を決める）。
        if sar_filter:
            if c[t] > sar[t]:
                dir_list = ("long",)
            elif c[t] < sar[t]:
                dir_list = ("short",)
            else:
                dir_list = ()
        else:
            dir_list = ("long", "short")

        drci1 = float(rci[t] - rci[t - 1])
        drci2 = float(rci[t] - rci[t - 2])
        prev_diff = drci1  # 直前の確定 RCI との差（バー粒度の「尖り」）
        ws = max(0, t - spike_window_bars + 1)
        # 効率・レンジ系はバー単位（方向非依存）。fade も同じ行スキーマで記録するため先に一度だけ計算する。
        atr_pct = float(atr[t] / c[t])
        bar_range_pct = float((h[t] - l[t]) / c[t])
        norm_range = bar_range_pct / atr_pct
        efficiency = abs(drci1) / max(norm_range, floor)

        for direction in dir_list:
            # RCI 符号ゲート（rci_sign_gate!=0 で有効）。境界は ±rci_sign_level（level=0 は従来の 0 境界と一致）。
            # 当該バー確定 RCI が > +level はショートのみ・< −level はロングのみ許可。rci[t] は finite 保証済み。
            if rci_sign_gate != 0:
                if not ((rci[t] > rci_sign_level and direction == "short") or
                        (rci[t] < -rci_sign_level and direction == "long")):
                    continue

            # トリガー判定。dual（RCI 同調）が有効なら「トゲ OR dual」の OR 合成（角度・直前差は課さず、トリガー
            # 自体を発火根拠にする。SAR・効率・クールダウンは従来 AND）。dual 無効なら従来どおり角度 AND 直前差
            # AND トゲ を順に課す（dual 無効では新パラメータ既定＝spike_early=1 の早期化のみが従来からの差分）。
            # トゲは _spike_entry が寄り値リカバリ→標準終値→ピン足即断の優先で最も早い entry_kind/price/評価起点を返す。
            if dual_enabled:
                se = _spike_entry(arr, t, ws, direction, spike_depth_min, spike_turn_min,
                                  spike_early) if spike_enabled else None
                passed_dual = _dual_trend_pass(
                    rci, rci_long, t, spike_window_bars, direction, dual_rci_slope_min)
                if se is None and not passed_dual:
                    continue
                if se is not None:
                    trigger = "spike"
                    entry_kind, entry_price, first_close_idx = se
                else:
                    trigger, entry_kind, entry_price, first_close_idx = "dual", "close", float(c[t]), t + 1
            else:
                # 角度ゲート（符号一致 + 閾値）。slope <= 0 は無効（prev_diff_min/eff_min と同じ 0=無効 規約。
                # UI で「角度」トグルを OFF にすると slope=0 が送られ、このゲートを丸ごとスキップする）。
                if slope > 0.0:
                    if direction == "long" and drci1 < slope:
                        continue
                    if direction == "short" and drci1 > -slope:
                        continue
                # 直前差ゲート（尖り。prev_diff_min <= 0 で無効）
                if prev_diff_min > 0.0:
                    if direction == "long" and prev_diff < prev_diff_min:
                        continue
                    if direction == "short" and prev_diff > -prev_diff_min:
                        continue
                # トゲ反転ゲート（足内近似・spike_depth_min/spike_turn_min ともに 0 で無効）。早期化（spike_early=1・
                # 既定）は寄り値リカバリ（open[t] で反転検出）とピン足即断（谷バー当日）で 1〜2 本の遅延を解消する。
                # spike_early=0 は従来の終値のみ（完全後方互換）。
                if spike_enabled:
                    se = _spike_entry(arr, t, ws, direction, spike_depth_min, spike_turn_min, spike_early)
                    if se is None:
                        continue
                    trigger = "spike"
                    entry_kind, entry_price, first_close_idx = se
                else:
                    trigger, entry_kind, entry_price, first_close_idx = "angle", "close", float(c[t]), t + 1

            # 効率フィルタ
            if efficiency < eff_min:
                continue

            # クールダウン（同一方向）
            if t - last_sig[direction] < cooldown:
                continue
            last_sig[direction] = t
            _emit(t, direction, trigger, drci1, drci2, prev_diff, efficiency,
                  atr_pct, bar_range_pct, norm_range,
                  entry_price=entry_price, entry_kind=entry_kind, first_close_idx=first_close_idx)

        # 第3トリガー「σフェード」（急変逆張り・SAR非依存・既存トリガーと OR）。SAR 方向/角度/効率は課さず、
        # リターンが直近 σ の k 倍を超えたら逆方向へ。クールダウンと寄り抑制（ループ先頭）は共通適用する。
        # dir_list が同方向を先に埋めていれば cooldown で自然にスキップされるため二重発火しない。
        if fade_enabled:
            sig = fade_sigma[t]
            if np.isfinite(sig) and sig > 0.0 and np.isfinite(ret[t]) and abs(ret[t]) > fade_sigma_k * sig:
                fdir = "short" if ret[t] > 0.0 else "long"
                if t - last_sig[fdir] >= cooldown:
                    last_sig[fdir] = t
                    _emit(t, fdir, "fade", drci1, drci2, prev_diff, efficiency,
                          atr_pct, bar_range_pct, norm_range)

        # 第4トリガー「ボリンジャーフェード」（σフェードと同じ合成規約＝SAR非依存・逆張り・OR・cooldown/寄り抑制のみ共通）。
        # z=(close−SMA20)/std20 が ±閾値を超えたら逆方向へ。σフェードが同方向を先に埋めていれば cooldown で自然スキップ。
        if fade_boll_enabled:
            z = fade_boll_zscore[t]
            if np.isfinite(z) and abs(z) >= fade_boll_z:
                bdir = "short" if z > 0.0 else "long"
                if t - last_sig[bdir] >= cooldown:
                    last_sig[bdir] = t
                    _emit(t, bdir, "fadeb", drci1, drci2, prev_diff, efficiency,
                          atr_pct, bar_range_pct, norm_range)

    if not rows:
        return _empty_entries(eval_bars)
    return pd.DataFrame(rows, columns=bar_entry_columns(eval_bars))


def bar_attention(
    bars: pd.DataFrame,
    params: Optional[dict] = None,
    code: str = "?",
) -> pd.DataFrame:
    """注目（事前アラート）シグナルを返す。**エントリーとは完全に独立**（bar_signals には一切触れない）。

    目的（要件 2026-07-19）: 「このシグナルが出ると高確率でエントリー条件が揃いそう」な時に
    早く気づけるようにする前段アラート。エントリーの数値・件数には一切影響しない（別関数・別配列）。

    定義 v1（トゲ条件の前段状態）: バー t で SAR 方向側のトゲ落差 depth が spike_depth_min の
    ATTENTION_DEPTH_FRAC（70%）以上に達しているが、戻り turn が spike_turn_min 未満（V の切り返し未確認）
    のとき「注目」を出す。方向つき: 下向きトゲ形成中（トラフ）＝ロング注目 / 上向き（ピーク）＝ショート注目。
    SAR 方向と矛盾する側は出さない（direction は entries と同じく SAR で決める）。トラフ/ピーク候補は
    entries と同じ足内 low/high 差し替え RCI（rci_at_low / rci_at_high）で早期に取る。

    同一フォーメーション内は初回のみ: 一度出したら armed にし、(a) 戻り成立＝エントリー化
    （depth≥spike_depth_min かつ turn≥spike_turn_min）または (b) 落差が ATTENTION_RESET_FRAC（50%）未満に
    戻ったら armed を解除して次のフォーメーションに備える。

    spike_depth_min<=0（トゲ無効）や spike_turn_min<=0（戻り閾値なし＝「未確認」状態が定義できない）の
    ときは注目を出さない（空 DataFrame）。戻り値列は ATTENTION_COLUMNS。
    """
    p = merge_bar_params(params)
    if bars is None or len(bars) == 0:
        return pd.DataFrame(columns=ATTENTION_COLUMNS)

    spike_depth_min = float(p.get("spike_depth_min", 0.0))
    spike_turn_min = float(p.get("spike_turn_min", 0.0))
    if spike_depth_min <= 0.0 or spike_turn_min <= 0.0:
        return pd.DataFrame(columns=ATTENTION_COLUMNS)

    b = bars.sort_values("time").reset_index(drop=True)
    time = b["time"].to_numpy()
    h = b["high"].to_numpy(dtype=np.float64)
    l = b["low"].to_numpy(dtype=np.float64)
    c = b["close"].to_numpy(dtype=np.float64)
    n = len(b)

    rci = rci_series(c, int(p["rci_period"]))
    sar, _trend = sar_series(h, l, c, p["af_init"], p["af_step"], p["af_max"])
    rci_low, rci_high = _rci_low_high(c, l, h, int(p["rci_period"]))
    spike_window_bars = int(p.get("spike_window_bars", 6))

    depth_hi = ATTENTION_DEPTH_FRAC * spike_depth_min
    depth_reset = ATTENTION_RESET_FRAC * spike_depth_min
    armed = {"long": False, "short": False}
    rows: list[dict] = []

    for t in range(n):
        if not np.isfinite(rci[t]) or not np.isfinite(sar[t]):
            continue
        # 方向は entries と同じ SAR 絶対条件（矛盾する側は出さない）。
        if c[t] > sar[t]:
            direction = "long"
        elif c[t] < sar[t]:
            direction = "short"
        else:
            continue

        ws = max(0, t - spike_window_bars + 1)
        prior = [float(rci[j]) for j in range(ws, t) if np.isfinite(rci[j])]
        if direction == "long":
            if not np.isfinite(rci_low[t]):
                continue
            w = prior + [float(rci_low[t])]
            depth_x, turn_x, _, _ = spike_reversal_features(w, float(rci[t]))
        else:
            if not np.isfinite(rci_high[t]):
                continue
            w = prior + [float(rci_high[t])]
            _, _, depth_x, turn_x = spike_reversal_features(w, float(rci[t]))

        turn_confirmed = depth_x >= spike_depth_min and turn_x >= spike_turn_min
        if depth_x < depth_reset:
            armed[direction] = False           # フォーメーション解消
        elif turn_confirmed:
            armed[direction] = False           # エントリー化（戻り成立）→ 次に備える
        elif (not armed[direction]) and depth_x >= depth_hi and turn_x < spike_turn_min:
            rows.append({
                "time": pd.Timestamp(time[t]), "code": code, "direction": direction,
                "rci": float(rci[t]), "sar": float(sar[t]),
                "depth": float(depth_x), "turn": float(turn_x),
            })
            armed[direction] = True            # 同一フォーメーション内は初回のみ

    if not rows:
        return pd.DataFrame(columns=ATTENTION_COLUMNS)
    return pd.DataFrame(rows, columns=ATTENTION_COLUMNS)
