"""ティック録画デーモン（`python -m scalplab.record`）。

2つのモード:
- 既定（引数なし）: JST の当日東証セッション（9:00-11:30, 12:30-15:30）を録画。8:5x 起動なら
  9:00 まで待機し、universe を解決 → 各銘柄を購読 → push を queue 経由でシンクへ → 15:35 に
  ファイナライズして自然終了する。
- アドホック: `--codes US.AAPL,US.TSLA --duration 60` で市場時間を無視し指定秒数だけ録画（検証用）。

クラッシュ安全性: 録画中は data/.live/<date>/{code}.{ticks|quotes}.csv に追記するだけ。終了時（finally）に
契約 parquet へ変換し .live を掃除する。途中でクラッシュしても `python -m scalplab.store.finalize`
で救済できる。日本株の権限拒否・クォータ超過は銘柄単位でログして続行し、全滅時のみ exit 1。
"""
from __future__ import annotations

import argparse
import logging
import math
import queue
import threading
import time
from datetime import date as date_t, datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from futu import (
    RET_OK,
    CurKlineHandlerBase,
    OrderBookHandlerBase,
    StockQuoteHandlerBase,
    SubType,
    TickerHandlerBase,
)

from .datafeed.moomoo_client import MoomooClient, NoPermissionError, OpenDUnavailableError
from .datafeed.universe import moomoo_to_yf, resolve_codes, resolve_codes_us
from .store.finalize import finalize_date
from .store.loader import bars1m_path
from .store.writer import (
    LIVE_DIR,
    QUOTES_COLUMNS,
    TICKS_COLUMNS,
    LiveCsvSink,
    enforce_bars1m,
    write_parquet,
)

log = logging.getLogger("scalplab.record")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "recorder.yaml"
LOG_DIR = PROJECT_ROOT / "logs"

# config の SubType 文字列 → futu SubType
SUBTYPE_MAP = {
    "TICKER": SubType.TICKER,
    "ORDER_BOOK": SubType.ORDER_BOOK,
    "QUOTE": SubType.QUOTE,
    "K_1M": SubType.K_1M,
}
SUBSCRIPTION_QUOTA = 100  # OpenD 実測クォータ

# OpenD 未起動時にプロセス内で再接続を試す間隔（秒）。launchd に再起動させるより長い周期で
# 粘ることで、後処理の空振り連打（2026-07-28 障害）を防ぐ。
CONNECT_RETRY_SEC = 60.0


# ---------------- 設定・時刻ロジック（純粋関数・テスト対象） ----------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    """recorder.yaml を読む。欠損キーは既定で補う。"""
    cfg = {}
    if path.exists():
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("subtypes", ["TICKER", "ORDER_BOOK"])
    cfg.setdefault("fallback_bars", False)
    # yfinance フォールバックは moomoo 購読クォータと無関係なので、購読リストより広い
    # 「松井ランキング上位 N」までバックテスト用に貯める（0 で購読リストのみ）。
    cfg.setdefault("fallback_top_n", 100)
    # J-Quants 分足の日次取り込み対象銘柄数（松井 Tick ランキング上位 N）。0 で無効。
    # API キー未設定なら（この値に関わらず）1 行 INFO でスキップする（無害）。
    cfg.setdefault("jquants_daily_top_n", 10)
    cfg.setdefault("summary_interval_sec", 60)
    cfg.setdefault("stale_timeout_sec", 90)
    session = cfg.setdefault("session", {})
    session.setdefault("premarket_wait_from", "08:50")
    session.setdefault("morning", ["09:00", "11:30"])
    session.setdefault("afternoon", ["12:30", "15:30"])
    session.setdefault("finalize_at", "15:35")
    return cfg


def _combine(date_str: str, hhmm: str) -> datetime:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    t = datetime.strptime(hhmm, "%H:%M").time()
    return datetime.combine(d, t)


def session_segments(cfg: dict, date_str: str) -> list[tuple[datetime, datetime]]:
    """東証セッションの (開始, 終了) を返す（午前・午後の2区間）。"""
    s = cfg["session"]
    return [
        (_combine(date_str, s["morning"][0]), _combine(date_str, s["morning"][1])),
        (_combine(date_str, s["afternoon"][0]), _combine(date_str, s["afternoon"][1])),
    ]


def is_in_session(now: datetime, segments: list[tuple[datetime, datetime]]) -> bool:
    """now がいずれかのセッション区間内か。"""
    return any(a <= now <= b for a, b in segments)


