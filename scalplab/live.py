"""リアルタイムシグナル通知デーモン（`python -m scalplab.live`。Phase 5 MVP）。

録画デーモン(record.py)と同じ moomoo OpenD 経由で TICKER を購読し、ティックごとに
ティック版戦略 RciImpulseStrategy を回して、エントリー条件が成立した瞬間に

  1) デスクトップ通知（OS 別に分岐。macOS=osascript の display notification /
     Windows=PowerShell の WinRT トースト / その他 OS=ログのみ。詳細は send_notification の docstring）
  2) logs/live-signals-YYYY-MM-DD.csv への追記
  3) 標準出力／ログへの1行出力

を行う。**検知と通知だけ**を行う（自動売買・決済・Web 連携はしない。決済は本人裁量）。

ティック供給源（証券会社）は TickSource アダプタで抽象化してある（下記）。実装済みは
  - MoomooTickSource : moomoo OpenD 経由（米国株のみ・現行の US 検証で使用）。
  - KabuStationTickSource : auカブコム kabu STATION API 経由（日本株・**Windows 常駐アプリ必須**）。
    config/live.yaml の `source: kabu` で選択。日本株はこちらを使う（データ源は kabu STATION に確定）。
立花証券 e支店（TachibanaTickSource）は将来枠（ダミー）。source に応じてセッション時間（US=NY 09:30-16:00 /
kabu=東証 09:00-11:30・12:30-15:30）も自動で切り替わる。

対象は米国株のみ。moomoo OpenD は日本株の相場情報が未対応（購読が「No permission」で拒否される）
ため JP.* は扱わない。日本株のリアルタイム化は立花証券 e支店 API（Mac 完結）導入後に、本モジュールと
同じ構造（ティック購読 → on_tick → 通知）で対応する予定。

録画デーモンとの共存: 本モジュールは record.py 本体には一切触れず、純粋な時刻・購読ヘルパ関数だけを
読み取り利用する（独立プロセス・独立の OpenQuoteContext）。同一銘柄の (code, SubType.TICKER) は
OpenD 側で購読が重複排除されるため、録画デーモンが既に購読している銘柄を本モジュールが重ねて購読しても
購読クォータを二重に消費しない（本モジュールは ORDER_BOOK を購読しない＝TICKER のみで足跡を最小化する）。
実際の used/remain は `--check` の「購読前／購読後クォータ」で確認できる。

セッション管理（手動起動のみ・launchd 化はしない）:
  - 既定: NY 09:30-16:00 の間だけ稼働する。寄り前に起動したら寄りまで待機、引け後に起動したら何もせず終了。
  - `--duration N`: 市場時間を無視して N 秒だけ稼働（市場が開いている時間帯の手動スモーク用）。
  - `--check`: OpenD 接続 → 購読 → 10 秒間の受信待ち → 統計表示 → 終了（市場外でも実行できる疎通確認）。
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import http.client
import json
import logging
import os
import platform
import queue
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import yaml

from futu import RET_OK, SubType, TickerHandlerBase

from .datafeed.moomoo_client import MoomooClient, OpenDUnavailableError
# record.py の純粋ヘルパ（時刻ロジック・購読の銘柄単位フォールバック）を読み取り利用する。
# record.py 本体は改変しない（録画側の挙動を変えない不変条件）。
from .record import (
    is_in_session,
    subscribe_all,
    us_session_day,
    us_session_segments,
)
from .strategy.rci_impulse import RciImpulseStrategy, merge_params

log = logging.getLogger("scalplab.live")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "live.yaml"
BEST_PARAMS_PATH = PROJECT_ROOT / "config" / "best-params.yaml"
LOG_DIR = PROJECT_ROOT / "logs"

_MIN_NS = 60_000_000_000  # 1 分 = 60e9 ns（engine と同じ分足グルーピング単位）
NOTIFY_TITLE = "scalp-lab"
DEFAULT_WATCH = ["US.TQQQ", "US.SOXL", "US.SOXS", "US.NVDA"]


class TickSourceUnavailableError(RuntimeError):
    """ティック供給源に接続できない（証券会社アプリ未起動・認証失敗など）を表す汎用例外。

    moomoo の OpenDUnavailableError と同じ扱い（呼び手が exit code を判断する）を、証券会社非依存の
    供給源にも与えるためのもの。check_mode / run_live は両方を捕まえて終了コードを返す。
    """


# ---------------- 東証セッションの時刻ロジック（kabu 用・JST naive） ----------------

def jp_session_day(now: Optional[datetime] = None) -> str:
    """JST の当日日付（YYYY-MM-DD）をセッション日として返す（東証はマシンローカル=JST 前提）。"""
    return (now or datetime.now()).strftime("%Y-%m-%d")


def jp_session_segments(date: str) -> list[tuple[datetime, datetime]]:
    """東証立会の (開始, 終了) を前場 09:00-11:30 / 後場 12:30-15:30（JST naive）で返す。

    run_live は「最初の区間の寄りで待機・最後の区間の引けで終了」に一般化してあるため、昼休み
    （11:30-12:30）は検知が空振りするだけ（ティックが来ない）で、区間分割そのものは不要。
    """
    d = datetime.strptime(date, "%Y-%m-%d")
    return [
        (d.replace(hour=9, minute=0, second=0, microsecond=0),
         d.replace(hour=11, minute=30, second=0, microsecond=0)),
        (d.replace(hour=12, minute=30, second=0, microsecond=0),
         d.replace(hour=15, minute=30, second=0, microsecond=0)),
    ]


# ---------------- 設定・パラメータ ----------------

def load_live_config(path: Path = CONFIG_PATH) -> dict:
    """live.yaml を読む。隣に live.local.yaml があればトップレベルキー単位で上書きし、欠損キーは既定で補う。

    ローカル上書きが要る理由: Mac（moomoo/米国株）と Windows（kabu/日本株）が**同一リポジトリ**を
    共有しており、live.yaml 本体の `source` / `watch` を各マシンで書き換えると毎回コンフリクトする。
    マシン固有の設定は Git 管理外の live.local.yaml（.gitignore 済み）に置き、共有既定である
    live.yaml には手を入れない運用にする。
    """
    cfg: dict = {}
    if path.exists():
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    local_path = path.with_name("live.local.yaml")
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
        cfg.update(local)  # 浅いマージ（トップレベルキーごと差し替え）
    cfg.setdefault("watch", list(DEFAULT_WATCH))
    cfg.setdefault("params", {})
    cfg.setdefault("notify", {})
    cfg["notify"].setdefault("sound", True)
    cfg.setdefault("source", "moomoo")  # moomoo（米国株）| kabu（日本株・kabu STATION）
    return cfg


def resolve_params(cfg: dict) -> dict:
    """検知パラメータを解決する。cfg["params"] が空なら best-params.yaml のチャンピオンを使う。

    どちらも merge_params で DEFAULT_PARAMS に上書きマージし、欠損キーを埋めた完全な dict を返す。
    """
    over = cfg.get("params") or {}
    if not over and BEST_PARAMS_PATH.exists():
        champ = yaml.safe_load(BEST_PARAMS_PATH.read_text(encoding="utf-8")) or {}
        over = champ.get("params") or {}
    return merge_params(over)


def trigger_label(params: dict) -> str:
    """CSV の trigger 列に入れる「設定上の主トリガー」ラベルを返す。

    ティック版戦略は全ゲートの AND なのでシグナル単位の帰属は取れない。有効なゲートのうち
    手法の核になるものを優先順（トゲ > 角度 > 直前差 > 効率）で1つ選び、どの設定で回していたかを残す。
    """
    if float(params.get("spike_depth_min", 0.0)) > 0.0 or float(params.get("spike_turn_min", 0.0)) > 0.0:
        return "spike"
    if float(params.get("slope_threshold", 0.0)) > 0.0:
        return "angle"
    if float(params.get("prev_diff_min", 0.0)) > 0.0:
        return "prev_diff"
    return "eff"


# ---------------- ティック時刻のパース ----------------

def parse_tick_time_ns(s: str) -> int:
    """moomoo のティック時刻文字列を JST/市場ローカル naive のエポック ns（int64）に変換する。

    backtest engine の t_ns（datetime64[ns].astype(int64)）と同じ「naive 壁時計を tz 変換せず ns 化」する。
    小数秒の有無（"...:18.955" / "...:18"）両方を pandas が受ける。
    """
    return int(pd.Timestamp(s).value)


# ---------------- 通知（OS 別分岐） ----------------

def _osascript_escape(s: str) -> str:
    """osascript の文字列リテラルに埋め込めるようバックスラッシュとダブルクォートを退避する。"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _notify_macos(message: str, sound: Optional[str], run: Callable) -> None:
    """macOS 通知（osascript の display notification。承認プロンプトは出ない）。"""
    script = f'display notification "{_osascript_escape(message)}" with title "{NOTIFY_TITLE}"'
    if sound:
        script += f' sound name "{_osascript_escape(sound)}"'
    run(["osascript", "-e", script], check=False, capture_output=True, timeout=5)


