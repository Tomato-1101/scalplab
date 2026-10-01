"""Strategy 抽象インタフェース。

なぜ2系統のコールバックか: 手法は「確定1分足で更新する指標(SAR/ATR/確定RCI)」と
「足内でティック毎に動く指標(intrabar RCI)」の両方に依存する。エンジンは
足確定時に on_bar_confirm、全ティックで on_tick を呼ぶ。スロットル等の間引きは
エンジンの責務にせず、素直に毎ティック評価する（リアルタイム検知へそのまま流用するため）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class Strategy(ABC):
    """ティックストリーム + 確定足コールバックで駆動する戦略の共通 IF。"""

    @abstractmethod
    def on_bar_confirm(self, bar: dict) -> None:
        """1分足確定時に呼ばれる。bar は少なくとも high/low/close を持つ dict。"""

    @abstractmethod
    def on_tick(self, t_ns: int, price: float) -> Optional[dict]:
        """各ティックで呼ばれる。エントリー条件成立時のみシグナル dict を返す（他は None）。

        t_ns はエポックからの経過ナノ秒（JST naive datetime を int64 化した値）。
        """

    @property
    @abstractmethod
    def params(self) -> dict:
        """現在のパラメータ（掃引・記録用）。"""