# ---------------- 米国セッションの時刻ロジック（zoneinfo 依存・JP パスに影響しない） ----------------
#
# 日本株モードは全て JST naive を前提にしている（_combine/session_segments）。米国株は
# 「NY ローカルの 09:30-16:00」を録画したいが、recorder は datetime.now()（マシンローカル
# naive = JST）と突き合わせるため、NY 時刻をマシンローカルの naive datetime に変換して扱う。
# 夏冬時間の差は zoneinfo に委ねる（JST では夏 22:30〜翌05:00 / 冬 23:30〜翌06:00 になる）。
# JP 用の既存関数には一切手を入れず、US 専用関数を追加している。

_NY_TZ = ZoneInfo("America/New_York")
US_OPEN = dt_time(9, 30)
US_CLOSE = dt_time(16, 0)
US_FINALIZE = dt_time(16, 5)


def us_session_day(now: datetime | None = None) -> str:
    """米国東部(America/New_York)の現在日付をセッション日(YYYY-MM-DD)として返す。

    JST 深夜0時をまたいで起動しても NY 日付基準で同一セッション日に保存するため。
    now に tz-aware datetime を渡すとテストで固定できる（None なら実時刻）。
    """
    now_ny = datetime.now(_NY_TZ) if now is None else now.astimezone(_NY_TZ)
    return now_ny.date().isoformat()


def _ny_to_local_naive(day: date_t, t: dt_time, local_tz: ZoneInfo | None) -> datetime:
    """NY ローカル時刻(day, t) をマシンローカル(既定)または指定 tz の naive datetime に変換する。

    recorder は datetime.now()（マシンローカル naive）と比較するので、NY 時刻をマシンローカルの
    naive に落とす。local_tz を明示すると決定的に計算できる（テスト用。既定はマシンローカル）。
    """
    aware = datetime.combine(day, t, tzinfo=_NY_TZ)
    local = aware.astimezone(local_tz) if local_tz is not None else aware.astimezone()
    return local.replace(tzinfo=None)


def us_session_segments(date_str: str, local_tz: ZoneInfo | None = None) -> list[tuple[datetime, datetime]]:
    """NY 09:30-16:00 を1区間として、マシンローカル naive datetime で返す（昼休みなし）。

    夏時間なら JST 22:30〜翌05:00、標準時間なら JST 23:30〜翌06:00 に相当する。
    """
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return [(
        _ny_to_local_naive(d, US_OPEN, local_tz),
        _ny_to_local_naive(d, US_CLOSE, local_tz),
    )]


def us_finalize_at(date_str: str, local_tz: ZoneInfo | None = None) -> datetime:
    """NY 16:05 相当の finalize 時刻をマシンローカル naive datetime で返す。"""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return _ny_to_local_naive(d, US_FINALIZE, local_tz)


def market_segments(cfg: dict, date_str: str, market: str) -> list[tuple[datetime, datetime]]:
    """market に応じたセッション区間（マシンローカル naive）を返す。JP は東証2区間、US は1区間。"""
    if market == "US":
        return us_session_segments(date_str)
    return session_segments(cfg, date_str)


def finalize_time(cfg: dict, date_str: str, market: str) -> datetime:
    """market に応じた finalize 時刻（マシンローカル naive）を返す。"""
    if market == "US":
        return us_finalize_at(date_str)
    return _combine(date_str, cfg["session"]["finalize_at"])


def build_subscription(codes: list[str], subtypes: list[str]) -> list:
    """config の subtype 文字列を futu SubType のリストに変換する。未知の型は無視。"""
    return [SUBTYPE_MAP[s] for s in subtypes if s in SUBTYPE_MAP]


def within_quota(n_codes: int, n_subtypes: int, quota: int = SUBSCRIPTION_QUOTA) -> bool:
    """購読総数（銘柄数 × subtype 数）がクォータ内か。"""
    return n_codes * n_subtypes <= quota


# ---------------- push ハンドラ（別スレッドで呼ばれる → queue に積むだけ） ----------------

class _RecorderState:
    """ハンドラとメインループで共有する軽量状態。"""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {"ticks": 0, "quotes": 0, "k1m": 0}
        self.last_data_monotonic: float = time.monotonic()
        # 書き込みスレッドの障害フラグ（ディスク満杯等）。0 でない＝live CSV への記録が
        # 取りこぼされている＝その日のデータが欠ける、を録画側から見えるようにする。
        self.writer_errors: int = 0
        self.last_writer_error: str | None = None