def _ps_single_quote(s: str) -> str:
    """PowerShell の単一引用符文字列に埋め込めるよう ' を '' に退避する。"""
    return s.replace("'", "''")


def _powershell_toast_script(message: str, *, sound: bool) -> str:
    """Windows 10+ 標準トースト（[Windows.UI.Notifications]）を出す PowerShell スクリプトを組み立てる。

    BurntToast 等の外部モジュールに一切依存しない（WinRT の型を直接ロードして呼ぶ）。WinRT トーストが
    使えない環境（Server Core・古い OS・WinRT ロード失敗）では catch 節で console beep + 標準出力に
    フォールバックし、PowerShell 側でも例外で死なせない（呼び手の subprocess も 0 終了させ検知ループを止めない）。
    AppId は専用ショートカットを作らずに済むよう PowerShell 自身の AppUserModelID を借用する。
    """
    title = _ps_single_quote(NOTIFY_TITLE)
    body = _ps_single_quote(message)
    beep = "[console]::beep(880,200); " if sound else ""
    return (
        "$ErrorActionPreference='Stop'; "
        "try {"
        "$null=[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x=$t.GetElementsByTagName('text');"
        f"$null=$x.Item(0).AppendChild($t.CreateTextNode('{title}'));"
        f"$null=$x.Item(1).AppendChild($t.CreateTextNode('{body}'));"
        "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
        "$a='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($a).Show($n);"
        "} catch {"
        f"{beep}"
        f"Write-Output ('scalp-lab 通知(フォールバック): {body}');"
        "}"
    )


def _notify_windows(message: str, sound: Optional[str], run: Callable) -> None:
    """Windows 10+ 通知（PowerShell の WinRT トースト。失敗時は PS 内で beep+出力にフォールバック）。"""
    script = _powershell_toast_script(message, sound=bool(sound))
    run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False, capture_output=True, timeout=10)


def send_notification(message: str, *, sound: Optional[str] = None,
                      runner: Optional[Callable] = None,
                      system: Optional[str] = None) -> None:
    """OS を判定してデスクトップ通知を出す。失敗しても例外を外へ出さない（検知ループを止めない）。

    OS 別の挙動:
      - macOS   (Darwin) : osascript の display notification（承認プロンプトなし。従来どおり）。
      - Windows          : PowerShell の WinRT トースト（[Windows.UI.Notifications]。外部モジュール非依存。
                          使えない環境では PS 内で console beep + 標準出力にフォールバック）。
      - その他  (Linux 等): 標準出力/ログのみ（デスクトップ通知は出さない）。

    sound は macOS では通知音名（"Glass" 等）、Windows では音を鳴らすか否か（真偽で解釈）に使う。
    runner を差し替えるとテストで subprocess を呼ばずに検証できる（既定は subprocess.run）。
    system を渡すと OS 判定を上書きできる（テスト用。既定は platform.system()）。
    """
    run = runner if runner is not None else subprocess.run
    system = system if system is not None else platform.system()
    try:
        if system == "Darwin":
            _notify_macos(message, sound, run)
        elif system == "Windows":
            _notify_windows(message, sound, run)
        else:
            log.info("NOTIFY %s", message)  # デスクトップ通知非対応 OS はログのみ
    except Exception as e:  # noqa: BLE001 - 通知失敗で検知ループを止めない
        log.warning("通知コマンド失敗: %s", e)


