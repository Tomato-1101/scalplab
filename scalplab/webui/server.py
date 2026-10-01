"""Web UI サーバ本体（stdlib http.server）とチャートデータ組み立てロジック。

設計の要点:
  - 追加 pip 依存を増やさないため HTTP は http.server.ThreadingHTTPServer + json のみ。
  - チャートに載せる指標は **すべてサーバ側で既存モジュールを使って計算**する
    （hist1m は strategy.rci_impulse_bar、ティックは backtest.engine を再利用）。JS 側で
    指標を再実装しないので、表示とバックテストの数値が構造的に一致する。
  - データアクセスは history.load_cache（hist1m）と store.loader（ticks）に委譲する。
    これらはモジュール変数（HIST_DIR / DATA_DIR）でパスを解決するので、テストは合成
    parquet を書いた一時ディレクトリへ差し替えるだけでサーバ起動なしに検証できる。

エントリー計算のデータソース別方針:
  - hist1m: 確定 1 分足のバー近似版（rci_impulse_bar.bar_signals）をセッション日ごとに実行。
  - bars1m: 自分で毎営業日 15:35 に保存する日本株の 1 分足（data/bars1m/DATE/CODE.parquet, JP.*）。
            hist1m と同じバー近似版をセッション日（＝保存日）ごとに実行する。ただし日本株は
            yfinance 由来で既に東証セッションのみのため RTH フィルタ（米国東部）は掛けない。
  - ticks:  ティック駆動 engine（run_backtest, 足内 RCI）を code×date で実行。
RCI 表示は短期 9・中期 14 を常に返す（指定）。戦略トリガーは既定 rci_period=9 のまま。

軸ズレ対策（メインチャートと RCI ペインの横軸同期）:
  指標系列（rci9/rci14/sar/sma）は **ローソクと同一の time 集合**で返し、ウォームアップ区間は
  value を持たない whitespace 点（{"time": t} のみ）として渡す。こうすると両ペインの
  time→論理インデックス対応が一致し、フロントの論理レンジ同期で同時刻が縦に揃う（間引くと
  下段の時間集合がローソクと不一致になり、2 チャート間で視覚的にズレる）。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import threading
import traceback
from collections import OrderedDict
from datetime import date as date_t
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

import numpy as np
import pandas as pd

from scalplab import annotations
from scalplab.backtest import metrics
from scalplab.backtest import regime as regime_mod
from scalplab.backtest import screen as screen_mod
from scalplab.backtest.engine import DEFAULT_HORIZONS, run_backtest
from scalplab.backtest.screen import filter_rth, iter_session_days
from scalplab.datafeed import history, universe
from scalplab.indicators.atr import atr_series
from scalplab.indicators.parabolic import sar_series
from scalplab.indicators.rci import rci_series
from scalplab.store import loader as store_loader
from scalplab.strategy.rci_impulse_bar import bar_attention, bar_signals

# --- 定数・既定値 -----------------------------------------------------------

STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
JP_NAMES_CSV = PROJECT_ROOT / "config" / "jp_names.csv"   # 日本株コード→社名マスタ
REPORTS_DIR = PROJECT_ROOT / "reports"                    # スクリーニング結果 run 置き場

RCI_SHORT = 9   # 表示・トリガー共通の短期
RCI_LONG = 14   # 表示専用の中期（指定）

DEFAULT_HIST_DAYS = 3          # bars1m/hist1m の 1 ページ日数（初期＝直近 N セッション、過去方向も N 日ずつ）
TICK_PAGE_DAYS = 1             # ticks は 1 日が重いので 1 ページ＝1 日（スクロールで 1 日ずつ遡る）
DEFAULT_TICK_TF = 10           # ティック既定足種（秒）。指定（US.* は ticks・既定 10s）
TICK_SLOPE_WINDOW_SEC = 20     # ティック角度の窓（秒）。UI「角度」ON の既定 30→20（指定）
DEFAULT_SMA = [5, 25]          # SMA は 5/25 固定（UI から期間入力を撤去。指定）
DEFAULT_COST_BP = 3.0
DEFAULT_BOLL_WINDOW = 20        # ボリンジャーバンド描画の窓（本数。戦略 fade_boll_window 既定と一致）
DEFAULT_BOLL_Z = 3.0           # バンド描画の z（fade_boll_z が>0ならその値・無効時はこの既定を使う）
DEFAULT_AF = (0.02, 0.02, 0.2)  # SAR 加速因子（UI では固定。strategy 既定と一致させる）

# code / date はそのまま data/<kind>/<date>/<code>.parquet に結合されるので、パス結合前に書式を縛る
# （"/" も ".." も構造的に入らない形にして、data/ 配下の外を読ませない）。
CODE_RE = re.compile(r"^[A-Za-z]{2}\.[A-Za-z0-9._-]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ローカル専用 UI。ここ以外の Host / Origin は拒否する（CSRF・DNS rebinding 対策）。
LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})

CHART_CACHE_MAX = 16   # (種別, code, date, tf/params, ファイル版) 単位のメモリキャッシュ上限

# 戦略パラメータの既定（source ごとに単位が違う: hist1m/bars1m=バー本数, ticks=秒）
# prev_diff_min は「一個前の RCI 確定値との差（尖り）」の下限。0 で無効（既定＝従来挙動）。
HIST_DEFAULTS = {"slope_threshold": 15.0, "prev_diff_min": 0.0, "eff_min": 2.0, "cooldown": 3, "cost_bp": DEFAULT_COST_BP}
BARS1M_DEFAULTS = {"slope_threshold": 15.0, "prev_diff_min": 0.0, "eff_min": 2.0, "cooldown": 3, "cost_bp": DEFAULT_COST_BP}
TICK_DEFAULTS = {"slope_threshold": 15.0, "prev_diff_min": 0.0, "eff_min": 3.0, "cooldown": 60, "cost_bp": DEFAULT_COST_BP}
DEFAULT_ENTRY_CODE = "US.SOXL"   # 初回表示（空画面で放置しない）
DEFAULT_TICK_CODE = "US.NVDA"
DEFAULT_BARS1M_CODE = "JP.7203"  # 日本株の初期表示（無ければ先頭銘柄）


# --- 小さなユーティリティ ---------------------------------------------------

def _epoch(ts) -> int:
    """naive datetime を UNIX 秒に写す。

    本プロジェクトの time は「市場ローカルの naive 時刻」。lightweight-charts は数値時刻を
    UTC として整形するため、naive を UTC とみなして epoch 化すると、画面には市場ローカルの
    壁時計時刻がそのまま出る（tz 変換を挟まないので表示と録画時刻が一致する）。
    """
    return int(pd.Timestamp(ts).value // 1_000_000_000)


def _num(v):
    """JSON 化可能な float に正規化する（NaN/Inf は None）。"""
    if v is None:
        return None
    f = float(v)
    if not math.isfinite(f):
        return None
    return f


def _round(v, nd):
    n = _num(v)
    return None if n is None else round(n, nd)


def _project_data_dir() -> Path:
    """data/ ディレクトリ（sources 走査用）。loader と同じルート解決を使う。"""
    return store_loader.DATA_DIR


def _under_data_dir(path: Path) -> bool:
    """解決後のパスが data/ 配下かを返す（CODE_RE / DATE_RE 検証の二重化）。"""
    try:
        path.resolve().relative_to(_project_data_dir().resolve())
    except ValueError:
        return False
    return True


def _validate_query_ids(q: dict) -> None:
    """パス結合に使う code / 日付クエリの書式を検証する（不正は ValueError→400）。"""
    code = (q.get("code") or "").strip()
    if code and not CODE_RE.fullmatch(code):
        raise ValueError(f"code の書式が不正です（例 JP.7203 / US.SOXL）: {code!r}")
    for k in ("date", "from", "to", "before"):
        v = (q.get(k) or "").strip()
        if v and not DATE_RE.fullmatch(v):
            raise ValueError(f"{k} の書式が不正です（YYYY-MM-DD）: {v!r}")


def _bars1m_index() -> tuple[dict[str, list[str]], list[str]]:
    """data/bars1m/DATE/ を走査し、日本株（JP.*）の (日付→code一覧, code和集合) を返す。

    日本株 1 分足は毎営業日 15:35 に日付ディレクトリへ保存される。US 等の混在ファイルは
    除外し JP.* だけを対象にする（本ソースは日本株専用）。
    """
    root = _project_data_dir() / "bars1m"
    per_date: dict[str, list[str]] = {}
    codes: set[str] = set()
    if root.exists():
        for d in sorted((x for x in root.iterdir() if x.is_dir()), key=lambda p: p.name):
            jp = sorted(f.stem for f in d.glob("*.parquet") if f.stem.startswith("JP."))
            if jp:
                per_date[d.name] = jp
                codes.update(jp)
    return per_date, sorted(codes)


def _ticks_dates_for(code: str) -> list[str]:
    """data/ticks/DATE/ を走査し、その code のティックが存在する日付（昇順）を返す。"""
    root = _project_data_dir() / "ticks"
    dates: list[str] = []
    if code and root.exists():
        for d in sorted((x for x in root.iterdir() if x.is_dir()), key=lambda p: p.name):
            if (d / f"{code}.parquet").exists():
                dates.append(d.name)
    return dates


def _resolve_source_and_dates(code: str) -> tuple[str, list[str]]:
    """code だけからデータソースと利用可能な保存日（昇順）を自動解決する。

    UI にはソース区別（日本株/米国株/ティック）を見せない方針（指定）。ルール:
      - JP.* → bars1m（自前保存の日本株 1 分足。全保存日が対象）。
      - US.* → ティック録画があれば ticks（足種選択が有効）、無ければ hist1m（過去 1 分足）。
    """
    code = (code or "").strip()
    if code.startswith("JP."):
        per_date, _ = _bars1m_index()
        dates = sorted(d for d, codes in per_date.items() if code in codes)
        return "bars1m", dates
    tick_dates = _ticks_dates_for(code)
    if tick_dates:
        return "ticks", tick_dates
    cache = history.load_cache(code)
    if not cache.empty:
        dates = sorted({d.isoformat() for d in pd.to_datetime(cache["time"]).dt.date})
        return "hist1m", dates
    return "hist1m", []  # データ無し。各ビルダが 200 + メッセージで返す


def _default_page_days(source: str) -> int:
    """ソース別の 1 ページ日数（ticks だけ 1 日、他は DEFAULT_HIST_DAYS）。"""
    return TICK_PAGE_DAYS if source == "ticks" else DEFAULT_HIST_DAYS


def _page_from_query(all_dates: list[str], q: dict, default_days: int) -> tuple[list[str], bool]:
    """all_dates（昇順）から before/days に基づく表示ページ（昇順）と has_more を返す。

    before が無ければ最新側から days 日、before があればその日付より前の days 日を返す
    （過去方向の遅延読み込み。UI はチャート左端に近づくと before=最古表示日 で次ページを取る）。
    has_more は「返したページより古い保存日がまだ存在するか」。
    """
    days = _get_int(q, "days", default_days)
    before = q.get("before") or None
    candidate = [d for d in all_dates if d < before] if before else list(all_dates)
    page = candidate[-days:] if days > 0 else candidate
    has_more = len(candidate) > len(page)
    return page, has_more


def _load_jp_names() -> dict[str, str]:
    """config/jp_names.csv（ヘッダ code,name）を読み、code→社名の辞書を返す。

    J-Quants の上場銘柄マスタから親が生成する（docs/data-contract.md 参照）。ファイルが
    無い・壊れている場合は空辞書を返し、UI はコードのまま表示にフォールバックする。
    """
    if not JP_NAMES_CSV.exists():
        return {}
    try:
        df = pd.read_csv(JP_NAMES_CSV, dtype=str)
    except Exception:  # noqa: BLE001 - マスタ破損時は社名なしで運用継続（コード表示）
        return {}
    if "code" not in df.columns or "name" not in df.columns:
        return {}
    out: dict[str, str] = {}
    for code, name in zip(df["code"], df["name"]):
        if isinstance(code, str) and isinstance(name, str) and name.strip():
            out[code.strip()] = name.strip()
    return out


# --- サイドバー並び順（毎日のランキングで自然に変わる） --------------------
#
# 米国株は前回録画セッションの実ティック数（＝その日どれだけ活発だったか）の降順、
# 日本株は直近 universe yaml（売買代金ランキング順で書かれている）の順で並べる。
# どちらも「毎日の新データ」で並びが自然に変わり、上位から見られるようにする狙い。

def _latest_jp_universe_order() -> list[str]:
    """直近の日本株 universe yaml（data/universe/YYYY-MM-DD.yaml の最新）の codes 順を返す。

    この yaml は universe.build が売買代金ランキングの降順で書き出すので、日本株サイドバーの
    並び順にそのまま使える。US-*.yaml・master.csv 等は対象外（JP 日次 yaml のみを拾う）。
    yaml が無い・壊れている場合は空リストを返す（呼び手はコード昇順にフォールバックする）。
    """
    import re

    root = _project_data_dir() / "universe"
    if not root.exists():
        return []
    pat = re.compile(r"^\d{4}-\d{2}-\d{2}\.yaml$")
    files = sorted(p for p in root.glob("*.yaml") if pat.match(p.name))
    if not files:
        return []
    try:
        import yaml

        doc = yaml.safe_load(files[-1].read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - yaml 破損時はランキング無し（コード順にフォールバック）
        return []
    return [str(c) for c in (doc.get("codes") or [])]


def _order_jp_codes_by_universe(codes: list[str]) -> list[str]:
    """日本株コードを直近 universe yaml 順に並べ替える（Tick回数ランキング未取得時のフォールバック）。

    yaml に載る銘柄をその順で前へ、載らない銘柄はコード昇順で末尾に置く。
    """
    order = _latest_jp_universe_order()
    rank = {c: i for i, c in enumerate(order)}
    ranked = sorted((c for c in codes if c in rank), key=lambda c: rank[c])
    rest = sorted(c for c in codes if c not in rank)
    return ranked + rest


def _latest_jp_tick_ranking_order() -> list[str]:
    """直近保存の松井 Tick回数ランキング CSV の code 順（moomoo コード）を返す。無ければ空。

    jp_tick_ranking の保存規約（data/universe/jp-tick-ranking-YYYY-MM-DD.csv）を単一の
    真実として再利用する（ファイル名パターンをここで二重定義しない）。
    """
    from ..datafeed import jp_tick_ranking

    path = jp_tick_ranking.latest_saved_csv(_project_data_dir() / "universe")
    if path is None:
        return []
    return [r["code"] for r in jp_tick_ranking.load_ranking_csv(path)]


def _order_jp_codes(codes: list[str]) -> list[str]:
    """日本株コードを最新の Tick回数ランキング順に並べ替える。

    ランキングに載る銘柄をその順で前へ、載らない銘柄は末尾に現行順（universe yaml 順→コード昇順）で
    置く。ランキング CSV がまだ無い環境では従来どおり universe yaml 順にフォールバックする。
    """
    ranking = _latest_jp_tick_ranking_order()
    if not ranking:
        return _order_jp_codes_by_universe(codes)
    rank = {c: i for i, c in enumerate(ranking)}
    ranked = sorted((c for c in codes if c in rank), key=lambda c: rank[c])
    rest = _order_jp_codes_by_universe([c for c in codes if c not in rank])
    return ranked + rest


def _order_us_codes(codes: list[str]) -> list[str]:
    """米国株コードを直近録画セッションの実ティック数の降順に並べ替える。

    universe.py の既存ロジック（latest_tick_session / tick_counts）を再利用する。録画のある
    銘柄をティック数降順（同数は入力順＝コード昇順）で前へ、録画の無い銘柄はコード昇順で末尾。
    ticks の探索は loader と同じ DATA_DIR を使う（テストの一時ディレクトリ差し替えに追従する）。
    """
    ticks_dir = _project_data_dir() / "ticks"
    session = universe.latest_tick_session(ticks_dir=ticks_dir)
    counts = universe.tick_counts(session, ticks_dir=ticks_dir) if session else {}
    recorded = [c for c in codes if counts.get(c, 0) > 0]
    recorded.sort(key=lambda c: counts.get(c, 0), reverse=True)  # 安定ソート：同数はコード昇順を保つ
    rest = sorted(c for c in codes if counts.get(c, 0) <= 0)
    return recorded + rest


# --- sources（利用可能な銘柄・日付） ----------------------------------------

def _latest_aptitude_hints() -> dict:
    """最新スクリーン結果から JP 銘柄の適性バッジ（◎/○ のみ）を code→badge で返す（軽い実装）。

    サイドバーに「この銘柄は手法が効きやすい」印を出すためのヒント。× や判定不能は載せない（印は付けない）。
    スクリーン結果が無ければ空 dict。銘柄ごとの再計算はせず最新 run の ranking.csv を1回読むだけ。
    """
    latest = list_screens().get("latest")
    if not latest:
        return {}
    try:
        df = pd.read_csv(REPORTS_DIR / latest / "ranking.csv")
    except Exception:  # noqa: BLE001 - 読めなければヒント無し（サイドバーは印なしで機能する）
        return {}
    hints: dict[str, str] = {}
    for _i, r in df.iterrows():
        badge = (str(r["aptitude"]) if ("aptitude" in df.columns and pd.notna(r.get("aptitude")))
                 else screen_mod.aptitude_badge(_num(r.get("range_med_bp"))))
        if badge in ("◎", "○"):
            hints[str(r["code"])] = badge
    return hints


def list_sources() -> dict:
    """データソース一覧と初期表示の既定を返す。"""
    hist_codes = sorted(p.stem for p in history.HIST_DIR.glob("*.parquet")) if history.HIST_DIR.exists() else []

    ticks_root = _project_data_dir() / "ticks"
    ticks: dict[str, list[str]] = {}
    if ticks_root.exists():
        for d in sorted((x for x in ticks_root.iterdir() if x.is_dir()), key=lambda p: p.name):
            codes = sorted(f.stem for f in d.glob("*.parquet"))
            if codes:
                ticks[d.name] = codes

    tick_dates = sorted(ticks.keys())
    latest_tick_date = tick_dates[-1] if tick_dates else None
    tick_code = None
    if latest_tick_date:
        codes = ticks[latest_tick_date]
        tick_code = DEFAULT_TICK_CODE if DEFAULT_TICK_CODE in codes else codes[0]

    hist_entry_code = DEFAULT_ENTRY_CODE if DEFAULT_ENTRY_CODE in hist_codes else (hist_codes[0] if hist_codes else None)

    # 日本株 1 分足（bars1m/JP.*）。銘柄は全保存日の和集合、期間は直近 DEFAULT_HIST_DAYS 日を既定に。
    # サイドバー表示順は直近 universe yaml（売買代金ランキング順）に並べ替える。
    bars1m_per_date, bars1m_codes = _bars1m_index()
    bars1m_codes = _order_jp_codes(bars1m_codes)
    bars1m_dates = sorted(bars1m_per_date.keys())
    bars1m_code = None
    bars1m_from = bars1m_to = None
    if bars1m_codes:
        bars1m_code = DEFAULT_BARS1M_CODE if DEFAULT_BARS1M_CODE in bars1m_codes else bars1m_codes[0]
        code_dates = [d for d in bars1m_dates if bars1m_code in bars1m_per_date[d]]
        if code_dates:
            bars1m_to = code_dates[-1]
            bars1m_from = code_dates[-DEFAULT_HIST_DAYS] if len(code_dates) >= DEFAULT_HIST_DAYS else code_dates[0]

    # rci_sign_gate / spike_*（RCI 符号ゲート・トゲ反転）の UI 既定は app.js の DEFAULT_COND が持ち、
    # フロントは常に明示送信する。ここ（/api/sources の defaults）は「UI 既定の広告」で、チャート API の
    # フォールバック（build_chart_* の _get_* 既定）は 0＝無効にして、param を明示しない直接呼び出し/テストを
    # 従来挙動のまま保つ。広告値は UI 既定＝日本株チャンピオン（トゲ単独モード: 符号 OFF・トゲ ON
    # depth=80/turn=25。2026-07-19 walkforward で選定）に合わせる。
    # sar_filter=1（従来どおり ON）・skip_open_bars=0（無効）はバー版のみ効くが、UI 既定広告として併記する。
    _SPIKE_UI = {"rci_sign_gate": 0, "rci_sign_level": 0.0, "dual_rci_slope_min": 0.0,
                 "spike_depth_min": 80.0, "spike_turn_min": 25.0,
                 "sar_filter": 1, "skip_open_bars": 0, "fade_sigma_k": 0.0, "fade_boll_z": 0.0}
    defaults = {
        "source": "hist1m",
        "rci_short": RCI_SHORT,
        "rci_long": RCI_LONG,
        "sma": ",".join(str(p) for p in DEFAULT_SMA),
        "hist1m": {
            "code": hist_entry_code,
            "days": DEFAULT_HIST_DAYS,
            **HIST_DEFAULTS,
            **_SPIKE_UI,
        },
        "bars1m": {
            "code": bars1m_code,
            "from": bars1m_from,
            "to": bars1m_to,
            **BARS1M_DEFAULTS,
            **_SPIKE_UI,
        },
        "ticks": {
            "code": tick_code,
            "date": latest_tick_date,
            "tf": DEFAULT_TICK_TF,
            **TICK_DEFAULTS,
            **_SPIKE_UI,
        },
    }
    # 日本株のドロップダウン表示用に code→社名を付与（UI は「社名 (7203)」で表示する）。
    jp_names = _load_jp_names()
    bars1m_names = {c: jp_names[c] for c in bars1m_codes if c in jp_names}

    # 米国株サイドバー用: hist1m ファイルとティック録画コードの和集合（ソースは UI に見せない）。
    # 表示順は直近録画セッションの実ティック数の降順（活発だった順）に並べ替える。
    us_tick_codes: set[str] = set()
    for codes in ticks.values():
        us_tick_codes.update(codes)
    us_codes = _order_us_codes(sorted(set(hist_codes) | us_tick_codes))

    # 初期表示銘柄（localStorage が無い初回）。日本株集中の方針で JP を最優先。
    defaults["code"] = bars1m_code or hist_entry_code or tick_code

    return {
        "hist1m": hist_codes,
        "bars1m": bars1m_codes,
        "bars1m_names": bars1m_names,
        "bars1m_dates": bars1m_dates,
        "bars1m_aptitude": _latest_aptitude_hints(),   # サイドバーの適性印（◎/○ のみ）
        "us_codes": us_codes,
        "ticks": ticks,
        "defaults": defaults,
    }


# --- 指標系列（candles / sma / rci / sar）を組み立てる --------------------

def _empty_payload(sma_periods):
    return {
        "candles": [],
        "sma": {str(p): [] for p in sma_periods},
        "rci9": [],
        "rci14": [],
        "sar": [],
        # ボリンジャーバンド（basis=SMA / upper=+z×std / lower=−z×std）。ローソクと同一 time 集合の
        # whitespace 方式（ウォームアップ NaN は time のみの点）で返す。全ソースで常に埋める。
        "boll": {"basis": [], "upper": [], "lower": []},
        "entries": [],
        "attention": [],   # 注目（事前アラート）シグナル。bars1m 経路で埋める。
        # 統計カードの初期値（データ無し）。実データ経路で build_stats により上書きする。
        "stats": {
            "n_signals": 0, "n_valid": 0, "n_truncated": 0,
            "n_long": 0, "n_short": 0, "horizons": [], "session": [], "empty": True,
        },
    }


def _push_segment(payload, bars: pd.DataFrame, sma_periods, af, boll_window=None, boll_z=None):
    """1 セッション分の確定バーから描画系列を計算して payload に積む。

    セッション（hist1m/bars1m は日、ticks は 1 日分の全バー）ごとに指標を計算する。
    軸ズレ対策として rci9/rci14/sar/sma は **ローソクと同一の time 集合**で返し、ウォーム
    アップ（値が NaN）区間は value を持たない whitespace 点（{"time": t}）として積む。こう
    すると下段 RCI ペインとメインの time→論理インデックス対応が一致し、可視論理レンジ同期で
    同時刻が縦に揃う（有限点だけ間引くと下段の時間集合がローソクと不一致になりズレる）。
    RCI は短期 9・中期 14 の両方、SAR は strategy と同じ af でバッチ計算する。
    """
    b = bars.sort_values("time").reset_index(drop=True)
    n = len(b)
    if n == 0:
        return
    t = [_epoch(x) for x in b["time"]]
    o = b["open"].to_numpy(dtype=np.float64)
    h = b["high"].to_numpy(dtype=np.float64)
    lo = b["low"].to_numpy(dtype=np.float64)
    c = b["close"].to_numpy(dtype=np.float64)

    for i in range(n):
        payload["candles"].append({
            "time": t[i], "open": _round(o[i], 4), "high": _round(h[i], 4),
            "low": _round(lo[i], 4), "close": _round(c[i], 4),
        })

    def _point(ti, val, nd):
        """有限なら値付き点、NaN なら whitespace（time のみ）点を返す。"""
        if math.isfinite(val):
            return {"time": ti, "value": round(float(val), nd)}
        return {"time": ti}

    r9 = rci_series(c, RCI_SHORT)
    r14 = rci_series(c, RCI_LONG)
    sar, _trend = sar_series(h, lo, c, af[0], af[1], af[2])
    for i in range(n):
        payload["rci9"].append(_point(t[i], r9[i], 3))
        payload["rci14"].append(_point(t[i], r14[i], 3))
        payload["sar"].append(_point(t[i], sar[i], 4))

    for p in sma_periods:
        m = pd.Series(c).rolling(int(p)).mean().to_numpy()
        arr = payload["sma"][str(p)]
        for i in range(n):
            arr.append(_point(t[i], m[i], 4))

    # ボリンジャーバンド（basis=SMA(window) / upper=basis+z×std / lower=basis−z×std）。
    # std は戦略 fade_boll と同じ ddof=0（「バンドに触れる==z 閾値到達==fadeb 発火」を視覚一致させる）。
    if boll_window and boll_z and int(boll_window) >= 2 and float(boll_z) > 0.0:
        w = int(boll_window)
        z = float(boll_z)
        cser = pd.Series(c)
        basis = cser.rolling(w).mean().to_numpy()
        std = cser.rolling(w).std(ddof=0).to_numpy()
        upper = basis + z * std
        lower = basis - z * std
        for i in range(n):
            payload["boll"]["basis"].append(_point(t[i], basis[i], 4))
            payload["boll"]["upper"].append(_point(t[i], upper[i], 4))
            payload["boll"]["lower"].append(_point(t[i], lower[i], 4))


# --- エントリー → マーカー/明細 --------------------------------------------

def _entry_common(row, epoch_time):
    """entries の 1 行を UI 共通のエントリー明細 dict に写す。"""
    return {
        "time": epoch_time,
        "direction": str(row["direction"]),
        "price": _round(row.get("entry_price"), 4),
        "signal": _round(row.get("signal_close", row.get("signal_price")), 4),
        "rci": _round(row.get("rci"), 2),
        "drci": _round(row.get("drci1", row.get("drci")), 2),
        "prev_diff": _round(row.get("prev_diff"), 2),
        "efficiency": _round(row.get("efficiency"), 2),
        "sar": _round(row.get("sar"), 4),
        "atr": _round(row.get("atr"), 4),
    }


def hist_entries_to_records(entries: pd.DataFrame) -> list[dict]:
    """バー近似 entries を UI 明細に変換する（前方リターンは +N バー net/gross）。"""
    out = []
    if entries is None or len(entries) == 0:
        return out
    _TRIGGER_LABEL = {"spike": "トゲ反転", "dual": "RCI同調", "angle": "角度",
                      "fade": "σフェード", "fadeb": "ボリンジャーフェード"}
    for _i, row in entries.iterrows():
        rec = _entry_common(row, _epoch(row["time"]))
        trig = str(row.get("trigger", "angle"))
        rec["trigger"] = trig
        fwd = []
        for k in (1, 2, 3):
            col = f"ret_{k}bar_net"
            if col in entries.columns:
                fwd.append({"label": f"+{k}本 net(bp)", "bp": _round(row[col], 2)})
        rec["forward"] = fwd
        rec["reason"] = f"発火トリガー={_TRIGGER_LABEL.get(trig, trig)}（SAR方向一致 + 効率/クールダウン通過。バー近似）"
        out.append(rec)
    return out


def bar_attention_to_records(att: pd.DataFrame) -> list[dict]:
    """注目（事前アラート）シグナルを UI 用レコードに変換する（bars1m 経路専用）。"""
    out = []
    if att is None or len(att) == 0:
        return out
    for _i, row in att.iterrows():
        out.append({
            "time": _epoch(row["time"]),
            "direction": str(row["direction"]),
            "rci": _round(row.get("rci"), 2),
            "sar": _round(row.get("sar"), 4),
            "depth": _round(row.get("depth"), 2),
            "turn": _round(row.get("turn"), 2),
        })
    return out


def tick_entries_to_records(entries: pd.DataFrame, tf_sec: int) -> list[dict]:
    """ティック駆動 entries を UI 明細に変換する（前方リターンは +Ns net、MFE/MAE 付き）。

    マーカーは表示足のビンに合わせて floor した時刻に置く（表示ローソクへ確実に載せる）。
    """
    out = []
    if entries is None or len(entries) == 0:
        return out
    for _i, row in entries.iterrows():
        raw = _epoch(row["time"])
        binned = raw - (raw % int(tf_sec))  # 表示足ビン開始時刻へスナップ
        rec = _entry_common(row, binned)
        rec["signal"] = _round(row.get("signal_price"), 4)
        rec["mfe"] = _round(row.get("mfe_bp"), 2)
        rec["mae"] = _round(row.get("mae_bp"), 2)
        fwd = []
        for h in (30, 60, 120):
            col = f"ret_{h}s_net"
            if col in entries.columns:
                fwd.append({"label": f"+{h}s net(bp)", "bp": _round(row[col], 2)})
        rec["forward"] = fwd
        rec["reason"] = "SAR方向一致 + 足内ΔRCI閾値超 + 効率フィルタ通過（ティック駆動）"
        out.append(rec)
    return out


# --- 統計カード（勝率・期待値・時間帯別など） ------------------------------

def build_stats(entries: pd.DataFrame, kind: str, source: str) -> dict:
    """entries から右パネル用の統計カードを組み立てる。

    kind='bar'（hist1m/bars1m の +Nバー評価）/ 'tick'（ticks の +Ns 評価）で評価窓が変わる。
    集計は scalplab.backtest.metrics の純関数を再利用し、ここでは JSON 化と丸めだけを行う。
    件数 0 のときは empty=True を返す（UI は「エントリーなし」を表示）。
    """
    total = int(len(entries)) if entries is not None else 0
    if total == 0:
        return {"kind": kind, "n_signals": 0, "n_valid": 0, "n_truncated": 0,
                "n_long": 0, "n_short": 0, "horizons": [], "session": [], "empty": True}

    valid = metrics.valid_entries(entries)
    n_valid = int(len(valid))
    n_long = int((valid["direction"] == "long").sum()) if n_valid else 0
    n_short = int((valid["direction"] == "short").sum()) if n_valid else 0

    horizons = []
    for h in metrics.horizon_breakdown(entries, kind):
        horizons.append({
            "label": h["label"],
            "win_rate": _num(h["win_rate"]),
            "mean_net_bp": _round(h["mean_net_bp"], 2),
            "median_net_bp": _round(h["median_net_bp"], 2),
            "mean_gross_bp": _round(h["mean_gross_bp"], 2),
            "sum_net_bp": _round(h["sum_net_bp"], 1),
            "n": int(h["n"]),
        })

    # 時間帯別: 日本株は前場/後場、米国（hist1m/ticks）は午前/午後。境界は正午（12:00）。
    labels = ("前場", "後場") if source == "bars1m" else ("午前", "午後")
    primary_col = "ret_1bar_net" if kind == "bar" else "ret_60s_net"
    session = [
        {"label": s["label"], "n": int(s["n"]), "win_rate": _num(s["win_rate"])}
        for s in metrics.session_split(entries, primary_col, labels=labels)
    ]

    # 主指標（バーは +1本、ティックは +60s）をトップレベルにも展開する。
    primary_label = "+1本" if kind == "bar" else "+60s"
    primary = next((h for h in horizons if h["label"] == primary_label), horizons[0] if horizons else None)

    stats = {
        "kind": kind,
        "n_signals": total,
        "n_valid": n_valid,
        "n_truncated": total - n_valid,
        "n_long": n_long,
        "n_short": n_short,
        "primary_label": primary_label,
        "horizons": horizons,
        "session": session,
        "max_losing_streak": metrics.max_losing_streak(entries, primary_col),
        "empty": False,
    }
    if primary is not None:
        stats["expectancy_bp"] = primary["mean_net_bp"]
        stats["median_net_bp"] = primary["median_net_bp"]
        stats["mean_gross_bp"] = primary["mean_gross_bp"]
        stats["sum_net_bp"] = primary["sum_net_bp"]
    if kind == "tick":
        stats["mfe_median_bp"] = _round(valid["mfe_bp"].median(), 2) if ("mfe_bp" in valid and n_valid) else None
        stats["mae_median_bp"] = _round(valid["mae_bp"].median(), 2) if ("mae_bp" in valid and n_valid) else None
    return stats


# --- スクリーニング結果（reports/screen-*/ranking.csv） --------------------

def list_screens() -> dict:
    """reports/screen-*/ranking.csv を持つ run 一覧（新しい順）を返す。"""
    runs: list[str] = []
    if REPORTS_DIR.exists():
        for d in sorted((x for x in REPORTS_DIR.glob("screen-*") if x.is_dir()),
                        key=lambda p: p.name, reverse=True):
            if (d / "ranking.csv").exists():
                runs.append(d.name)
    return {"runs": runs, "latest": runs[0] if runs else None}


def read_screen(run: str | None) -> dict:
    """指定 run（既定は最新）の ranking.csv を UI 用の行リストに変換して返す。

    run はホワイトリスト（list_screens が返す run 名）に一致するものだけ受理する
    （任意パス連結を許さずトラバーサルを防ぐ）。日本株コードには社名を付ける。
    """
    runs = list_screens()["runs"]
    if not runs:
        return {"run": None, "runs": [], "rows": [],
                "message": "スクリーニング結果がありません。scalplab.backtest.screen を実行すると reports/screen-* に出力されます。"}
    if not run or run not in runs:
        run = runs[0]

    df = pd.read_csv(REPORTS_DIR / run / "ranking.csv")
    names = _load_jp_names()
    rows = []
    for _i, r in df.iterrows():
        code = str(r["code"])
        rows.append({
            "code": code,
            "name": names.get(code),  # 米国コードは None（UI はティッカー表示）
            "n_signals": _num(r.get("n_signals_median")),
            "median_exp1": _num(r.get("median_exp1_net_bp")),
            "median_win1": _num(r.get("median_win1")),
            "median_exp2": _num(r.get("median_exp2_net_bp")),
            "median_win2": _num(r.get("median_win2")),
            "best_exp1": _num(r.get("best_exp1_net_bp")),
            "best_win1": _num(r.get("best_win1")),
            "best_rci_period": _num(r.get("best_rci_period")),
            "best_slope_threshold": _num(r.get("best_slope_threshold")),
            "best_eff_min": _num(r.get("best_eff_min")),
            # 銘柄適性（機能B の run で書かれる列。旧レポートには無いので None にフォールバック）
            "turnover_oku": _num(r.get("avg_turnover_oku")),
            "range_bp": _num(r.get("range_med_bp")),
            "bars_per_day": _num(r.get("bars_per_day")),
            "tick_count": _num(r.get("tick_count")),
            # 手法適性バッジ（◎/○/×）。CSV に列があればそれを、無ければレンジ中央値から算出（旧レポート互換）。
            "aptitude": (str(r["aptitude"]) if ("aptitude" in df.columns and pd.notna(r.get("aptitude")))
                         else screen_mod.aptitude_badge(_num(r.get("range_med_bp")))),
        })
    return {"run": run, "runs": runs, "rows": rows}


# --- スクリーニング実行（機能B: 現在の条件を全 JP 銘柄へ適用） --------------
#
# ThreadingHTTPServer なので同時リクエストがありうる。重いバックテストの二重実行を防ぐため
# ノンブロッキングなロックで弾く（実行中の POST は 409 を返す）。

_SCREEN_RUN_LOCK = threading.Lock()


def _latest_tick_counts() -> dict[str, int]:
    """最新の松井 Tick回数ランキング CSV を code→回数の dict にする（無ければ空）。"""
    from ..datafeed import jp_tick_ranking

    path = jp_tick_ranking.latest_saved_csv(_project_data_dir() / "universe")
    if path is None:
        return {}
    return {r["code"]: r["tick_count"] for r in jp_tick_ranking.load_ranking_csv(path)}


def run_screen(body: dict) -> dict:
    """現在のチャート条件（cond 一式）と期間(from/to)を全 JP 銘柄の bars1m に適用して銘柄別成績を返す。

    結果は reports/screen-*/ranking.csv（既存形式互換＋適性列）に保存し、read_screen 経由の UI 行で返す。
    期間未指定は全保存日。from/to 書式が不正なら ValueError（呼び手が 400 にする）。
    """
    per_date, _codes = _bars1m_index()
    all_dates = sorted(per_date.keys())
    frm = (str(body.get("from") or "").strip()) or None
    to = (str(body.get("to") or "").strip()) or None
    if frm:
        date_t.fromisoformat(frm)
    if to:
        date_t.fromisoformat(to)
    dates = [d for d in all_dates if (not frm or d >= frm) and (not to or d <= to)]
    if not dates:
        return {"run": None, "runs": list_screens()["runs"], "rows": [],
                "message": "指定期間に日本株1分足の保存データがありません。"}

    codes = screen_mod.auto_symbols_bars1m(dates, "jp")
    if not codes:
        return {"run": None, "runs": list_screens()["runs"], "rows": [],
                "message": "対象の日本株銘柄がありません。"}

    # 条件はチャートと同じパラメータ体系（トゲ反転・角度・符号ゲート・効率・クールダウン）。
    params = {
        "rci_period": RCI_SHORT,
        "slope_threshold": _get_slope(body, BARS1M_DEFAULTS["slope_threshold"]),
        "prev_diff_min": _get_float(body, "prev_diff_min", BARS1M_DEFAULTS["prev_diff_min"]),
        "rci_sign_gate": _get_int(body, "rci_sign_gate", 0),
        "rci_sign_level": _get_float(body, "rci_sign_level", 0.0),
        "dual_rci_slope_min": _get_float(body, "dual_rci_slope_min", 0.0),
        "spike_depth_min": _get_float(body, "spike_depth_min", 0.0),
        "spike_turn_min": _get_float(body, "spike_turn_min", 0.0),
        "eff_min": _get_float(body, "eff_min", BARS1M_DEFAULTS["eff_min"]),
        "sar_filter": _get_int(body, "sar_filter", 1),
        "skip_open_bars": _get_int(body, "skip_open_bars", 0),
        "fade_sigma_k": _get_float(body, "fade_sigma_k", 0.0),  # σフェード（急変逆張り・0=無効）
        "fade_boll_z": _get_float(body, "fade_boll_z", 0.0),    # ボリンジャーフェード（逆張り・0=無効）
        "cooldown_bars": _get_int(body, "cooldown", BARS1M_DEFAULTS["cooldown"]),
        "af_init": DEFAULT_AF[0], "af_step": DEFAULT_AF[1], "af_max": DEFAULT_AF[2],
    }
    cost_bp = _get_float(body, "cost_bp", BARS1M_DEFAULTS["cost_bp"])

    ranking, n_missing = screen_mod.screen_run_bars1m(
        codes, dates, params, cost_bp, eval_bars=(1, 2, 3), tick_counts=_latest_tick_counts())

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = REPORTS_DIR / f"screen-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    ranking.to_csv(out_dir / "ranking.csv", index=False)

    result = read_screen(out_dir.name)
    result.update(n_missing=n_missing, n_symbols=len(codes),
                  period={"from": dates[0], "to": dates[-1]})
    return result


# --- エントリー履歴（機能: 実際に効いた銘柄の全エントリーをチャンピオン設定で一覧） ------

def _jp_champion_config() -> dict:
    """config/best-params-jp.yaml を読み、params / cost_bp / eval_bars を返す（履歴ビュー用の固定設定）。

    履歴はチャート条件（ユーザーがいじる値）とは独立に、日本株チャンピオンを固定適用する。
    """
    import yaml

    path = PROJECT_ROOT / "config" / "best-params-jp.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    params = dict(raw.get("params") or {})
    params.setdefault("af_init", DEFAULT_AF[0])
    params.setdefault("af_step", DEFAULT_AF[1])
    params.setdefault("af_max", DEFAULT_AF[2])
    cost_bp = float(raw.get("cost_bp", BARS1M_DEFAULTS["cost_bp"]))
    eval_bars = tuple(int(k) for k in (raw.get("eval_bars") or (1, 2)))
    return {"params": params, "cost_bp": cost_bp, "eval_bars": eval_bars}


def build_history(q: dict) -> dict:
    """指定銘柄(JP.*)の全保存日 bars1m にチャンピオン設定を適用し、エントリー履歴を返す。

    「日本株で実際に効いた銘柄のエントリー履歴を見る」ビューの実体。前場/後場を独立計算する既存機構
    （screen.iter_jp_sessions）を再利用して昼休み跨ぎを構造的に除外する。日別小計と累計を付けて返す。
    戻り値: {code, name, eval_bars, champion, days:[{date, entries:[...], subtotal}], totals}。
    """
    code = (q.get("code") or "").strip()
    if not code:
        raise ValueError("code は必須です。")

    per_date, _codes = _bars1m_index()
    code_dates = sorted(d for d in per_date if code in per_date.get(d, []))

    conf = _jp_champion_config()
    params, cost_bp = conf["params"], conf["cost_bp"]
    ks = tuple(conf["eval_bars"])

    days = []
    tot_n = tot_long = tot_short = 0
    tot_ret = {k: 0.0 for k in ks}
    cum = {k: 0.0 for k in ks}   # 累計（日をまたいで積む）

    for d in code_dates:
        try:
            bars = store_loader.load_bars1m(d, code)
        except FileNotFoundError:
            continue
        sess = screen_mod.filter_jp_session(bars)
        parts = [bar_signals(day, params, code=code, cost_bp=cost_bp, eval_bars=ks)
                 for day in screen_mod.iter_jp_sessions(sess)]
        parts = [p for p in parts if len(p)]
        entries = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        if len(entries):
            entries = entries.sort_values("time").reset_index(drop=True)

        recs = []
        day_ret = {k: 0.0 for k in ks}
        n_long = n_short = 0
        for _i, row in entries.iterrows():
            fwd = {}
            for k in ks:
                col = f"ret_{k}bar_net"
                v = _round(row.get(col), 2) if col in entries.columns else None
                fwd[str(k)] = v
                if v is not None:
                    day_ret[k] += v
                    cum[k] += v
            direction = str(row["direction"])
            if direction == "long":
                n_long += 1
            else:
                n_short += 1
            recs.append({
                "time": _epoch(row["time"]),
                "direction": direction,
                "price": _round(row.get("entry_price"), 4),
                "ret": fwd,                                       # {"1": net_bp, "2": net_bp}
                "trigger": str(row.get("trigger", "angle")),
                "cum": {str(k): _round(cum[k], 2) for k in ks},   # その行までの累計 net
            })
        days.append({
            "date": d, "entries": recs,
            "subtotal": {"n": len(recs), "n_long": n_long, "n_short": n_short,
                         "ret": {str(k): _round(day_ret[k], 2) for k in ks}},
        })
        tot_n += len(recs)
        tot_long += n_long
        tot_short += n_short
        for k in ks:
            tot_ret[k] += day_ret[k]

    names = _load_jp_names()
    return {
        "code": code,
        "name": names.get(code),
        "eval_bars": list(ks),
        "champion": {
            "spike_depth_min": params.get("spike_depth_min"),
            "spike_turn_min": params.get("spike_turn_min"),
            "cooldown_bars": params.get("cooldown_bars"),
            "cost_bp": cost_bp,
        },
        "days": days,
        "totals": {"n": tot_n, "n_long": tot_long, "n_short": tot_short,
                   "ret": {str(k): _round(tot_ret[k], 2) for k in ks}},
        "message": (None if code_dates else f"{code} の日本株1分足の保存データがありません。"),
    }


# --- エントリー注釈（機能A: ユーザーが手法を教える教師データ） -------------

def _annotations_db_path() -> Path:
    """注釈 DB のパス。data ルートに置くので、テストの DATA_DIR 差し替えに自動追従する。"""
    return _project_data_dir() / "annotations.sqlite3"


def _annotation_context(code: str, ts: pd.Timestamp) -> dict:
    """保存時点の指標スナップショット（price/rci9/rci14/sar/atr/sma5/sma25）を採取する。

    AI が注釈を「その時どういう相場だったか」と一緒に読めるようにするための文脈。JP.* は bars1m、
    US.* は hist1m の当日セッションから、クリックしたバー（時刻一致・無ければ 60 秒以内の最近傍）の
    指標を確定足バッチ（表示と同じ rci_series/sar_series/atr_series）で計算する。取得できなければ空 dict。
    """
    source, _ = _resolve_source_and_dates(code)
    date = ts.date().isoformat()
    bars = None
    if source == "bars1m":
        try:
            bars = store_loader.load_bars1m(date, code)
        except FileNotFoundError:
            bars = None
    elif source == "hist1m":
        cache = history.load_cache(code)
        if not cache.empty:
            d = pd.to_datetime(cache["time"]).dt.date
            bars = cache[d == ts.date()].reset_index(drop=True)
    if bars is None or len(bars) == 0:
        return {}

    b = bars.sort_values("time").reset_index(drop=True)
    times = pd.to_datetime(b["time"])
    exact = times[times == ts].index
    if len(exact):
        i = int(exact[0])
    else:
        diff = (times - ts).abs()
        i = int(diff.idxmin())
        if diff.iloc[i].total_seconds() > 60:
            return {}

    c = b["close"].to_numpy(dtype=np.float64)
    h = b["high"].to_numpy(dtype=np.float64)
    lo = b["low"].to_numpy(dtype=np.float64)
    r9 = rci_series(c, RCI_SHORT)
    r14 = rci_series(c, RCI_LONG)
    sar, _trend = sar_series(h, lo, c, DEFAULT_AF[0], DEFAULT_AF[1], DEFAULT_AF[2])
    atr = atr_series(h, lo, c, 14)
    sma5 = pd.Series(c).rolling(5).mean().to_numpy()
    sma25 = pd.Series(c).rolling(25).mean().to_numpy()

    def g(arr, nd=4):
        return _round(arr[i], nd)

    return {
        "price": g(c), "rci9": g(r9, 2), "rci14": g(r14, 2), "sar": g(sar),
        "atr": g(atr), "sma5": g(sma5), "sma25": g(sma25),
        "bar_index": i, "n_bars": int(len(b)),
    }


def _annotations_list(code: str | None) -> list[dict]:
    """注釈一覧を返す。各行にチャート用の bar_epoch（マーカー配置座標）を付ける。"""
    recs = annotations.list_annotations(code, db_path=_annotations_db_path())
    for r in recs:
        try:
            r["bar_epoch"] = _epoch(pd.Timestamp(r["bar_time"]))
        except Exception:  # noqa: BLE001 - 壊れた時刻は座標なしで返す（一覧は出す）
            r["bar_epoch"] = None
    return recs


def create_annotation(body: dict) -> tuple[int, dict]:
    """注釈を1件作成する（POST /api/annotations）。bar_time は epoch 秒で受ける。"""
    code = str(body.get("code") or "").strip()
    kind = str(body.get("kind") or "").strip()
    reason = str(body.get("reason") or "")
    try:
        epoch = int(body.get("bar_time"))
    except (TypeError, ValueError):
        return 400, {"error": "bar_time（epoch秒）が不正です"}
    ts = pd.Timestamp(epoch, unit="s")  # naive JST 壁時計へ戻す（_epoch の逆写像）
    bar_time_iso = ts.isoformat()
    source, _ = _resolve_source_and_dates(code)
    context = _annotation_context(code, ts)
    try:
        rec = annotations.add_annotation(
            code, bar_time_iso, kind, reason, source, context, db_path=_annotations_db_path())
    except ValueError as e:
        return 400, {"error": str(e)}
    rec["bar_epoch"] = epoch
    return 200, {"annotation": rec}


def delete_annotation(body: dict) -> tuple[int, dict]:
    """注釈を1件削除する（POST /api/annotations/delete）。"""
    try:
        aid = int(body.get("id"))
    except (TypeError, ValueError):
        return 400, {"error": "id が不正です"}
    ok = annotations.delete_annotation(aid, db_path=_annotations_db_path())
    return 200, {"deleted": ok}


# --- source 別のチャート構築 ------------------------------------------------

def _parse_sma(raw) -> list[int]:
    if not raw:
        return list(DEFAULT_SMA)
    periods = []
    for tok in str(raw).split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            v = int(float(tok))
        except ValueError:
            raise ValueError(f"SMA 期間が不正です: {tok!r}")
        if v < 1 or v > 500:
            raise ValueError(f"SMA 期間は 1〜500 の範囲で指定してください: {v}")
        periods.append(v)
    # 重複除去（順序保持）
    seen = set()
    uniq = []
    for p in periods:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq or list(DEFAULT_SMA)


def _get_float(q, key, default):
    raw = q.get(key)
    if raw is None or raw == "":
        return float(default)
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{key} は数値で指定してください: {raw!r}")


def _get_int(q, key, default):
    raw = q.get(key)
    if raw is None or raw == "":
        return int(default)
    try:
        return int(float(raw))
    except ValueError:
        raise ValueError(f"{key} は整数で指定してください: {raw!r}")


def _boll_display_params(q) -> tuple[int, float]:
    """バンド描画の (window, z) を返す。z は fade_boll_z>0 ならその値・無効時は既定 3.0。

    バンドは戦略トグルの ON/OFF に関わらず常に返し（描画の表示/非表示は UI 側で切替）、z は「有効なら
    その閾値のバンド、無効なら既定 3.0 のバンド」を出す。window は fade_boll_window（既定 20）。
    """
    z = _get_float(q, "fade_boll_z", 0.0)
    disp_z = z if z > 0.0 else DEFAULT_BOLL_Z
    w = _get_int(q, "fade_boll_window", DEFAULT_BOLL_WINDOW)
    if w < 2:
        w = DEFAULT_BOLL_WINDOW
    return w, disp_z


def _get_slope(q, default):
    """角度閾値を取得する。API 仕様の rci_slope_threshold と別名 slope_threshold を両受け。"""
    raw = q.get("slope_threshold")
    if raw is None or raw == "":
        raw = q.get("rci_slope_threshold")
    if raw is None or raw == "":
        return float(default)
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"slope_threshold は数値で指定してください: {raw!r}")


def build_chart_hist1m(q: dict) -> dict:
    """hist1m（過去 1 分足）のチャートペイロードを構築する（過去方向ページング対応）。

    from/to が来ればその範囲（レガシー API・テスト互換）、無ければ before/days による
    過去方向ページング（既定は最新 DEFAULT_HIST_DAYS 日、before 指定でその日より前を N 日）。
    """
    code = q.get("code") or DEFAULT_ENTRY_CODE
    sma_periods = _parse_sma(q.get("sma"))
    slope = _get_slope(q, HIST_DEFAULTS["slope_threshold"])
    prev_diff_min = _get_float(q, "prev_diff_min", HIST_DEFAULTS["prev_diff_min"])
    rci_sign_gate = _get_int(q, "rci_sign_gate", 0)  # 未指定は 0＝無効（UI は明示送信）
    rci_sign_level = _get_float(q, "rci_sign_level", 0.0)  # 符号ゲート境界 ±level（0=従来 0 境界）
    dual_rci_slope_min = _get_float(q, "dual_rci_slope_min", 0.0)  # 第2トリガー RCI 同調（0=無効）
    spike_depth_min = _get_float(q, "spike_depth_min", 0.0)  # トゲ反転（未指定は 0＝無効）
    spike_turn_min = _get_float(q, "spike_turn_min", 0.0)
    eff_min = _get_float(q, "eff_min", HIST_DEFAULTS["eff_min"])
    sar_filter = _get_int(q, "sar_filter", 1)  # SAR 方向フィルタ（未指定は 1＝従来）
    skip_open_bars = _get_int(q, "skip_open_bars", 0)  # 前場の寄り抑制（未指定は 0＝無効）
    fade_sigma_k = _get_float(q, "fade_sigma_k", 0.0)  # σフェード（急変逆張り。未指定は 0＝無効）
    fade_boll_z = _get_float(q, "fade_boll_z", 0.0)    # ボリンジャーフェード（逆張り。未指定は 0＝無効）
    cooldown = _get_int(q, "cooldown", HIST_DEFAULTS["cooldown"])
    cost_bp = _get_float(q, "cost_bp", HIST_DEFAULTS["cost_bp"])
    boll_window, boll_z = _boll_display_params(q)  # ボリンジャーバンド描画用（表示は常時・UI で表示切替）

    cache = history.load_cache(code)
    all_dates = (sorted({d.isoformat() for d in pd.to_datetime(cache["time"]).dt.date})
                 if not cache.empty else [])
    span_from = all_dates[0] if all_dates else None
    span_to = all_dates[-1] if all_dates else None

    if q.get("from") or q.get("to"):
        default_from = all_dates[-DEFAULT_HIST_DAYS] if len(all_dates) >= DEFAULT_HIST_DAYS else span_from
        req_from = q.get("from") or default_from
        req_to = q.get("to") or span_to
    else:
        _pg, _hm = _page_from_query(all_dates, q, DEFAULT_HIST_DAYS)
        req_from = _pg[0] if _pg else span_from
        req_to = _pg[-1] if _pg else span_to

    page_dates: list[str] = []
    if all_dates and req_from is not None and req_to is not None:
        date_t.fromisoformat(req_from)
        date_t.fromisoformat(req_to)
        page_dates = [d for d in all_dates if req_from <= d <= req_to]
    has_more = bool(page_dates) and any(d < page_dates[0] for d in all_dates)

    meta = {
        "source": "hist1m", "resolved_source": "hist1m", "code": code, "tf_label": "1分",
        "available_tfs": None,
        "sma": sma_periods, "available_from": span_from, "available_to": span_to,
        "from": req_from, "to": req_to,
        "page_from": (page_dates[0] if page_dates else None),
        "page_to": (page_dates[-1] if page_dates else None),
        "has_more": has_more,
        "params": {"rci_period": RCI_SHORT, "slope_threshold": slope, "prev_diff_min": prev_diff_min,
                   "rci_sign_gate": rci_sign_gate, "rci_sign_level": rci_sign_level,
                   "dual_rci_slope_min": dual_rci_slope_min, "spike_depth_min": spike_depth_min,
                   "spike_turn_min": spike_turn_min, "eff_min": eff_min, "cooldown_bars": cooldown,
                   "sar_filter": sar_filter, "skip_open_bars": skip_open_bars,
                   "fade_sigma_k": fade_sigma_k, "fade_boll_z": fade_boll_z, "cost_bp": cost_bp},
        "boll": {"window": boll_window, "z": boll_z},
    }

    payload = _empty_payload(sma_periods)
    payload["meta"] = meta

    if cache.empty:
        payload["message"] = "この銘柄の過去1分足がありません。config/screen.yaml の銘柄を取得してください。"
        return payload
    if not page_dates:
        payload["message"] = (
            f"指定期間（{req_from}〜{req_to}）にこの銘柄のデータがありません。"
            f"利用可能な期間は {span_from}〜{span_to} です。"
        )
        return payload

    d = pd.to_datetime(cache["time"]).dt.date
    fdate = date_t.fromisoformat(page_dates[0])
    tdate = date_t.fromisoformat(page_dates[-1])
    sel = cache[(d >= fdate) & (d <= tdate)].reset_index(drop=True)
    # 通常取引時間（09:31-16:00 ET）に絞る。strategy スクリーニングと同条件にして
    # 表示ローソクとエントリー計算を同じバー集合の上で一致させる。
    sel = filter_rth(sel)
    if sel.empty:
        payload["message"] = (
            f"指定期間（{req_from}〜{req_to}）にこの銘柄のデータがありません。"
            f"利用可能な期間は {span_from}〜{span_to} です。"
        )
        return payload

    params = {"rci_period": RCI_SHORT, "slope_threshold": slope, "prev_diff_min": prev_diff_min,
              "rci_sign_gate": rci_sign_gate, "rci_sign_level": rci_sign_level,
              "dual_rci_slope_min": dual_rci_slope_min, "spike_depth_min": spike_depth_min,
              "spike_turn_min": spike_turn_min, "eff_min": eff_min, "cooldown_bars": cooldown,
              "sar_filter": sar_filter, "skip_open_bars": skip_open_bars,
              "fade_sigma_k": fade_sigma_k, "fade_boll_z": fade_boll_z,
              "af_init": DEFAULT_AF[0], "af_step": DEFAULT_AF[1], "af_max": DEFAULT_AF[2]}

    entry_frames = []
    for day in iter_session_days(sel):
        _push_segment(payload, day, sma_periods, DEFAULT_AF, boll_window, boll_z)
        entry_frames.append(bar_signals(day, params, code=code, cost_bp=cost_bp, eval_bars=(1, 2, 3)))
    entries = pd.concat(entry_frames, ignore_index=True) if entry_frames else pd.DataFrame()
    payload["entries"] = hist_entries_to_records(entries)
    payload["stats"] = build_stats(entries, "bar", "hist1m")
    return payload


def build_chart_bars1m(q: dict) -> dict:
    """bars1m（自前保存の日本株 1 分足・JP.*）のチャートペイロードを構築する。

    hist1m と同じバー近似版（rci_impulse_bar.bar_signals）をセッション日（＝保存日）ごとに
    実行する。ただし日本株は既に東証セッションのみのため RTH フィルタは掛けない（掛けると
    米国東部時間で誤って全消しになる）。指標も保存日ごとに計算しオーバーナイト混入を避ける。
    """
    code = q.get("code")
    sma_periods = _parse_sma(q.get("sma"))
    slope = _get_slope(q, BARS1M_DEFAULTS["slope_threshold"])
    prev_diff_min = _get_float(q, "prev_diff_min", BARS1M_DEFAULTS["prev_diff_min"])
    rci_sign_gate = _get_int(q, "rci_sign_gate", 0)  # 未指定は 0＝無効（UI は明示送信）
    rci_sign_level = _get_float(q, "rci_sign_level", 0.0)  # 符号ゲート境界 ±level（0=従来 0 境界）
    dual_rci_slope_min = _get_float(q, "dual_rci_slope_min", 0.0)  # 第2トリガー RCI 同調（0=無効）
    spike_depth_min = _get_float(q, "spike_depth_min", 0.0)  # トゲ反転（未指定は 0＝無効）
    spike_turn_min = _get_float(q, "spike_turn_min", 0.0)
    eff_min = _get_float(q, "eff_min", BARS1M_DEFAULTS["eff_min"])
    sar_filter = _get_int(q, "sar_filter", 1)  # SAR 方向フィルタ（未指定は 1＝従来）
    skip_open_bars = _get_int(q, "skip_open_bars", 0)  # 前場の寄り抑制（未指定は 0＝無効）
    fade_sigma_k = _get_float(q, "fade_sigma_k", 0.0)  # σフェード（急変逆張り。未指定は 0＝無効）
    fade_boll_z = _get_float(q, "fade_boll_z", 0.0)    # ボリンジャーフェード（逆張り。未指定は 0＝無効）
    cooldown = _get_int(q, "cooldown", BARS1M_DEFAULTS["cooldown"])
    cost_bp = _get_float(q, "cost_bp", BARS1M_DEFAULTS["cost_bp"])
    boll_window, boll_z = _boll_display_params(q)  # ボリンジャーバンド描画用（表示は常時・UI で表示切替）

    per_date, codes = _bars1m_index()
    all_dates = sorted(per_date.keys())
    if not code:
        code = DEFAULT_BARS1M_CODE if DEFAULT_BARS1M_CODE in codes else (codes[0] if codes else None)

    code_dates = [d for d in all_dates if code and code in per_date.get(d, [])]
    span_from = code_dates[0] if code_dates else None
    span_to = code_dates[-1] if code_dates else None

    # from/to（レガシー）優先、無ければ before/days（新 UI の過去方向ページング）。
    if q.get("from") or q.get("to"):
        default_from = (code_dates[-DEFAULT_HIST_DAYS] if len(code_dates) >= DEFAULT_HIST_DAYS else span_from)
        req_from = q.get("from") or default_from
        req_to = q.get("to") or span_to
    else:
        _pg, _hm = _page_from_query(code_dates, q, DEFAULT_HIST_DAYS)
        req_from = _pg[0] if _pg else span_from
        req_to = _pg[-1] if _pg else span_to

    # from/to の書式検証（不正は 400 に落とす）。比較は "YYYY-MM-DD" の辞書順＝時系列順。
    sel_dates: list[str] = []
    if code_dates and req_from is not None and req_to is not None:
        date_t.fromisoformat(req_from)
        date_t.fromisoformat(req_to)
        sel_dates = [d for d in code_dates if req_from <= d <= req_to]
    has_more = bool(sel_dates) and any(d < sel_dates[0] for d in code_dates)

    meta = {
        "source": "bars1m", "resolved_source": "bars1m", "code": code, "tf_label": "1分",
        "available_tfs": None,
        "sma": sma_periods, "available_from": span_from, "available_to": span_to,
        "from": req_from, "to": req_to, "n_days": len(all_dates),
        "page_from": (sel_dates[0] if sel_dates else None),
        "page_to": (sel_dates[-1] if sel_dates else None),
        "has_more": has_more,
        "params": {"rci_period": RCI_SHORT, "slope_threshold": slope, "prev_diff_min": prev_diff_min,
                   "rci_sign_gate": rci_sign_gate, "rci_sign_level": rci_sign_level,
                   "dual_rci_slope_min": dual_rci_slope_min, "spike_depth_min": spike_depth_min,
                   "spike_turn_min": spike_turn_min, "eff_min": eff_min, "cooldown_bars": cooldown,
                   "sar_filter": sar_filter, "skip_open_bars": skip_open_bars,
                   "fade_sigma_k": fade_sigma_k, "fade_boll_z": fade_boll_z, "cost_bp": cost_bp},
        "boll": {"window": boll_window, "z": boll_z},
    }
    payload = _empty_payload(sma_periods)
    payload["meta"] = meta

    if not code_dates:
        payload["message"] = (
            f"日本株1分足は毎営業日 15:35 に自動保存されます（現在 {len(all_dates)} 日分）。"
        )
        return payload

    if not sel_dates:
        payload["message"] = (
            f"指定期間（{req_from}〜{req_to}）にこの銘柄のデータがありません。"
            f"利用可能な期間は {span_from}〜{span_to} です。"
        )
        return payload

    params = {"rci_period": RCI_SHORT, "slope_threshold": slope, "prev_diff_min": prev_diff_min,
              "rci_sign_gate": rci_sign_gate, "rci_sign_level": rci_sign_level,
              "dual_rci_slope_min": dual_rci_slope_min, "spike_depth_min": spike_depth_min,
              "spike_turn_min": spike_turn_min, "eff_min": eff_min, "cooldown_bars": cooldown,
              "sar_filter": sar_filter, "skip_open_bars": skip_open_bars,
              "fade_sigma_k": fade_sigma_k, "fade_boll_z": fade_boll_z,
              "af_init": DEFAULT_AF[0], "af_step": DEFAULT_AF[1], "af_max": DEFAULT_AF[2]}

    entry_frames = []
    att_frames = []
    for d in sel_dates:
        bars = store_loader.load_bars1m(d, code)  # 保存日＝1 セッション。RTH フィルタは掛けない
        _push_segment(payload, bars, sma_periods, DEFAULT_AF, boll_window, boll_z)
        entry_frames.append(bar_signals(bars, params, code=code, cost_bp=cost_bp, eval_bars=(1, 2, 3)))
        att_frames.append(bar_attention(bars, params, code=code))  # 注目は entries と独立に計算
    entries = pd.concat(entry_frames, ignore_index=True) if entry_frames else pd.DataFrame()
    attention = pd.concat(att_frames, ignore_index=True) if att_frames else pd.DataFrame()
    payload["entries"] = hist_entries_to_records(entries)
    payload["attention"] = bar_attention_to_records(attention)
    payload["stats"] = build_stats(entries, "bar", "bars1m")
    payload["note"] = "日本株1分足（自前保存）。RTHフィルタ非適用・保存日ごとに指標計算。"
    return payload


# --- ティック再計算のメモリキャッシュ -------------------------------------
# build_chart_ticks はリクエストごとに parquet 読み → 足合成 → run_backtest（spike 有効時は
# 全ティックを走る純 Python ループ）を回す。同じ (code, date, tf, params) の再描画で毎回数秒〜
# 数十秒を焼くのが実害なので、上限付き LRU で使い回す。鍵にはティック parquet の
# (mtime_ns, size) を含めるので、録画の追記・finalize による置換があれば自動で失効する。
_CHART_CACHE: OrderedDict = OrderedDict()
_CHART_CACHE_LOCK = threading.Lock()


def _chart_cache_get(key: tuple):
    with _CHART_CACHE_LOCK:
        if key not in _CHART_CACHE:
            return None
        _CHART_CACHE.move_to_end(key)
        return _CHART_CACHE[key]


def _chart_cache_put(key: tuple, value) -> None:
    with _CHART_CACHE_LOCK:
        _CHART_CACHE[key] = value
        _CHART_CACHE.move_to_end(key)
        while len(_CHART_CACHE) > CHART_CACHE_MAX:
            _CHART_CACHE.popitem(last=False)


def build_chart_ticks(q: dict) -> dict:
    """ticks（録画ティック）のチャートペイロードを構築する（過去方向ページング対応）。

    date が来ればその 1 日（レガシー API・テスト互換）、無ければ before/days による
    過去方向ページング（ticks は 1 日が重いので 1 ページ＝TICK_PAGE_DAYS 日）。
    """
    code = q.get("code") or DEFAULT_TICK_CODE
    tf = _get_int(q, "tf", DEFAULT_TICK_TF)
    if tf not in (5, 10, 30, 60):
        raise ValueError(f"足種は 5/10/30/60 秒のいずれかです: {tf}")
    sma_periods = _parse_sma(q.get("sma"))
    slope = _get_slope(q, TICK_DEFAULTS["slope_threshold"])
    prev_diff_min = _get_float(q, "prev_diff_min", TICK_DEFAULTS["prev_diff_min"])
    rci_sign_gate = _get_int(q, "rci_sign_gate", 0)  # 未指定は 0＝無効（UI は明示送信）
    spike_depth_min = _get_float(q, "spike_depth_min", 0.0)  # トゲ反転（未指定は 0＝無効）
    spike_turn_min = _get_float(q, "spike_turn_min", 0.0)
    eff_min = _get_float(q, "eff_min", TICK_DEFAULTS["eff_min"])
    cooldown = _get_int(q, "cooldown", TICK_DEFAULTS["cooldown"])
    cost_bp = _get_float(q, "cost_bp", TICK_DEFAULTS["cost_bp"])
    boll_window, boll_z = _boll_display_params(q)  # ボリンジャーバンド描画用（表示は常時・UI で表示切替）

    all_dates = _ticks_dates_for(code)
    explicit_date = q.get("date")
    if explicit_date:
        page_dates = [explicit_date]
    else:
        page_dates, _hm = _page_from_query(all_dates, q, TICK_PAGE_DAYS)
    has_more = bool(page_dates) and any(d < page_dates[0] for d in all_dates)

    tf_label = {5: "5秒", 10: "10秒", 30: "30秒", 60: "1分"}[tf]
    meta = {
        "source": "ticks", "resolved_source": "ticks", "code": code,
        "date": (page_dates[-1] if page_dates else explicit_date),
        "tf": tf, "tf_label": tf_label, "available_tfs": [5, 10, 30, 60],
        "sma": sma_periods,
        "available_from": (all_dates[0] if all_dates else None),
        "available_to": (all_dates[-1] if all_dates else None),
        "page_from": (page_dates[0] if page_dates else None),
        "page_to": (page_dates[-1] if page_dates else None),
        "has_more": has_more,
        "params": {"rci_period": RCI_SHORT, "slope_threshold": slope, "prev_diff_min": prev_diff_min,
                   "rci_sign_gate": rci_sign_gate, "spike_depth_min": spike_depth_min,
                   "spike_turn_min": spike_turn_min, "eff_min": eff_min, "cooldown_sec": cooldown,
                   "cost_bp": cost_bp},
        "boll": {"window": boll_window, "z": boll_z},
    }
    payload = _empty_payload(sma_periods)
    payload["meta"] = meta

    if not page_dates:
        payload["message"] = (
            f"{code} のティック録画がありません。ティックは平日 22:20〜翌 05:00 に自動録画されます。"
        )
        return payload

    # エントリーは足内 RCI のティック駆動 engine（表示足とは別基準）で計算する
    tick_params = {"rci_period": RCI_SHORT, "slope_threshold": slope, "prev_diff_min": prev_diff_min,
                   "rci_sign_gate": rci_sign_gate, "spike_depth_min": spike_depth_min,
                   "spike_turn_min": spike_turn_min, "eff_min": eff_min,
                   "slope_window_sec": TICK_SLOPE_WINDOW_SEC,
                   "cooldown_sec": cooldown, "af_init": DEFAULT_AF[0],
                   "af_step": DEFAULT_AF[1], "af_max": DEFAULT_AF[2]}

    entry_frames = []
    loaded_any = False
    missing_explicit = False
    params_key = json.dumps(tick_params, sort_keys=True, default=str)
    for d in page_dates:
        path = store_loader.ticks_path(d, code)
        if not _under_data_dir(path):
            raise ValueError(f"参照先が data/ の外になります: code={code!r} date={d!r}")
        try:
            st = path.stat()
        except OSError:
            missing_explicit = True
            continue
        stamp = (st.st_mtime_ns, st.st_size)   # ファイルが変われば下のキャッシュは自動失効する

        bars_key = ("bars", code, d, tf, stamp)
        bars = _chart_cache_get(bars_key)
        if bars is None:
            bars = store_loader.resample_ticks_to_bars(store_loader.load_ticks(d, code), tf)
            _chart_cache_put(bars_key, bars)
        if bars.empty:
            continue
        _push_segment(payload, bars, sma_periods, DEFAULT_AF, boll_window, boll_z)

        ent_key = ("entries", code, d, params_key, stamp)
        ent = _chart_cache_get(ent_key)
        if ent is None:
            ent = run_backtest(d, [code], params=tick_params, horizons=DEFAULT_HORIZONS)
            _chart_cache_put(ent_key, ent)
        entry_frames.append(ent)
        loaded_any = True

    if not loaded_any:
        if missing_explicit:
            payload["message"] = (
                f"{explicit_date} の {code} のティックがありません。"
                "ティックは平日 22:20〜翌 05:00 に自動録画されます。"
            )
        else:
            payload["message"] = "ティックはありますが、この足種でのバーが生成できませんでした。"
        return payload

    entries = pd.concat(entry_frames, ignore_index=True) if entry_frames else pd.DataFrame()
    payload["entries"] = tick_entries_to_records(entries, tf)
    payload["stats"] = build_stats(entries, "tick", "ticks")
    payload["note"] = "ティックの RCI 表示は選択足種で計算。エントリーは足内RCIのティック駆動で判定。"
    return payload


def build_chart(q: dict) -> dict:
    """query dict からチャートペイロードを構築する。

    source が明示されればそれを使う（レガシー API・テスト互換）。無ければ code だけから
    自動解決する（UI にソース区別を見せない方針: JP.*→bars1m, US.*→ticks か hist1m）。
    """
    _validate_query_ids(q)
    source = (q.get("source") or "").strip().lower()
    if not source:
        source, _ = _resolve_source_and_dates((q.get("code") or "").strip())
    if source == "hist1m":
        return build_chart_hist1m(q)
    if source == "bars1m":
        return build_chart_bars1m(q)
    if source == "ticks":
        return build_chart_ticks(q)
    raise ValueError(f"未知の source: {source!r}（hist1m / bars1m / ticks を指定してください）")


# --- API ルーティング（テストから直接呼べる形） ----------------------------

def handle_api(path: str, q: dict) -> tuple[int, dict]:
    """API パスとクエリ dict を受け取り (status, json) を返す。サーバ起動不要。"""
    if path == "/api/sources":
        return 200, list_sources()
    if path == "/api/chart":
        try:
            return 200, build_chart(q)
        except ValueError as e:
            return 400, {"error": str(e)}
        except Exception as e:  # noqa: BLE001 - 予期せぬ失敗はログしつつ 500 で人間可読に
            traceback.print_exc()
            return 500, {"error": f"サーバ内部エラー: {e}"}
    if path == "/api/regime":
        try:
            date = (q.get("date") or "").strip() or None
            force = str(q.get("force") or "") == "1"
            return 200, regime_mod.judge_regime(date=date, force=force)
        except Exception as e:  # noqa: BLE001 - 予期せぬ失敗はログしつつ 500 で人間可読に
            traceback.print_exc()
            return 500, {"error": f"レジーム判定エラー: {e}"}
    if path == "/api/screens":
        return 200, list_screens()
    if path == "/api/screen":
        try:
            return 200, read_screen(q.get("run"))
        except Exception as e:  # noqa: BLE001 - 読み取り失敗は 500 で人間可読に
            traceback.print_exc()
            return 500, {"error": f"スクリーニング読込エラー: {e}"}
    if path == "/api/history":
        try:
            return 200, build_history(q)
        except ValueError as e:
            return 400, {"error": str(e)}
        except Exception as e:  # noqa: BLE001 - 予期せぬ失敗は 500 で人間可読に
            traceback.print_exc()
            return 500, {"error": f"履歴読込エラー: {e}"}
    if path == "/api/annotations":
        try:
            code = (q.get("code") or "").strip() or None
            return 200, {"annotations": _annotations_list(code)}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return 500, {"error": f"注釈読込エラー: {e}"}
    return 404, {"error": f"未知の API: {path}"}


def handle_api_post(path: str, body: dict) -> tuple[int, dict]:
    """POST API パスとボディ dict を受け取り (status, json) を返す。サーバ起動不要（テストから直接呼べる）。"""
    if path == "/api/annotations":
        try:
            return create_annotation(body)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return 500, {"error": f"注釈作成エラー: {e}"}
    if path == "/api/annotations/delete":
        try:
            return delete_annotation(body)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return 500, {"error": f"注釈削除エラー: {e}"}
    if path == "/api/screen/run":
        # 重いバックテストの二重実行を弾く（実行中は 409）。
        if not _SCREEN_RUN_LOCK.acquire(blocking=False):
            return 409, {"error": "スクリーニングを実行中です。完了までお待ちください。"}
        try:
            return 200, run_screen(body)
        except ValueError as e:
            return 400, {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return 500, {"error": f"スクリーニング実行エラー: {e}"}
        finally:
            _SCREEN_RUN_LOCK.release()
    return 404, {"error": f"未知の API: {path}"}


# --- HTTP ハンドラ ----------------------------------------------------------

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def _local_authority_ok(authority: str, port: int, hosts: frozenset) -> bool:
    """"host[:port]" が自ホスト・自ポートかを返す（Host / Origin 共通の判定）。"""
    try:
        parts = urlsplit(f"//{authority}")
        hostname, got_port = parts.hostname, parts.port
    except ValueError:  # ポートが数値でない等
        return False
    if not hostname or hostname.lower() not in hosts:
        return False
    return got_port == port if got_port is not None else port == 80


class Handler(BaseHTTPRequestHandler):
    server_version = "scalplab-webui/1.0"

    def log_message(self, fmt, *args):  # アクセスログは簡素に stderr へ
        return

    def _send_json(self, status: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local_only_ok(self) -> bool:
        """Host / Origin を検証する（ローカル専用 UI。CSRF・DNS rebinding 対策）。

        Host: 自ホスト名（ループバック＋明示 bind したアドレス）かつ自ポートでなければ拒否する。
              rebinding させた任意ドメイン名で 127.0.0.1 に着弾しても Host が合わず読めない。
        Origin: ブラウザは cross-origin 要求に必ず付けるので、付いていて自オリジンでない値
              （"null" 含む）は拒否する。curl 等の非ブラウザは Origin 無しでそのまま通る。
        """
        port = int(self.server.server_address[1])
        bound = str(self.server.server_address[0] or "")
        # --host で明示 bind したアドレス経由のアクセスまで自分で塞がないよう許可に足す。
        hosts = (LOCAL_HOSTNAMES | {bound}) if bound not in ("", "0.0.0.0", "::") else LOCAL_HOSTNAMES
        if not _local_authority_ok(self.headers.get("Host") or "", port, hosts):
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https"):
            return False
        return _local_authority_ok(parts.netloc, port, hosts)

    def _send_forbidden(self):
        self._send_json(403, {"error": "ローカル（localhost）以外からの要求は受け付けません"})

    def _send_static(self, rel: str):
        # ディレクトリトラバーサル防止（STATIC_DIR 配下に限定）。startswith の前方一致だと
        # 兄弟ディレクトリ（static-backup 等）が接頭辞一致で通るので relative_to で判定する。
        root = STATIC_DIR.resolve()
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self._send_json(404, {"error": f"not found: {rel}"})
            return
        if not target.is_file():
            self._send_json(404, {"error": f"not found: {rel}"})
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._local_only_ok():
            self._send_forbidden()
            return
        parsed = urlparse(self.path)
        path = parsed.path
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if path.startswith("/api/"):
            status, obj = handle_api(path, q)
            self._send_json(status, obj)
            return
        if path in ("/", "/index.html"):
            self._send_static("index.html")
            return
        if path.startswith("/static/"):
            self._send_static(path[len("/static/"):])
            return
        self._send_json(404, {"error": f"not found: {path}"})

    def do_POST(self):
        if not self._local_only_ok():
            self._send_forbidden()
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            self._send_json(404, {"error": f"not found: {path}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "リクエストボディが不正な JSON です"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": "リクエストボディは JSON オブジェクトで送ってください"})
            return
        status, obj = handle_api_post(path, body)
        self._send_json(status, obj)


def serve(host: str = "127.0.0.1", port: int = 8025):
    """サーバを起動してブロックする。"""
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"scalp-lab webui: http://{host}:{port}  (Ctrl-C で停止)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。")
    finally:
        httpd.server_close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="scalp-lab バックテスト・エントリー確認 Web UI")
    ap.add_argument("--host", default="127.0.0.1", help="バインドするホスト（既定 127.0.0.1）")
    ap.add_argument("--port", type=int, default=8025, help="ポート（既定 8025）")
    args = ap.parse_args(argv)
    serve(args.host, args.port)
    return 0