class _TickerHandler(TickerHandlerBase):
    def __init__(self, q: queue.Queue, state: _RecorderState) -> None:
        super().__init__()
        self.q = q
        self.state = state

    def on_recv_rsp(self, rsp):
        ret, data = super().on_recv_rsp(rsp)
        if ret != RET_OK:
            return ret, data
        for _, r in data.iterrows():
            self.q.put((
                "ticks",
                str(r["code"]),
                {
                    "time": r["time"],
                    "price": r["price"],
                    "volume": r["volume"],
                    "turnover": r["turnover"],
                    "direction": str(r["ticker_direction"]),
                    "seq": r["sequence"],
                    "tick_type": str(r["type"]),
                },
            ))
            self.state.counters["ticks"] += 1
        self.state.last_data_monotonic = time.monotonic()
        return ret, data


class _OrderBookHandler(OrderBookHandlerBase):
    def __init__(self, q: queue.Queue, state: _RecorderState) -> None:
        super().__init__()
        self.q = q
        self.state = state

    def on_recv_rsp(self, rsp):
        ret, data = super().on_recv_rsp(rsp)
        if ret != RET_OK:
            return ret, data
        # data は dict。Bid/Ask は [(price, volume, order_count, {}), ...]。L1 = 先頭。
        bid = data.get("Bid") or []
        ask = data.get("Ask") or []
        b = bid[0] if bid else (math.nan, 0)
        a = ask[0] if ask else (math.nan, 0)
        t = data.get("svr_recv_time_bid") or data.get("svr_recv_time_ask")
        if not t:
            t = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        self.q.put((
            "quotes",
            str(data["code"]),
            {"time": t, "bid": b[0], "ask": a[0], "bid_vol": b[1], "ask_vol": a[1]},
        ))
        self.state.counters["quotes"] += 1
        self.state.last_data_monotonic = time.monotonic()
        return ret, data


class _CurKlineHandler(CurKlineHandlerBase):
    """K_1M 購読時の受信カウント用（契約 bars1m は録画ティックから合成するので永続化しない）。"""

    def __init__(self, state: _RecorderState) -> None:
        super().__init__()
        self.state = state

    def on_recv_rsp(self, rsp):
        ret, data = super().on_recv_rsp(rsp)
        if ret == RET_OK:
            self.state.counters["k1m"] += len(data)
            self.state.last_data_monotonic = time.monotonic()
        return ret, data


class _StockQuoteHandler(StockQuoteHandlerBase):
    """SubType.QUOTE 購読時のカウント用。bid/ask を持たないため永続化はしない（ORDER_BOOK を使うこと）。"""

    def __init__(self, state: _RecorderState) -> None:
        super().__init__()
        self.state = state

    def on_recv_rsp(self, rsp):
        ret, data = super().on_recv_rsp(rsp)
        if ret == RET_OK:
            self.state.last_data_monotonic = time.monotonic()
        return ret, data


def _register_handlers(client: MoomooClient, subtypes: list[str], q: queue.Queue, state: _RecorderState) -> None:
    """購読する subtype に応じて必要なハンドラだけ登録する。"""
    if "TICKER" in subtypes:
        client.set_handler(_TickerHandler(q, state))
    if "ORDER_BOOK" in subtypes:
        client.set_handler(_OrderBookHandler(q, state))
    if "K_1M" in subtypes:
        client.set_handler(_CurKlineHandler(state))
    if "QUOTE" in subtypes:
        log.warning("SubType.QUOTE は bid/ask を持たない。L1 気配には ORDER_BOOK を使うこと。")
        client.set_handler(_StockQuoteHandler(state))


# ---------------- 書き込みスレッド ----------------

def _writer_loop(q: queue.Queue, sink: LiveCsvSink, stop_event: threading.Event,
                 state: _RecorderState | None = None) -> None:
    """queue を1本のスレッドで捌いて CSV に追記する（シンクを単一スレッド化してロック不要にする）。

    書き込み例外（ディスク満杯等）でスレッドが死ぬと、以後のティックが queue に滞留して
    メモリだけ増え、録画本体は最後まで気づかない（クラッシュ安全のための live CSV が黙って
    止まる）。例外はログして障害フラグに数え、ループは続ける（1件落としても録画は続ける方がマシ）。
    """
    while not stop_event.is_set() or not q.empty():
        try:
            kind, code, row = q.get(timeout=0.5)
        except queue.Empty:
            try:
                sink.flush()  # 手空きに flush してクラッシュ時の取りこぼしを減らす
            except Exception as e:  # noqa: BLE001 - flush 失敗でスレッドを殺さない
                _note_writer_error(state, "flush", e)
            continue
        cols = TICKS_COLUMNS if kind == "ticks" else QUOTES_COLUMNS
        try:
            sink.write(code, kind, cols, row)
        except Exception as e:  # noqa: BLE001 - 書き込み失敗でスレッドを殺さない
            _note_writer_error(state, f"write {kind}/{code}", e)