def format_signal_message(sig: dict, trigger: str) -> str:
    """シグナル dict を通知・ログ用の1行に整形する。"""
    return (f'{sig["code"]} {sig["direction"].upper()} @ {sig["price"]:.6g}  '
            f'rci={sig["rci"]:.0f} drci={sig["drci"]:+.0f} [{trigger}]')


# ---------------- シグナル CSV ----------------

class SignalLog:
    """発火シグナルを logs/live-signals-*.csv に追記する薄いシンク（既存 LiveCsvSink と同じ流儀）。"""

    COLUMNS = ["time", "code", "direction", "price", "rci", "drci", "trigger"]

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        need_header = (not self.path.exists()) or self.path.stat().st_size == 0
        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._fh, fieldnames=self.COLUMNS, extrasaction="ignore")
        if need_header:
            self._w.writeheader()

    def append(self, sig: dict, trigger: str) -> None:
        self._w.writerow({
            "time": pd.Timestamp(sig["t_ns"]).isoformat(sep=" "),
            "code": sig["code"],
            "direction": sig["direction"],
            "price": f'{sig["price"]:.6g}',
            "rci": f'{sig["rci"]:.2f}',
            "drci": f'{sig["drci"]:.2f}',
            "trigger": trigger,
        })
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# ---------------- 検知（ティック → バー確定 → on_tick） ----------------

class LiveDetector:
    """監視銘柄ごとに RciImpulseStrategy を保持し、ティック列から 1 分足を合成して駆動する。

    engine（ベクトル化）と同じ逐次セマンティクス:「次の分の最初のティックで直前分の足を確定してから
    そのティックを on_tick で評価する」。SAR/ATR/確定 RCI は on_bar_confirm でしか更新されないため、
    ライブでもティックから 1 分足を合成して on_bar_confirm を呼ぶ必要がある。

    コールドスタート注意: 起動時は確定足が無いので ATR(atr_period) が確定するまで（既定 14 本≒14 分）は
    シグナルが出ない。寄り直後の数分は空振りになるのは想定挙動（ウォームアップ）。
    """

    def __init__(self, codes: list[str], params: dict) -> None:
        self._params = dict(params)
        self._strats: dict[str, RciImpulseStrategy] = {
            code: RciImpulseStrategy(params, code=code) for code in codes
        }
        self._cur_min: dict[str, Optional[int]] = {code: None for code in codes}
        self._bar: dict[str, Optional[dict]] = {code: None for code in codes}

    def process_tick(self, code: str, t_ns: int, price: float) -> Optional[dict]:
        """1 ティックを処理し、シグナル成立時のみシグナル dict を返す（他は None）。"""
        strat = self._strats.get(code)
        if strat is None:
            return None  # 監視外の銘柄は無視
        t_ns = int(t_ns)
        price = float(price)
        minute = t_ns // _MIN_NS
        cur = self._cur_min[code]

        if cur is None:
            # 最初のティック: 直前足は無い。今分のバーを開始するだけ。
            self._cur_min[code] = minute
            self._bar[code] = {"high": price, "low": price, "close": price}
        elif minute > cur:
            # 分が変わった: 直前分の足を確定してから新しい分のバーを開始する。
            strat.on_bar_confirm(self._bar[code])
            self._cur_min[code] = minute
            self._bar[code] = {"high": price, "low": price, "close": price}
        elif minute == cur:
            b = self._bar[code]
            if price > b["high"]:
                b["high"] = price
            if price < b["low"]:
                b["low"] = price
            b["close"] = price
        else:
            # 逆行ティック（分をまたいで過去へ戻る乱れ）は無視して時系列の単調性を保つ。
            return None

        return strat.on_tick(t_ns, price)


# ---------------- push ハンドラ（futu の別スレッドから呼ばれる → queue に積むだけ） ----------------

class _TickerHandler(TickerHandlerBase):
    def __init__(self, q: queue.Queue, state: dict) -> None:
        super().__init__()
        self.q = q
        self.state = state

    def on_recv_rsp(self, rsp):
        ret, data = super().on_recv_rsp(rsp)
        if ret != RET_OK:
            return ret, data
        for _, r in data.iterrows():
            try:
                t_ns = parse_tick_time_ns(str(r["time"]))
                price = float(r["price"])
            except Exception:  # noqa: BLE001 - 壊れた1行で受信スレッドを落とさない
                continue
            self.q.put((str(r["code"]), t_ns, price))
            self.state["ticks"] += 1
        return ret, data


# ---------------- ティック供給源アダプタ（証券会社非依存） ----------------

class TickSource:
    """ライブ検知に流し込むティック供給源の共通インタフェース（証券会社非依存）。

    実装は connect() → subscribe() で接続・購読を確立し、受信した各ティックを
    (code, t_ns, price) の3タプルで tick_queue に push、受信のたび state["ticks"] を +1 する。
    検知ループ(run_live)・疎通確認(check_mode) はこの tick_queue から取り出すだけで供給源の違いを
    意識しない。データ源の証券会社（moomoo / kabu STATION / 立花 …）はこのクラスを差し替えて足す。
    session_day / session_segments で供給源ごとの立会時間（US / 東証）も返す。
    """

    name = "接続先"  # check_mode の表示ラベル（実装で上書き）

    def connect(self) -> None:
        """接続を確立する（供給源固有の到達確認を含む）。到達不能はその源固有の例外を投げてよい。"""
        raise NotImplementedError

    def subscribe(self, codes: list[str], tick_queue: "queue.Queue",
                  state: dict) -> tuple[list[str], list[tuple[str, str]]]:
        """codes を購読し (成功銘柄, 失敗[(code, 理由)]) を返す。ティックは tick_queue に push する。"""
        raise NotImplementedError

    def subscription_stats(self) -> dict:
        """購読クォータ統計（{total_used, remain} 相当の dict）を返す。取得できなければ例外でよい。"""
        raise NotImplementedError

    def close(self) -> None:
        """購読解除・接続クローズ（セッション終了時。冪等に）。"""
        raise NotImplementedError

    def session_day(self, now: Optional[datetime] = None) -> str:
        """この供給源の市場のセッション日(YYYY-MM-DD)を返す。"""
        raise NotImplementedError

    def session_segments(self, date: str) -> list[tuple[datetime, datetime]]:
        """この供給源の市場の立会区間 [(開始, 終了), ...]（マシンローカル naive）を返す。"""
        raise NotImplementedError


