"""moomoo OpenD (futu-api) の薄いラッパ。

設計方針（tradelog の教訓）:
- OpenD 非起動時に futu の同期 API がハングするのを避けるため、OpenQuoteContext を作る前に
  必ず socket 接続で死活確認する（タイムアウト付き）。不達なら即 OpenDUnavailableError。
- 日本株は現状「No permission (Stocks quote permissions)」で相場 API が拒否される。これを
  正常系として区別できるよう NoPermissionError に分類する（権限が付き次第そのまま動く）。
- 切断時は指数バックオフで再接続し、登録済みハンドラと購読を復元する（reconnect）。
"""
from __future__ import annotations

import logging
import os
import socket
import time
from typing import Any

from futu import (
    OpenQuoteContext,
    RET_OK,
    SecurityType,
)

log = logging.getLogger(__name__)

OPEND_HOST = os.environ.get("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.environ.get("OPEND_PORT", "11111"))


class OpenDUnavailableError(RuntimeError):
    """OpenD に socket 到達できない（未起動・ポート違い等）。"""


class NoPermissionError(RuntimeError):
    """相場権限が無い（日本株の "No permission" 等）。他銘柄は続行できるよう区別する。"""


def _is_no_permission(msg: Any) -> bool:
    """moomoo のエラー文字列が権限不足を示すか判定する。"""
    return "no permission" in str(msg).lower()


def check_opend_reachable(host: str = OPEND_HOST, port: int = OPEND_PORT, timeout: float = 3.0) -> None:
    """OpenD への TCP 到達性を確認する。不達なら OpenDUnavailableError を投げる。

    futu の同期 API を触る前に呼ぶことで「OpenD 未起動でハング」を防ぐ。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except OSError as e:
        raise OpenDUnavailableError(
            f"OpenD へ接続できません（{host}:{port}, timeout={timeout}s）: {e}. "
            "moomoo_OpenD.app が起動・ログイン済みか確認してください。"
        ) from e
    finally:
        sock.close()


class MoomooClient:
    """OpenQuoteContext のライフサイクル管理と薄い包み。

    生成しただけでは接続しない（connect() で socket 事前チェック→ctx 生成）。
    set_handler / subscribe は再接続時に復元できるよう内部に記録しておく。
    """

    def __init__(self, host: str = OPEND_HOST, port: int = OPEND_PORT) -> None:
        self.host = host
        self.port = port
        self._ctx: OpenQuoteContext | None = None
        self._handlers: list[Any] = []
        # 再購読復元用: [(code_list, subtype_list), ...]
        self._subs: list[tuple[list[str], list[str]]] = []

    # --- 接続管理 ---

    @property
    def ctx(self) -> OpenQuoteContext:
        if self._ctx is None:
            raise RuntimeError("未接続です。connect() を先に呼んでください。")
        return self._ctx

    def connect(self) -> None:
        """socket 事前チェック → OpenQuoteContext 生成。"""
        check_opend_reachable(self.host, self.port)
        self._ctx = OpenQuoteContext(host=self.host, port=self.port)
        log.info("OpenD 接続確立: %s:%s", self.host, self.port)

    def close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            finally:
                self._ctx = None

    def reconnect(self, max_tries: int = 6, base_delay: float = 1.0) -> bool:
        """指数バックオフで再接続し、ハンドラと購読を復元する。成功で True。

        切断検知（recorder の watchdog）から呼ばれる。socket 事前チェックを毎回挟むので
        OpenD が落ちている間は無駄にハングしない。
        """
        for attempt in range(max_tries):
            delay = base_delay * (2 ** attempt)
            try:
                self.close()
                check_opend_reachable(self.host, self.port)
                self._ctx = OpenQuoteContext(host=self.host, port=self.port)
                for h in self._handlers:
                    self._ctx.set_handler(h)
                for code_list, subtype_list in self._subs:
                    ret, msg = self._ctx.subscribe(code_list, subtype_list)
                    if ret != RET_OK:
                        log.warning("再購読失敗 %s %s: %s", code_list, subtype_list, msg)
                log.info("再接続成功（試行 %d 回目）", attempt + 1)
                return True
            except Exception as e:  # 接続不能はバックオフして再試行
                log.warning("再接続失敗（%d/%d）: %s。%.1fs 待機", attempt + 1, max_tries, e, delay)
                time.sleep(delay)
        log.error("再接続を諦めました（%d 回失敗）", max_tries)
        return False

    # --- 薄い包み ---

    def get_global_state(self) -> dict[str, Any]:
        ret, data = self.ctx.get_global_state()
        if ret != RET_OK:
            raise RuntimeError(f"get_global_state 失敗: {data}")
        return data

    def query_subscription(self) -> dict[str, Any]:
        ret, data = self.ctx.query_subscription()
        if ret != RET_OK:
            raise RuntimeError(f"query_subscription 失敗: {data}")
        return data

    def get_stock_basicinfo(self, market: Any, stock_type: Any = SecurityType.STOCK):
        """銘柄マスタを返す（pandas.DataFrame）。権限不足は NoPermissionError。"""
        ret, data = self.ctx.get_stock_basicinfo(market, stock_type)
        if ret != RET_OK:
            if _is_no_permission(data):
                raise NoPermissionError(f"get_stock_basicinfo: {data}")
            raise RuntimeError(f"get_stock_basicinfo 失敗: {data}")
        return data

    def get_market_snapshot(self, code_list: list[str]):
        """スナップショットを返す。権限不足（日本株など）は NoPermissionError で区別。"""
        ret, data = self.ctx.get_market_snapshot(code_list)
        if ret != RET_OK:
            if _is_no_permission(data):
                raise NoPermissionError(f"get_market_snapshot: {data}")
            raise RuntimeError(f"get_market_snapshot 失敗: {data}")
        return data

    def request_history_kline(
        self,
        code: str,
        start: str,
        end: str,
        ktype: Any,
        autype: Any,
        max_count: int = 1000,
        page_req_key: Any = None,
        extended_time: bool = False,
    ) -> tuple[Any, Any]:
        """過去 K ラインを 1 ページ取得する。戻り値 (data, next_page_req_key)。

        futu の request_history_kline は (ret, data, page_req_key) を返す。next_page_req_key が
        None になるまで呼び手がページングする。権限不足は NoPermissionError で区別する
        （日本株など。他銘柄の続行判断を呼び手に委ねる）。
        """
        ret, data, next_key = self.ctx.request_history_kline(
            code,
            start=start,
            end=end,
            ktype=ktype,
            autype=autype,
            max_count=max_count,
            page_req_key=page_req_key,
            extended_time=extended_time,
        )
        if ret != RET_OK:
            if _is_no_permission(data):
                raise NoPermissionError(f"request_history_kline {code}: {data}")
            raise RuntimeError(f"request_history_kline 失敗 {code}: {data}")
        return data, next_key

    def set_handler(self, handler: Any) -> None:
        """push ハンドラを登録（再接続時に復元できるよう記録も残す）。"""
        self._handlers.append(handler)
        self.ctx.set_handler(handler)

    def subscribe(self, code_list: list[str], subtype_list: list[str]) -> None:
        """購読する。権限不足は NoPermissionError で区別し、記録して再接続時に復元する。"""
        ret, msg = self.ctx.subscribe(code_list, subtype_list)
        if ret != RET_OK:
            if _is_no_permission(msg):
                raise NoPermissionError(f"subscribe {code_list} {subtype_list}: {msg}")
            raise RuntimeError(f"subscribe 失敗 {code_list} {subtype_list}: {msg}")
        self._subs.append((list(code_list), list(subtype_list)))

    def unsubscribe_all(self) -> None:
        """全購読を解除する（セッション終了時）。"""
        try:
            self.ctx.unsubscribe_all()
        except Exception as e:  # 解除失敗しても close で片付くので落とさない
            log.warning("unsubscribe_all 失敗: %s", e)
        finally:
            self._subs.clear()