def _note_writer_error(state: _RecorderState | None, what: str, e: Exception) -> None:
    """書き込み失敗をログし、状態へ障害フラグを立てる（同期は GIL 下の単純加算で足りる）。"""
    log.error("録画の書き込みに失敗（%s）: %s", what, e)
    if state is not None:
        state.writer_errors += 1
        state.last_writer_error = f"{what}: {e}"


# ---------------- 購読 ----------------

def subscribe_all(client: MoomooClient, codes: list[str], sub_objs: list) -> tuple[list[str], list[tuple[str, str]]]:
    """銘柄単位で購読し、権限/クォータ失敗は記録して続行する。

    戻り値 (ok_codes, failed[(code, reason)])。全滅は呼び手が exit 1 を判断する。
    """
    ok: list[str] = []
    failed: list[tuple[str, str]] = []
    for code in codes:
        try:
            client.subscribe([code], sub_objs)
            ok.append(code)
        except NoPermissionError as e:
            failed.append((code, f"NoPermission: {e}"))
            log.warning("購読不可（権限）: %s - %s", code, e)
        except Exception as e:  # noqa: BLE001 - クォータ超過等も銘柄単位で握って続行
            failed.append((code, str(e)))
            log.warning("購読失敗: %s - %s", code, e)
    return ok, failed


# ---------------- 1分足フォールバック（yfinance） ----------------

def fallback_codes(date: str, codes: list[str], top_n: int) -> list[str]:
    """フォールバック保存の対象銘柄 = 購読銘柄 ∪ 松井Tickランキング上位 top_n。

    日次の bars1m 蓄積が moomoo 購読クォータ（JP は権限なしで実質無意味）に縛られて
    25 銘柄しか保存されていなかった是正（2026-07-21）。yfinance はクォータと無関係なので、
    ランキング CSV（当日→無ければ直近保存分）から広く取ってバックテストの母集団を増やす。
    """
    pool = [c for c in codes if c.startswith("JP.")]
    if top_n > 0:
        try:
            from scalplab.datafeed import jp_tick_ranking as jtr
            path = jtr.ranking_csv_path(date)
            if not path.exists():
                path = jtr.latest_saved_csv()
            if path is not None and path.exists():
                ranked = [r["code"] for r in jtr.load_ranking_csv(path)[:top_n]]
                pool += [c for c in ranked if c not in set(pool)]
        except Exception as e:  # noqa: BLE001 - ランキング欠損時は購読リストのみで続行
            log.warning("フォールバック銘柄のランキング拡張に失敗（購読リストのみで続行）: %s", e)
    return pool


def fetch_fallback_bars(date: str, codes: list[str]) -> int:
    """JP 銘柄の当日1分足を yfinance から取得し bars1m(source='yfinance') で保存する。

    ティック由来の bars1m が既にある銘柄は上書きしない（録画ティックが正）。書けた銘柄数を返す。
    """
    try:
        import yfinance as yf  # type: ignore[import-not-found]
    except ImportError:
        log.warning("yfinance 未導入のため 1分足フォールバックをスキップ")
        return 0

    import pandas as pd

    written = 0
    for code in codes:
        if not code.startswith("JP."):
            continue
        if bars1m_path(date, code).exists():
            continue  # ティック由来がある
        try:
            hist = yf.Ticker(moomoo_to_yf(code)).history(
                period="5d", interval="1m", auto_adjust=False
            )
        except Exception as e:  # noqa: BLE001
            log.warning("yfinance 1分足取得失敗 %s: %s", code, e)
            continue
        if hist is None or hist.empty:
            continue
        idx = hist.index
        # yfinance の index は取引所 tz-aware。JST naive に落として当日ぶんだけ残す。
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert("Asia/Tokyo").tz_localize(None)
        df = pd.DataFrame({
            "time": idx,
            "open": hist["Open"].to_numpy(),
            "high": hist["High"].to_numpy(),
            "low": hist["Low"].to_numpy(),
            "close": hist["Close"].to_numpy(),
            "volume": hist["Volume"].to_numpy(),
            "turnover": (hist["Close"] * hist["Volume"]).to_numpy(),
            "source": "yfinance",
        })
        df = df[df["time"].dt.strftime("%Y-%m-%d") == date]
        if df.empty:
            continue
        write_parquet(enforce_bars1m(df), bars1m_path(date, code))
        written += 1
    log.info("1分足フォールバック: %d 銘柄を yfinance から保存", written)
    return written