class MoomooTickSource(TickSource):
    """moomoo OpenD 経由の TICKER 供給源（米国株のみ相場対応）。

    録画デーモン(record.py)と同じ OpenQuoteContext ラッパ(MoomooClient)＋純粋ヘルパ(subscribe_all)を
    読み取り利用する。ORDER_BOOK は購読せず TICKER のみ（足跡を最小化）。JP.* は moomoo が相場未対応で
    購読拒否される（日本株は KabuStationTickSource を使う）。セッションは NY 09:30-16:00。
    """

    name = "OpenD"

    def __init__(self) -> None:
        self._client = MoomooClient()

    def connect(self) -> None:
        # OpenD 未起動は OpenDUnavailableError を投げる（呼び手が exit code を判断する）。
        self._client.connect()

    def subscribe(self, codes, tick_queue, state):
        self._client.set_handler(_TickerHandler(tick_queue, state))
        return subscribe_all(self._client, codes, [SubType.TICKER])

    def subscription_stats(self) -> dict:
        return self._client.query_subscription()

    def close(self) -> None:
        try:
            self._client.unsubscribe_all()
        except Exception:  # noqa: BLE001 - 解除失敗しても close で片付くので落とさない
            pass
        self._client.close()

    def session_day(self, now: Optional[datetime] = None) -> str:
        return us_session_day(now)

    def session_segments(self, date: str) -> list[tuple[datetime, datetime]]:
        return us_session_segments(date)


# ---- kabu STATION のコード変換・時刻正規化ヘルパ（テストから直接叩けるよう純関数化） ----

def _jp_to_kabu_symbol(code: str) -> str:
    """内部コード 'JP.7203' → kabu の Symbol '7203'（'JP.' 接頭辞が無ければそのまま）。"""
    return code.split(".", 1)[1] if "." in code else code


def _kabu_symbol_to_code(symbol: str) -> str:
    """kabu の Symbol '7203' → 内部コード 'JP.7203'。"""
    return f"JP.{symbol}"


def _kabu_time_to_ns(s: str) -> int:
    """kabu の CurrentPriceTime（ISO8601・+09:00 付き想定）を JST 壁時計 naive の epoch ns に変換する。

    backtest engine / moomoo 経路と同じ「naive 壁時計を tz 変換せず ns 化」に揃える
    （tz-aware なら Asia/Tokyo に変換してから tz を落とす。東証は DST 無しなので壁時計は不変）。
    """
    ts = pd.Timestamp(s)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Tokyo").tz_localize(None)
    return int(ts.value)


