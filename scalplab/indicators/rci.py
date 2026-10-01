"""RCI（Rank Correlation Index）。

なぜ自前実装か: 本手法のトリガーは「足内(intrabar)で刻々と動く RCI の角度」なので、
確定足のバッチ計算だけでなく、ティック毎に O(period log period) で再計算できる逐次器が要る。
scipy を持ち込まず（依存を増やさない方針）、タイ（同値）は平均順位で自前処理する。

RCI = (1 − 6Σd²/(n(n²−1))) × 100
  d = 日付順位（最新=1, 最古=n）− 価格順位（高値=1, 同値は平均順位）
  → 終値が単調上昇なら +100、単調下降なら −100。
"""
from __future__ import annotations

from collections import deque

import numpy as np


def _average_ranks_desc(values: np.ndarray) -> np.ndarray:
    """降順（最大値=1）の平均順位を返す。同値グループは平均順位を割り当てる。

    なぜ降順か: RCI の価格順位は「高値ほど小さい順位」を採るのが定義のため。
    """
    n = len(values)
    order = np.argsort(-values, kind="mergesort")  # 値の降順に並ぶ位置インデックス
    ranks = np.empty(n, dtype=np.float64)
    i = 0
    while i < n:
        j = i
        # order 上で連続する同値をひとまとめにする
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0  # 位置 i..j（1始まり順位）の平均
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _rci_window(window: np.ndarray) -> float:
    """長さ n の終値ウィンドウ（古い→新しい順）に対する RCI を返す。"""
    n = len(window)
    date_rank = np.arange(n, 0, -1, dtype=np.float64)  # 最古=n, 最新=1
    price_rank = _average_ranks_desc(window)
    d = date_rank - price_rank
    d2 = float(np.dot(d, d))
    return (1.0 - 6.0 * d2 / (n * (n * n - 1))) * 100.0


def rci_series(closes, period: int) -> np.ndarray:
    """確定足終値系列のバッチ RCI。前方 period-1 本は NaN。"""
    closes = np.asarray(closes, dtype=np.float64)
    n = len(closes)
    out = np.full(n, np.nan, dtype=np.float64)
    if period < 2 or n < period:
        return out
    for i in range(period - 1, n):
        out[i] = _rci_window(closes[i - period + 1 : i + 1])
    return out


class IntrabarRci:
    """足内 RCI 逐次器。バックテストとリアルタイム検知で同一コードを使う。

    設計: deque には「確定足の close」を最新 period-1 本だけ保持する。形成中の足の
    close は update_tick(price) の引数として都度与え、確定足 period-1 本 + 形成足 1 本の
    合計 period 本ウィンドウで RCI を計算する（形成足を最新の 1 本として扱う）。
    こうすると rci_series(np.append(確定足, price), period)[-1] と厳密に一致し、
    確定足が 1 本増えるたびの挙動がバッチ計算と突き合わせ可能になる。
    """

    def __init__(self, period: int):
        if period < 2:
            raise ValueError("period は 2 以上")
        self.period = int(period)
        # 形成足と合わせて period 本にするため確定足は period-1 本あれば足りる
        self._closes: deque[float] = deque(maxlen=self.period - 1)

    def confirm_bar(self, close: float) -> None:
        """足を確定して close を確定足列に追加する。"""
        self._closes.append(float(close))

    def update_tick(self, price: float) -> float:
        """形成中の足の close を現在ティック価格に置換した RCI を返す。

        確定足が period-1 本に満たない間は NaN（ウォームアップ）。
        """
        if len(self._closes) < self.period - 1:
            return float("nan")
        window = np.empty(self.period, dtype=np.float64)
        window[:-1] = np.fromiter(self._closes, dtype=np.float64, count=self.period - 1)
        window[-1] = float(price)
        return _rci_window(window)


def intrabar_rci_batch(base_closes, prices, period: int) -> np.ndarray:
    """確定足 period-1 本(base_closes, 古→新)を固定し、各 price を最新足として挿入した
    intrabar RCI をまとめて返すベクトル版。

    なぜ必要か: バックテストは全ティックで IntrabarRci.update_tick を呼ぶが、ティック毎に
    argsort する逐次実装が支配的コストだった。同一の確定足ウィンドウ(base_closes)に対して
    多数の price を評価する場面（＝1本の形成足に属する全ティック）では、順位を閉形式で
    一括算出できる。降順平均順位は「その値より大きい個数 g」と「等しい個数 e」から
    rank = g + (e+1)/2 で厳密に求まる（_average_ranks_desc と一致。タイは平均順位）。
    したがって _rci_window(np.append(base_closes, p))[-1] と数値が厳密一致する。
    """
    b = np.asarray(base_closes, dtype=np.float64)
    P = np.asarray(prices, dtype=np.float64)
    m = period - 1
    if len(b) != m:
        raise ValueError("base_closes は period-1 本である必要がある")
    # base 内順位（price 非依存）: base_g[j]=#{base_l>b_j}, base_e[j]=#{base_l==b_j}(自身含む)
    base_g = (b[:, None] > b[None, :]).sum(axis=0).astype(np.float64)
    base_e = (b[:, None] == b[None, :]).sum(axis=0).astype(np.float64)
    # price と base の大小（G×m）
    p_gt_b = (P[:, None] > b[None, :]).astype(np.float64)   # [p>b_j]
    p_eq_b = (P[:, None] == b[None, :]).astype(np.float64)   # [p==b_j]
    p_lt_b = (P[:, None] < b[None, :]).astype(np.float64)    # [p<b_j]=[b_j>p]
    num_base_gt_p = p_lt_b.sum(axis=1)   # #{b_j>p}
    num_base_eq_p = p_eq_b.sum(axis=1)   # #{b_j==p}
    # 最新足(price)の降順平均順位と date_rank(=1)との差
    r_price_p = num_base_gt_p + num_base_eq_p / 2.0 + 1.0
    d_p = 1.0 - r_price_p
    # base 各要素の降順平均順位（price 挿入後の全ウィンドウ基準）と date_rank との差
    r_price_base = base_g[None, :] + p_gt_b + (base_e[None, :] + p_eq_b + 1.0) / 2.0
    date_rank_base = (period - np.arange(m)).astype(np.float64)  # b_0→period, …, b_{m-1}→2
    d_base = date_rank_base[None, :] - r_price_base
    sum_d2 = d_p ** 2 + (d_base ** 2).sum(axis=1)
    denom = period * (period * period - 1)
    return (1.0 - 6.0 * sum_d2 / denom) * 100.0