# ---------------- 録画ループ ----------------

def _walkforward_after_finalize(date: str, market: str, duration: float | None) -> None:
    """US セッション録画の finalize 後に、チャンピオンパラメータのウォークフォワード検証を回す。

    その夜の新しい録画に対する純アウトオブサンプル評価を積み上げる（reports/walkforward.*）。
    録画の後始末（finally）の中から呼ばれるため、失敗しても例外を外へ出さない（ログのみ）。
    exit code・parquet 変換など既存のクリーンアップに一切影響させないのが不変条件。
    アドホック（duration 指定）と JP モードでは実行しない。
    """
    if market != "US" or duration is not None:
        return
    try:
        from scalplab.backtest.walkforward import run_walkforward

        res = run_walkforward(date)
        if res is None:
            log.info("ウォークフォワード検証: 対象なし/冪等スキップ（date=%s）", date)
        else:
            ov = res["overall"]
            log.info("ウォークフォワード検証: date=%s hash=%s n=%s win60=%.3f exp60_net=%.2fbp",
                     date, res["params_hash"], ov["n"],
                     ov["win60"] if ov["win60"] == ov["win60"] else float("nan"),
                     ov["exp60_net_bp"] if ov["exp60_net_bp"] == ov["exp60_net_bp"] else float("nan"))
    except Exception as e:  # noqa: BLE001 - 録画終了処理を絶対に壊さない
        log.error("ウォークフォワード検証に失敗（録画終了処理は継続）: %s", e)


def _walkforward_jp_after_finalize(date: str, market: str, duration: float | None) -> None:
    """JP セッション録画の finalize（+ yfinance 1分足保存）後に、日本株チャンピオンの
    ウォークフォワード検証を回す（reports/walkforward-jp.*）。

    US 版 (_walkforward_after_finalize) と同じ非干渉パターン: 録画の後始末（finally）から
    呼ばれるため失敗しても例外を外へ出さない（ログのみ）。exit code・parquet 変換・
    1分足フォールバックに一切影響させないのが不変条件。アドホック（duration 指定）と US では実行しない。
    bars1m がまだ無い日は各処理が graceful skip する。walkforward → autotune の順に回し、
    それぞれ独立の try/except で握る（片方が失敗しても他方・録画後始末に波及させない）。
    """
    if market != "JP" or duration is not None:
        return
    try:
        from scalplab.backtest.walkforward import run_walkforward_jp

        res = run_walkforward_jp(date)
        if res is None:
            log.info("JP ウォークフォワード検証: 対象なし/冪等スキップ（date=%s）", date)
        else:
            ov = res["overall"]
            first = res["eval_bars"][0]
            log.info("JP ウォークフォワード検証: date=%s hash=%s n=%s win%d=%.3f exp%d_net=%.2fbp",
                     date, res["params_hash"], ov["n"], first,
                     ov[f"win{first}"] if ov[f"win{first}"] == ov[f"win{first}"] else float("nan"),
                     first,
                     ov[f"exp{first}_net_bp"] if ov[f"exp{first}_net_bp"] == ov[f"exp{first}_net_bp"] else float("nan"))
    except Exception as e:  # noqa: BLE001 - 録画終了処理を絶対に壊さない
        log.error("JP ウォークフォワード検証に失敗（録画終了処理は継続）: %s", e)

    # walkforward の直後に夜間自動チューナー（挑戦者追跡）を回す。数分かかりうるが録画は既に終わっており影響しない。
    try:
        from scalplab.backtest.autotune import run_autotune

        ares = run_autotune(date)
        if ares is None:
            log.info("JP 自動チューナー: 対象なし/冪等スキップ（date=%s）", date)
        else:
            log.info("JP 自動チューナー: date=%s combos=%d codes=%d 差し替え候補=%d",
                     ares["date"], ares["n_combos"], ares["n_codes"], len(ares["challengers"]))
    except Exception as e:  # noqa: BLE001 - 録画終了処理を絶対に壊さない
        log.error("JP 自動チューナーに失敗（録画終了処理は継続）: %s", e)