def _safe_close(obj) -> None:
    """close() を持つオブジェクトを例外を握って閉じる（WebSocket/ジェネレータ両対応）。"""
    if obj is None:
        return
    close = getattr(obj, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC6455 のハンドシェイク用マジック


class _KabuWebSocket:
    """kabu STATION の WebSocket PUSH を受信する最小クライアント（stdlib のみ・依存追加なし）。

    受信専用（クライアントからの送信はハンドシェイクと ping への pong 応答だけ）なので、RFC6455 の
    テキスト/バイナリフレーム解読・分割フレーム連結・ping/pong・close だけを実装する。
    `for msg in ws:` でテキストメッセージ(str)を1件ずつ yield し、サーバ close／切断で StopIteration。
    """

    def __init__(self, host: str, port: int, path: str, timeout: float = 60.0) -> None:
        self.host = host
        self.port = port
        self.path = path
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buf = b""

    def connect(self) -> "_KabuWebSocket":
        key = base64.b64encode(os.urandom(16)).decode()
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        s.sendall(req.encode("ascii"))
        head = self._read_handshake(s)
        status_line = head.split("\r\n", 1)[0]
        if "101" not in status_line:
            s.close()
            raise ConnectionError(f"WebSocket ハンドシェイク失敗: {status_line!r}")
        self._sock = s
        return self

    def _read_handshake(self, s: socket.socket) -> str:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        self._buf = rest  # ハンドシェイク直後に届いたフレームデータを取りこぼさない
        return head.decode("latin1")

    def _recv_bytes(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 接続が切れた（recv 0 バイト）")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = self._recv_bytes(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_bytes(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_bytes(8))[0]
        mask = self._recv_bytes(4) if masked else b""
        payload = self._recv_bytes(length) if length else b""
        if masked:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(length))
        return fin, opcode, payload

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self._sock is None:
            return
        n = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        mask = os.urandom(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(n))
        self._sock.sendall(bytes(header) + masked)

    def __iter__(self) -> "_KabuWebSocket":
        return self

    def __next__(self) -> str:
        data = b""
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:          # close
                raise StopIteration
            if opcode == 0x9:          # ping → pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:          # pong（無視）
                continue
            data += payload            # text(0x1)/binary(0x2)/continuation(0x0)
            if fin:
                return data.decode("utf-8", "replace")

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                self._send_frame_on(sock, 0x8)
            except Exception:  # noqa: BLE001
                pass
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

    def _send_frame_on(self, sock: socket.socket, opcode: int) -> None:
        mask = os.urandom(4)
        sock.sendall(bytes([0x80 | (opcode & 0x0F), 0x80]) + mask)


class KabuStationTickSource(TickSource):
    """auカブコム kabu STATION API 経由のティック供給源（日本株・**Windows 常駐アプリ必須**）。

    kabu STATION アプリが起動している時だけ localhost に REST(`/kabusapi`) と WebSocket が生きる。
    フロー: connect() で `POST /token`（APIPassword→Token・日次失効）→ subscribe() で `PUT /register`
    （最大50銘柄）→ `ws://localhost:<port>/kabusapi/websocket` の PUSH を受信スレッドで受け取り、
    moomoo と同じ (code, t_ns, price) に正規化して tick_queue へ流す。close() で受信停止＋`/unregister/all`。

    **PUSH は価格更新ベース（全約定ではない）**: CurrentPrice が更新された時に届く（約定が無くても板由来で
    更新されうる／逆に同値約定は届かないことがある）。それでも足内 RCI（IntrabarRci）検知には十分＝
    分足を合成して各更新で on_tick を評価できる。全約定の歩み値が必要なら別 API（板情報の逐次差分）が要る。

    資格情報: APIPassword は環境変数（既定 `KABU_API_PASSWORD`。config の `kabu.password_env` で変数名変更可）
    から読む。**平文でリポジトリに置かない**。host/port/exchange/reconnect_tries は config の `kabu.*` で調整。
    REST(`http_request`) と WebSocket(`ws_connect`) は DI 可能（テストでモックを注入して実機非依存に検証する）。
    """

    name = "kabu STATION"
    _MAX_SYMBOLS = 50
    _API_PREFIX = "/kabusapi"

    def __init__(self, cfg: Optional[dict] = None, *, password: Optional[str] = None,
                 host: Optional[str] = None, port: Optional[int] = None,
                 exchange: Optional[int] = None, reconnect_tries: Optional[int] = None,
                 http_request: Optional[Callable] = None, ws_connect: Optional[Callable] = None,
                 auto_stream: bool = True) -> None:
        kcfg = ((cfg or {}).get("kabu") or {})
        if password is None:
            self._password_env = kcfg.get("password_env", "KABU_API_PASSWORD")
            password = os.environ.get(self._password_env)
        else:
            self._password_env = None
        self._password = password
        self.host = host or kcfg.get("host", "localhost")
        self.port = int(port if port is not None else kcfg.get("port", 18080))
        self._exchange = int(exchange if exchange is not None else kcfg.get("exchange", 1))  # 1=東証
        self._reconnect_tries = int(
            reconnect_tries if reconnect_tries is not None else kcfg.get("reconnect_tries", 1))
        self._http_request = http_request or self._default_http_request
        self._ws_connect = ws_connect or self._default_ws_connect
        self._auto_stream = auto_stream
        self._token: Optional[str] = None
        self._registered: list[str] = []
        self._tick_queue: Optional[queue.Queue] = None
        self._state: Optional[dict] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None

    # --- 接続・認証 ---

    def connect(self) -> None:
        if not self._password:
            hint = f"環境変数 {self._password_env}" if self._password_env else "password 引数"
            raise TickSourceUnavailableError(
                f"kabu STATION の APIPassword が未設定です（{hint} を設定してください）。")
        self._connect_token()

    def _connect_token(self) -> None:
        # KabuS 起動直後は API ポートが開いておらず ConnectionRefused になる（2026-09-01 実測）。
        # 生の OSError のまま伝播すると呼び出し側の except TickSourceUnavailableError を
        # すり抜けて traceback クラッシュするので、ここで意味のある例外に変換する。
        try:
            status, data = self._http_request(
                "POST", f"{self._API_PREFIX}/token",
                {"Content-Type": "application/json"}, {"APIPassword": self._password})
        except OSError as e:
            raise TickSourceUnavailableError(
                f"kabu STATION API ({self.host}:{self.port}) へ接続できません: {e}。"
                "アプリが起動済みで API 設定が有効か確認してください。") from e
        if status != 200 or not data.get("Token"):
            raise TickSourceUnavailableError(
                f"kabu STATION トークン取得に失敗 (status={status}, resp={data})。"
                "kabu STATION アプリが起動・ログイン済みで API 設定が有効か、APIPassword が正しいか確認してください。")
        self._token = data["Token"]
        log.info("kabu STATION トークン取得成功")

    # --- 購読 ---

    def subscribe(self, codes, tick_queue, state):
        self._tick_queue = tick_queue
        self._state = state
        if self._token is None:
            self._connect_token()
        ok, failed = self._register(list(codes))
        self._registered = list(ok)
        if ok and self._auto_stream:
            self._stop.clear()
            self._thread = threading.Thread(target=self._ws_loop, name="kabu-ws", daemon=True)
            self._thread.start()
        return ok, failed

    def _put_register(self, body: dict) -> tuple[int, dict]:
        return self._http_request(
            "PUT", f"{self._API_PREFIX}/register",
            {"Content-Type": "application/json", "X-API-KEY": self._token or ""}, body)

    def _register(self, codes: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
        symbols = [_jp_to_kabu_symbol(c) for c in codes]
        body = {"Symbols": [{"Symbol": s, "Exchange": self._exchange} for s in symbols]}
        status, data = self._put_register(body)
        if status == 401:  # トークン失効 → 再取得して1回だけリトライ
            log.info("kabu register が 401（トークン失効）。再取得して再登録する")
            self._connect_token()
            status, data = self._put_register(body)
        if status != 200:
            reason = f"register 失敗 status={status}: {data}"
            return [], [(c, reason) for c in codes]
        registered = {str(r.get("Symbol")) for r in data.get("RegistList", [])}
        ok, failed = [], []
        for code, sym in zip(codes, symbols):
            if sym in registered:
                ok.append(code)
            else:
                failed.append((code, "register に反映されず（銘柄コード/市場を確認）"))
        return ok, failed

    def subscription_stats(self) -> dict:
        used = len(self._registered)
        return {"total_used": used, "remain": max(0, self._MAX_SYMBOLS - used)}

    # --- スナップショット取得（寄り前気配の記録用） ---

    def unregister_all(self) -> tuple[int, dict]:
        """`PUT /unregister/all` で銘柄登録枠（50 銘柄）を空にする。(status, data) を返す。

        なぜ公開メソッドが要るか: `/board` は register 不要と思われていたが、実機では**内部で銘柄を
        自動登録して 50 枠を消費する**（2026-08-19 実測。51 銘柄目以降は Code 4002006 レジスト数エラー）。
        50 銘柄を超える巡回（scalplab.premarket の 291 銘柄）は「枠を空ける → 次の 50 銘柄を /board」の
        ローテーションでしか成立しないため、巡回側から枠を空けられるようにする。
        トークン失効（401）は register / board と同じ流儀で再取得して1回だけリトライする。
        """
        path = f"{self._API_PREFIX}/unregister/all"
        status, data = self._http_request(
            "PUT", path, {"Content-Type": "application/json", "X-API-KEY": self._token or ""}, None)
        if status == 401:
            log.info("kabu unregister/all が 401（トークン失効）。再取得して再試行する")
            self._connect_token()
            status, data = self._http_request(
                "PUT", path,
                {"Content-Type": "application/json", "X-API-KEY": self._token or ""}, None)
        return status, data

    def board_snapshot(self, code: str) -> tuple[int, dict]:
        """`GET /board` で時価・板のスナップショットを1件取る。

        寄り前気配の全銘柄巡回（scalplab.premarket）はこれを使う。**50 銘柄を超えて回すには
        unregister_all() との併用が必須**（/board 自体が登録枠を消費するため。同メソッドの docstring 参照）。
        トークン失効（401）は register と同じ流儀で再取得して1回だけリトライする。
        """
        sym = _jp_to_kabu_symbol(code)
        path = f"{self._API_PREFIX}/board/{sym}@{self._exchange}"
        status, data = self._http_request("GET", path, {"X-API-KEY": self._token or ""}, None)
        if status == 401:
            log.info("kabu board が 401（トークン失効）。再取得して再試行する")
            self._connect_token()
            status, data = self._http_request("GET", path, {"X-API-KEY": self._token or ""}, None)
        return status, data

    # --- WebSocket 受信 ---

    def _default_ws_connect(self):
        ws = _KabuWebSocket(self.host, self.port, path=f"{self._API_PREFIX}/websocket")
        ws.connect()
        return ws

    def _ws_loop(self) -> None:
        """WebSocket を受信し続ける（切断時は reconnect_tries 回まで再接続する）。受信スレッド本体。"""
        attempts = 0
        while not self._stop.is_set():
            try:
                ws = self._ws_connect()
                self._ws = ws
                try:
                    for msg in ws:
                        if self._stop.is_set():
                            break
                        self._handle_message(msg)
                finally:
                    self._ws = None
                    _safe_close(ws)
            except Exception as e:  # noqa: BLE001 - 受信スレッドを落とさない
                log.warning("kabu WebSocket 受信エラー: %s", e)
            if self._stop.is_set():
                break
            if attempts >= self._reconnect_tries:
                log.warning("kabu WebSocket 再接続の上限（%d 回）に達した。受信を終了する。",
                            self._reconnect_tries)
                break
            attempts += 1
            log.info("kabu WebSocket 再接続 %d/%d", attempts, self._reconnect_tries)
            try:
                self._reconnect_session()
            except Exception as e:  # noqa: BLE001 - 再取得失敗ならループを畳む
                log.warning("kabu 再接続時のトークン再取得/再登録に失敗: %s", e)
                break

    def _reconnect_session(self) -> None:
        """再接続に備えてトークンを取り直し、登録済み銘柄を register し直す。"""
        self._connect_token()
        if self._registered:
            self._register(self._registered)

    def _handle_message(self, raw: str) -> None:
        """PUSH の1メッセージ(JSON)を (code, t_ns, price) に正規化して tick_queue へ入れる。"""
        try:
            d = json.loads(raw)
        except Exception:  # noqa: BLE001 - 壊れた1メッセージで受信を止めない
            return
        price = d.get("CurrentPrice")
        symbol = d.get("Symbol")
        if price is None or not symbol:
            return  # 価格更新でない PUSH（板のみ更新など）は投入しない
        t = d.get("CurrentPriceTime")
        try:
            t_ns = _kabu_time_to_ns(t) if t else int(pd.Timestamp.now().value)
            price = float(price)
        except Exception:  # noqa: BLE001
            return
        self._tick_queue.put((_kabu_symbol_to_code(str(symbol)), t_ns, price))
        self._state["ticks"] += 1

    # --- 終了 ---

    def close(self) -> None:
        self._stop.set()
        ws, self._ws = self._ws, None
        _safe_close(ws)  # 受信ブロック中の recv を解除する
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._token and self._registered:
            try:
                self._http_request(
                    "PUT", f"{self._API_PREFIX}/unregister/all",
                    {"Content-Type": "application/json", "X-API-KEY": self._token}, None)
            except Exception as e:  # noqa: BLE001
                log.warning("kabu unregister/all 失敗: %s", e)
        self._registered = []

    # --- REST（stdlib http.client） ---

    def _default_http_request(self, method: str, path: str, headers: dict,
                              body: Optional[dict]) -> tuple[int, dict]:
        """kabu STATION のローカル REST を1回叩く。(status, data) を返す（stdlib のみ）。"""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5.0)
        try:
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                data = json.loads(raw) if raw else {}
            except Exception:  # noqa: BLE001
                data = {"_raw": raw.decode("utf-8", "replace")}
            return resp.status, data
        finally:
            conn.close()

    # --- セッション時間（東証） ---

    def session_day(self, now: Optional[datetime] = None) -> str:
        return jp_session_day(now)

    def session_segments(self, date: str) -> list[tuple[datetime, datetime]]:
        return jp_session_segments(date)


class TachibanaTickSource(TickSource):
    """立花証券 e支店 API（OS 不問・Mac 完結可）経由の供給源（未実装・将来枠）。

    立花 e支店 API は HTTP 要求 + イベント通知(EVENT I/F) 方式。実装時は connect() でログイン
    (CLMAuthLoginRequest)、subscribe() で時価の実況(EVENT) を購読し PUSH を (code, t_ns, price) に
    整形して tick_queue へ流す（KabuStationTickSource と同じ subscribe/close 契約）。日本株なので
    セッションは東証。データ源は kabu STATION に確定済みのため当面は枠のみ。
    """

    name = "立花 e支店"

    def connect(self) -> None:
        raise NotImplementedError("立花 e支店 供給源は未実装（データ源は kabu STATION に確定済み）")

    def subscribe(self, codes, tick_queue, state):
        raise NotImplementedError("立花 e支店 供給源は未実装（データ源は kabu STATION に確定済み）")

    def subscription_stats(self) -> dict:
        raise NotImplementedError("立花 e支店 供給源は未実装（データ源は kabu STATION に確定済み）")

    def close(self) -> None:
        raise NotImplementedError("立花 e支店 供給源は未実装（データ源は kabu STATION に確定済み）")

    def session_day(self, now: Optional[datetime] = None) -> str:
        return jp_session_day(now)

    def session_segments(self, date: str) -> list[tuple[datetime, datetime]]:
        return jp_session_segments(date)


# ---------------- シグナル処理 ----------------

def handle_signal(sig: dict, *, trigger: str, signal_log: Optional[SignalLog],
                  sound: Optional[str], notifier: Callable = send_notification) -> None:
    """発火シグナルを「ログ1行 → CSV 追記 → 通知」の順で処理する。"""
    msg = format_signal_message(sig, trigger)
    log.info("SIGNAL %s", msg)
    if signal_log is not None:
        signal_log.append(sig, trigger)
    notifier(msg, sound=sound)


# ---------------- ロギング ----------------

def _setup_logging(date: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger("scalplab")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.FileHandler(LOG_DIR / f"live-{date}.log")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


# ---------------- --check（疎通確認） ----------------

def check_mode(codes: list[str], seconds: int = 10, *,
               source_factory: Callable[[], TickSource] = MoomooTickSource) -> int:
    """接続 → 購読 → seconds 秒の受信待ち → 統計表示 → 終了。市場外でも実行できる。

    目的は「接続と購読が通ること」の確認。市場閉場中はティック 0 でよい（購読クォータの used/remain も表示）。
    source_factory を差し替えるとテストで OpenD 非依存に検証できる（既定は MoomooTickSource）。
    """
    print("=== scalp-lab live --check ===")
    q: queue.Queue = queue.Queue()
    state = {"ticks": 0}
    source = source_factory()
    label = getattr(source, "name", "接続先")
    try:
        try:
            source.connect()
        except (OpenDUnavailableError, TickSourceUnavailableError) as e:
            print(f"{label} 接続 : NG - {e}")
            return 2
        print(f"{label} 接続 : OK")

        try:
            sub0 = source.subscription_stats()
            print(f"購読前クォータ  : used={sub0.get('total_used')} remain={sub0.get('remain')}")
        except Exception as e:  # noqa: BLE001
            print(f"購読前クォータ  : 取得失敗 - {e}")

        ok, failed = source.subscribe(codes, q, state)
        print(f"購読成功        : {len(ok)}/{len(codes)} {ok}")
        for code, reason in failed:
            print(f"  購読失敗      : {code} - {reason}")

        try:
            sub1 = source.subscription_stats()
            print(f"購読後クォータ  : used={sub1.get('total_used')} remain={sub1.get('remain')}")
        except Exception as e:  # noqa: BLE001
            print(f"購読後クォータ  : 取得失敗 - {e}")

        if not ok:
            print("購読が全滅したため受信待ちをスキップして終了。")
            return 1

        print(f"受信待ち        : {seconds} 秒...")
        per_code: dict[str, int] = {}
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                code, _t_ns, _price = q.get(timeout=0.5)
            except queue.Empty:
                continue
            per_code[code] = per_code.get(code, 0) + 1
        print(f"受信ティック合計: {state['ticks']}")
        for code in sorted(per_code):
            print(f"  {code}: {per_code[code]}")
        if state["ticks"] == 0:
            print("（ティック 0 ＝市場閉場中は正常。接続・購読の疎通確認が目的）")
        return 0
    finally:
        source.close()


# ---------------- ライブ検知ループ ----------------

def _wait_until(target: datetime, now_fn: Callable[[], datetime]) -> None:
    """target 時刻まで 1〜5 秒刻みで中断可能に待機する（沈黙させない進捗ログ付き）。"""
    log.info("寄り付き待機: %s まで待つ", target.strftime("%H:%M:%S"))
    last_log = time.monotonic()
    while now_fn() < target:
        time.sleep(min(5.0, max(1.0, (target - now_fn()).total_seconds())))
        if time.monotonic() - last_log >= 30.0:
            remain = (target - now_fn()).total_seconds()
            log.info("寄り付き待機中... 残り %.0f 秒", max(0.0, remain))
            last_log = time.monotonic()


def run_live(codes: list[str], params: dict, *, notify_sound: Optional[str] = None,
             duration: Optional[float] = None, session: bool = True,
             signal_log_path: Optional[Path] = None,
             notifier: Callable = send_notification,
             now_fn: Callable[[], datetime] = datetime.now,
             source_factory: Callable[[], TickSource] = MoomooTickSource) -> int:
    """ライブ検知ループ本体。exit code を返す。

    - duration 指定: 市場時間を無視して duration 秒だけ回す（手動スモーク用）。
    - duration 未指定 & session=True: 供給源のセッション時間内のみ稼働（寄り前は待機、引け後は即終了）。
      セッション時間は source.session_segments に委ねる（moomoo=NY 09:30-16:00 / kabu=東証立会）。
    - source_factory: ティック供給源を差し替える（テスト用。既定は MoomooTickSource）。
    """
    detector = LiveDetector(codes, params)
    trigger = trigger_label(params)
    source = source_factory()
    if signal_log_path is None:
        signal_log_path = LOG_DIR / f"live-signals-{source.session_day(now_fn())}.csv"
    slog = SignalLog(signal_log_path)

    q: queue.Queue = queue.Queue()
    state = {"ticks": 0}
    n_signals = 0
    try:
        try:
            source.connect()
        except (OpenDUnavailableError, TickSourceUnavailableError) as e:
            log.error("データ源へ接続できず検知を開始できない: %s", e)
            return 1
        ok, failed = source.subscribe(codes, q, state)
        log.info("購読成功 %d / 失敗 %d（監視 %d 銘柄）", len(ok), len(failed), len(codes))
        for code, reason in failed:
            log.warning("購読失敗: %s - %s", code, reason)
        if not ok:
            log.error("全銘柄の購読に失敗（相場権限・銘柄コードを確認）。終了する。")
            return 1

        # --- 稼働時間の決定（供給源のセッションに委ねる） ---
        end_dt: Optional[datetime] = None
        start_mono = time.monotonic()
        if duration is None and session:
            segs = source.session_segments(source.session_day(now_fn()))
            open_dt, close_dt = segs[0][0], segs[-1][1]  # 最初の寄り〜最後の引け（昼休みは空振り）
            now = now_fn()
            if now >= close_dt:
                log.info("セッション終了後の起動のため何もしない（%s 以降）", close_dt.strftime("%H:%M"))
                return 0
            if now < open_dt:
                _wait_until(open_dt, now_fn)
            end_dt = close_dt
            log.info("検知開始: %d 銘柄、%s に終了予定", len(ok), close_dt.strftime("%H:%M"))
        else:
            log.info("検知開始（duration=%s 秒・セッション無視）: %d 銘柄",
                     "無制限" if duration is None else f"{duration:.0f}", len(ok))

        # --- メインループ ---
        last_summary = time.monotonic()
        last_ticks = 0

        def _running() -> bool:
            if duration is not None:
                return (time.monotonic() - start_mono) < duration
            if end_dt is not None:
                return now_fn() < end_dt
            return True  # session=False & duration=None は無制限（手動 Ctrl-C 終了）

        while _running():
            try:
                code, t_ns, price = q.get(timeout=0.5)
            except queue.Empty:
                pass
            else:
                sig = detector.process_tick(code, t_ns, price)
                if sig is not None:
                    n_signals += 1
                    handle_signal(sig, trigger=trigger, signal_log=slog,
                                  sound=notify_sound, notifier=notifier)

            now_mono = time.monotonic()
            if now_mono - last_summary >= 60.0:
                d_ticks = state["ticks"] - last_ticks
                log.info("稼働中 銘柄=%d ticks=%d(+%d) signals=%d",
                         len(ok), state["ticks"], d_ticks, n_signals)
                last_summary = now_mono
                last_ticks = state["ticks"]

        log.info("検知終了: 受信 ticks=%d / 発火 signals=%d", state["ticks"], n_signals)
        return 0
    finally:
        source.close()
        slog.close()


# ---------------- エントリポイント ----------------

def _make_source_factory(cfg: dict) -> tuple[Callable[[], TickSource], str]:
    """config の source（moomoo|kabu）から供給源ファクトリとセッション日を決める。

    未知の source は ValueError（呼び手が使い方を表示して終了する）。
    """
    source_type = str(cfg.get("source", "moomoo")).lower()
    if source_type == "moomoo":
        return MoomooTickSource, us_session_day()
    if source_type == "kabu":
        return (lambda: KabuStationTickSource(cfg)), jp_session_day()
    raise ValueError(f"未知の source: {source_type}（moomoo か kabu を指定してください）")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="リアルタイムシグナル通知（手動起動。米国=moomoo / 日本=kabu STATION）")
    parser.add_argument("--check", action="store_true",
                        help="接続 → 購読 → 10 秒受信 → 統計表示で終了（市場外でも可）")
    parser.add_argument("--codes", default=None,
                        help="監視銘柄をカンマ区切りで上書き（例 US.TQQQ,US.SOXL / JP.7203,JP.9984）。既定は live.yaml の watch")
    parser.add_argument("--duration", type=float, default=None,
                        help="市場時間を無視して指定秒数だけ稼働（市場が開いている時間帯の手動スモーク用）")
    parser.add_argument("--config", default=None, help="live.yaml のパス（既定 config/live.yaml）")
    args = parser.parse_args(argv)

    cfg = load_live_config(Path(args.config) if args.config else CONFIG_PATH)
    codes = ([c.strip() for c in args.codes.split(",") if c.strip()]
             if args.codes else list(cfg["watch"]))
    if not codes:
        print("監視銘柄が空。config/live.yaml の watch か --codes を指定してください。")
        return 1

    source_type = str(cfg.get("source", "moomoo")).lower()
    try:
        source_factory, date = _make_source_factory(cfg)
    except ValueError as e:
        print(e)
        return 1
    _setup_logging(date)
    log.info("データ源: %s", source_type)

    # データ源に合わない銘柄は購読拒否される見込み。警告だけ出して購読は試みる（銘柄単位で握って続行）。
    if source_type == "kabu":
        non_jp = [c for c in codes if not c.startswith("JP.")]
        if non_jp:
            log.warning("kabu STATION は日本株のみ。JP.* 以外は登録拒否される見込み: %s", non_jp)
    else:
        jp = [c for c in codes if not c.startswith("US.")]
        if jp:
            log.warning("米国株以外は moomoo 相場未対応のため購読拒否される見込み: %s", jp)

    if args.check:
        return check_mode(codes, source_factory=source_factory)

    params = resolve_params(cfg)
    sound = "Glass" if cfg.get("notify", {}).get("sound", True) else None
    log.info("検知パラメータ: %s", {k: params[k] for k in (
        "rci_period", "slope_window_sec", "slope_threshold", "prev_diff_min",
        "spike_depth_min", "spike_turn_min", "eff_min", "cooldown_sec")})
    return run_live(codes, params, notify_sound=sound, duration=args.duration,
                    source_factory=source_factory)


if __name__ == "__main__":
    raise SystemExit(main())