def _jquants_daily_after_finalize(date: str, market: str, duration: float | None, cfg: dict) -> None:
    """JP セッション finalize（yfinance 1分足保存の後）に J-Quants 分足の当日取り込みを回す。

    US 版フック（_walkforward_after_finalize 等）と同じ非干渉パターン: 録画の後始末（finally）から
    呼ばれるため失敗しても例外を外へ出さない（ログのみ）。exit code・parquet 変換・1分足フォールバック・
    後続の JP ウォークフォワードに一切影響させない。API キー未設定・jquants_daily_top_n=0 のときは
    1 行 INFO でスキップ（無害）。アドホック（duration 指定）と US では実行しない。
    """
    if market != "JP" or duration is not None:
        return
    top_n = int(cfg.get("jquants_daily_top_n", 10))
    if top_n <= 0:
        log.info("J-Quants 分足の日次取り込み: 無効化中（jquants_daily_top_n=0）")
        return
    try:
        from scalplab.datafeed.jquants_bars import run_daily

        res = run_daily(top_n=top_n)
        if res.get("skipped"):
            log.info("J-Quants 分足の日次取り込み: スキップ（%s）", res["skipped"])
        else:
            log.info("J-Quants 分足の日次取り込み: 新規 %d 日 / スキップ %d 日 / 銘柄 %d",
                     res.get("written", 0), res.get("skipped_days", 0), res.get("codes", 0))
    except Exception as e:  # noqa: BLE001 - 録画終了処理を絶対に壊さない
        log.error("J-Quants 分足の日次取り込みに失敗（録画終了処理は継続）: %s", e)


def _run_loop(state: _RecorderState, client: MoomooClient, ok_codes: list[str],
              *, until: datetime | None, duration: float | None,
              segments: list[tuple[datetime, datetime]] | None,
              summary_interval: int, stale_timeout: int) -> None:
    """録画メインループ。until（既定モード）か duration 秒（アドホック）まで回す。

    - summary_interval 毎に受信サマリーを1行出す（沈黙させない）。
    - セッション中に stale_timeout 秒データが来なければ再接続を試みる（アドホックは常時監視）。
    """
    start_mono = time.monotonic()
    last_summary = start_mono
    last_counts = dict(state.counters)

    def _should_continue() -> bool:
        if duration is not None:
            return (time.monotonic() - start_mono) < duration
        return datetime.now() < until

    while _should_continue():
        time.sleep(1.0)
        now_mono = time.monotonic()

        if now_mono - last_summary >= summary_interval:
            c = state.counters
            d_ticks = c["ticks"] - last_counts["ticks"]
            d_quotes = c["quotes"] - last_counts["quotes"]
            log.info(
                "録画中 銘柄=%d ticks=%d(+%d) quotes=%d(+%d) k1m=%d",
                len(ok_codes), c["ticks"], d_ticks, c["quotes"], d_quotes, c["k1m"],
            )
            last_summary = now_mono
            last_counts = dict(c)

        # watchdog: セッション中（アドホックは常時）にデータが途切れたら再接続
        active = True if segments is None else is_in_session(datetime.now(), segments)
        if active and (now_mono - state.last_data_monotonic) > stale_timeout:
            log.warning("データ停止を検知（%ds 無受信）。再接続を試みる", stale_timeout)
            if client.reconnect():
                state.last_data_monotonic = time.monotonic()


# ---------------- エントリポイント ----------------

def _setup_logging(date: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger("scalplab")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.FileHandler(LOG_DIR / f"recorder-{date}.log")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def _connect_with_retry(client: MoomooClient, cfg: dict, date: str, market: str,
                        duration: float | None) -> None:
    """OpenD へ接続する。既定モードでは finalize 時刻まで CONNECT_RETRY_SEC 毎に粘る。

    OpenD は GUI アプリで手動起動・再ログインを要するため、接続不可で即 exit 1 すると
    launchd の KeepAlive が 10 秒周期でプロセスごと再起動し、そのたびに後処理（finalize と
    1分足フォールバック）が空振りする。2026-07-28 の OpenD 停止では、この周期実行が
    場中に yfinance を叩いて JP bars1m を数行の断片で上書きし、当日データを失わせた。
    プロセス内で待てば周期実行が消え、場中に OpenD が復活した時点から録画を継続できる。
    アドホック（duration 指定）は検証用なので従来どおり即例外。
    """
    try:
        client.connect()
        return
    except OpenDUnavailableError:
        if duration is not None:
            raise
    deadline = finalize_time(cfg, date, market)
    log.error("OpenD へ接続できない。%s まで %.0f 秒毎に再接続を試す（moomoo_OpenD.app の起動待ち）",
              deadline.strftime("%H:%M"), CONNECT_RETRY_SEC)
    while datetime.now() < deadline:
        time.sleep(min(CONNECT_RETRY_SEC, max(1.0, (deadline - datetime.now()).total_seconds())))
        try:
            client.connect()
        except OpenDUnavailableError:
            continue
        log.info("OpenD へ再接続できた。録画を続行する。")
        return
    raise OpenDUnavailableError(
        f"finalize 時刻 {deadline.strftime('%H:%M')} まで OpenD へ接続できなかった"
    )


def record(date: str, codes: list[str], cfg: dict, *,
           duration: float | None = None, market: str = "JP") -> int:
    """録画を実行して exit code を返す。duration 指定でアドホック（市場時間無視）。

    market="US" のときはセッション区間・finalize 時刻を NY 基準（zoneinfo）で計算する。
    JP パス（既定）は従来どおり JST naive のまま。
    """
    subtypes = cfg["subtypes"]
    sub_objs = build_subscription(codes, subtypes)
    if not within_quota(len(codes), len(sub_objs)):
        log.warning(
            "購読総数 %d がクォータ %d を超過。一部購読が失敗する可能性。",
            len(codes) * len(sub_objs), SUBSCRIPTION_QUOTA,
        )

    state = _RecorderState()
    q: queue.Queue = queue.Queue()
    sink = LiveCsvSink(date)
    stop_event = threading.Event()
    writer = threading.Thread(target=_writer_loop, args=(q, sink, stop_event, state), daemon=True)

    client = MoomooClient()
    exit_code = 0
    connected = False
    try:
        _connect_with_retry(client, cfg, date, market, duration)
        connected = True
        _register_handlers(client, subtypes, q, state)
        writer.start()

        ok_codes, failed = subscribe_all(client, codes, sub_objs)
        log.info("購読成功 %d / 失敗 %d（subtypes=%s）", len(ok_codes), len(failed), subtypes)
        if not ok_codes and duration is not None:
            log.error("全銘柄の購読に失敗。録画を中止する。")
            exit_code = 1
            return exit_code
        if not ok_codes:
            # 既定モード: 日本株の気配権限が日中に付与される可能性（アプリ側での有効化）に備え、
            # finalize 時刻まで 10 分毎に購読を再試行する。最後まで失敗しても finally の
            # 1分足フォールバックがその日のデータを拾えるよう、ここでは終了しない。
            finalize_dt = finalize_time(cfg, date, market)
            log.error("全銘柄の購読に失敗。%s まで10分毎に再試行する（権限付与待ち）",
                      finalize_dt.strftime("%H:%M"))
            while datetime.now() < finalize_dt and not ok_codes:
                time.sleep(min(600.0, max(1.0, (finalize_dt - datetime.now()).total_seconds())))
                ok_codes, failed = subscribe_all(client, codes, sub_objs)
            if not ok_codes:
                log.error("引け時刻まで購読権限が付与されなかった。")
                exit_code = 1
                return exit_code
            log.info("購読再試行に成功: %d 銘柄", len(ok_codes))

        if duration is not None:
            log.info("アドホック録画: %d 銘柄 × %.0fs", len(ok_codes), duration)
            _run_loop(state, client, ok_codes,
                      until=None, duration=duration, segments=None,
                      summary_interval=cfg["summary_interval_sec"],
                      stale_timeout=cfg["stale_timeout_sec"])
        else:
            segments = market_segments(cfg, date, market)
            finalize_dt = finalize_time(cfg, date, market)
            first_start = segments[0][0]
            now = datetime.now()
            if now < first_start:
                wait_s = (first_start - now).total_seconds()
                log.info("寄り付き待機: %s まで %.0f 秒待つ", first_start.strftime("%H:%M"), wait_s)
                # 中断可能な待機（1秒刻み）
                while datetime.now() < first_start:
                    # while 判定後に時刻を取り直すので、境界で残り <=0 になると sleep(負値) が
                    # ValueError になる。他の待機箇所（:587/:643）と同じく下限で守る。
                    time.sleep(min(5.0, max(0.05, (first_start - datetime.now()).total_seconds())))
            log.info("録画開始: %d 銘柄、%s に finalize", len(ok_codes), finalize_dt.strftime("%H:%M"))
            _run_loop(state, client, ok_codes,
                      until=finalize_dt, duration=None, segments=segments,
                      summary_interval=cfg["summary_interval_sec"],
                      stale_timeout=cfg["stale_timeout_sec"])
        return exit_code
    except OpenDUnavailableError as e:
        log.error("OpenD へ接続できず録画を開始できない: %s", e)
        return 1
    finally:
        # 購読解除 → 書き込みスレッド停止 → live をドレイン → parquet 変換
        # OpenD へ一度も接続できなかった場合は live に何も書かれておらず writer も動いていない。
        # 未接続のまま unsubscribe/finalize を呼んでも WARNING と空の結果を撒くだけなので飛ばす。
        if connected:
            try:
                client.unsubscribe_all()
            except Exception:  # noqa: BLE001
                pass
        stop_event.set()
        if writer.is_alive():
            writer.join(timeout=10)
        sink.close()
        client.close()
        if connected:
            log.info("受信合計 ticks=%d quotes=%d k1m=%d", state.counters["ticks"], state.counters["quotes"], state.counters["k1m"])
            results = finalize_date(date, cleanup=True)
            for code, counts in sorted(results.items()):
                parts = ", ".join(f"{k}={v}" for k, v in counts.items())
                log.info("finalize %s: %s", code, parts)
            log.info("finalize 完了: %d 銘柄", len(results))
            # US セッションの finalize 成功後にチャンピオンパラメータのウォークフォワード検証を回す。
            # 失敗しても録画の後始末・exit code には影響させない（ヘルパ内で例外を握る）。
            _walkforward_after_finalize(date, market, duration)
        # 1分足フォールバックは OpenD が落ちていた日も引け後に 1 回だけ走らせる（JP バックテストの
        # 唯一のデータ源のため）。未接続時は _connect_with_retry が finalize 時刻まで粘った後に
        # ここへ来るので、場中の断片で上書きすることはない。アドホックの接続失敗時のみ抑止する。
        if cfg.get("fallback_bars") and (connected or duration is None):
            fetch_fallback_bars(date, fallback_codes(date, codes, int(cfg.get("fallback_top_n", 100))))
        # yfinance フォールバックの直後に J-Quants 分足の当日取り込みを回す（公式データで置換）。
        # API キー未設定なら 1 行 INFO でスキップ（無害）。US 版フックと同じ非干渉パターン。
        _jquants_daily_after_finalize(date, market, duration, cfg)
        # JP は yfinance 1分足保存の完了後に日本株チャンピオンのウォークフォワードを回す
        # （bars1m が揃ってから読むため fetch_fallback_bars の後に置く）。US 版と同じ非干渉パターン。
        _walkforward_jp_after_finalize(date, market, duration)


def main() -> int:
    parser = argparse.ArgumentParser(description="ティック録画デーモン")
    parser.add_argument("--market", choices=["JP", "US"], default="JP",
                        help="録画市場。JP=東証セッション（既定）/ US=米国東部セッション")
    parser.add_argument("--date", default=None,
                        help="録画日 YYYY-MM-DD。未指定なら JP=JST 当日 / US=NY セッション日")
    parser.add_argument("--codes", help="アドホック録画の code（カンマ区切り。例 US.AAPL,US.TSLA）")
    parser.add_argument("--duration", type=float, help="アドホック録画の秒数（--codes と併用、市場時間無視）")
    parser.add_argument("--fallback-bars", action="store_true", help="セッション後に yfinance 1分足を補完")
    args = parser.parse_args()

    market = args.market
    # US はセッション日を NY 現在日付にする（JST 深夜0時をまたいでも同一セッション日に保存）。
    if args.date is not None:
        date = args.date
    elif market == "US":
        date = us_session_day()
    else:
        date = datetime.now().strftime("%Y-%m-%d")
    _setup_logging(date)
    cfg = load_config()
    if args.fallback_bars:
        cfg["fallback_bars"] = True

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        duration = args.duration if args.duration is not None else 60.0
        return record(date, codes, cfg, duration=duration, market=market)

    # 引け後の起動は何もせず正常終了する。launchd の KeepAlive(SuccessfulExit=false) が
    # 「権限なしで exit 1 → 即再起動」を夜通し繰り返すのを防ぐガード。
    if datetime.now() >= finalize_time(cfg, date, market):
        log.info("finalize 時刻を過ぎた起動のため何もしない（market=%s date=%s）", market, date)
        return 0

    codes = resolve_codes_us(date) if market == "US" else resolve_codes(date)
    if not codes:
        cfg_name = "config/universe_us.yaml" if market == "US" else "config/universe.yaml"
        log.error("universe が空。%s を確認。", cfg_name)
        return 1
    return record(date, codes, cfg, market=market)


if __name__ == "__main__":
    raise SystemExit(main())
