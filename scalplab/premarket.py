"""寄り前気配の記録（kabuステーション API・Windows 実行機用）。

ギャップリバーサル戦略のエッジは 09:00 の板寄せに集中しており、実行には 08:00〜09:00 の
気配を見て寄り成行を入れる必要がある（本戦略で未解決の最大論点）。その前段として、
まず「8時台の気配から寄り値がどれだけ予測できるか」を実測するための生データをここで録る。

方式: `GET /board` を巡回ポーリングする。**/board は内部で銘柄を自動登録して register の
50 枠を消費する**（2026-08-19 実機で判明。51 銘柄目以降は `Code 4002006 レジスト数エラー`。
実装当初の「register 不要＝50銘柄制限を受けない」という前提は誤りだった）。そのため銘柄を
50 件ずつのバッチに割り、**各バッチの直前に `PUT /unregister/all` で枠を空ける**ローテーションで回す
（実機実証: 291 銘柄 = 6 バッチで約 6 分/周・レジスト数エラーゼロ）。
/board は応答が遅い（単発で約1秒 = kabusapi issue #214。実測も中央値 863ms）ため 2 段構成にする:

  - フェーズ all   (collect_start〜focus_start): 全銘柄をラウンドロビン巡回（1周 約6分）
  - フェーズ focus (focus_start〜end)          : その時点の気配で単純ギャップ率
    （(気配 − 前日終値) / 前日終値）下位 focus_n 銘柄だけを高頻度巡回（寄り直前を密に録る）

フェーズ判定は**バッチ（約50秒）ごと**に行う。1 周（約6分）ごとだと focus 開始が最大 6 分遅れ、
寄り直前のいちばん重要な時間帯を取り逃すため。

保存: data/premarket/YYYY-MM-DD.parquet（.gitignore 済み。1 日およそ 50MB のためリポジトリには入れず、
拠点間は圧縮して別経路で渡す）。主要フィールドはフラット列に、全フィールドは raw(JSON 文字列) 列に残す
（kabu の応答のどのフィールドが寄り前に使えるかが未確定のため。parquet 圧縮で実サイズは小さい）。

注意（kabu API の罠）: /board の **BidPrice は「売」気配、AskPrice は「買」気配**（名前と逆）。
フィールド名は API のまま保存し、解釈は分析側（Mac）で行う。

発注はしない。読み取りのみ。

実行（Windows）:
  .venv\\Scripts\\python -m scalplab.premarket            # 平日 07:55 にタスクスケジューラから起動
  .venv\\Scripts\\python -m scalplab.premarket --check    # 1銘柄だけ /board して応答を表示（疎通確認）
  .venv\\Scripts\\python -m scalplab.premarket --duration 60  # 今から60秒だけ全銘柄巡回（時間帯無視のデバッグ）
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import yaml

from scalplab.live import (KabuStationTickSource, TickSourceUnavailableError,
                           load_live_config)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "premarket.yaml"
OUT_DIR = PROJECT_ROOT / "data" / "premarket"
LOG_DIR = PROJECT_ROOT / "logs"

log = logging.getLogger("scalplab.premarket")

# /board 応答から素直に持つスカラー列（存在しないキーは None のまま保存する）
_SCALAR_KEYS = [
    "Symbol", "SymbolName", "CurrentPrice", "CurrentPriceTime", "CurrentPriceStatus",
    "CalcPrice", "PreviousClose", "PreviousCloseTime", "OpeningPrice", "OpeningPriceTime",
    "TradingVolume", "TradingVolumeTime", "VWAP", "TradingValue",
    "BidQty", "BidPrice", "BidSign", "AskQty", "AskPrice", "AskSign",
    "OverSellQty", "UnderBuyQty", "MarketOrderSellQty", "MarketOrderBuyQty",
]
# 板は上位3本だけフラット化する（10本全ては raw に残る）
_DEPTH_LEVELS = 3
# kabu API の銘柄登録枠の上限。/board もこの枠を消費するので巡回はこの単位でローテーションする
_BOARD_SLOT_LIMIT = 50


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for key in ("collect_start", "focus_start", "end", "codes"):
        if key not in cfg:
            raise ValueError(f"config/premarket.yaml に {key} がありません")
    return cfg


def flatten_board(code: str, data: dict, phase: str, fetched_at: datetime) -> dict:
    """/board の応答1件を保存行にする。未知フィールドは raw に全部残す。"""
    row: dict = {"fetched_at": fetched_at, "code": code, "phase": phase}
    for k in _SCALAR_KEYS:
        row[k.lower()] = data.get(k)
    for side in ("Sell", "Buy"):
        for i in range(1, _DEPTH_LEVELS + 1):
            d = data.get(f"{side}{i}") or {}
            row[f"{side.lower()}{i}_price"] = d.get("Price")
            row[f"{side.lower()}{i}_qty"] = d.get("Qty")
            row[f"{side.lower()}{i}_sign"] = d.get("Sign")
    row["raw"] = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return row


def indicative_price(row: dict) -> Optional[float]:
    """寄り前の「気配価格」の最良推定。どのフィールドに出るかが実機未確定なので順に当たる。

    CalcPrice（板寄せの計算価格）→ 最良売/買気配 → CurrentPrice の順。正の値が取れたものを使う。
    """
    for key in ("calcprice", "sell1_price", "buy1_price", "currentprice"):
        v = row.get(key)
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def select_focus(rows: list[dict], codes: list[str], focus_n: int) -> list[str]:
    """フェーズ all の最新スナップショットから、単純ギャップ率の下位 focus_n 銘柄を選ぶ。

    σ 正規化（gap_sigma）は日足の蓄積が要るので分析側（Mac）で行い、実行時は
    (気配 − 前日終値) / 前日終値 の単純ギャップで絞る。気配が取れていない銘柄は選ばない。
    """
    latest: dict[str, dict] = {}
    for r in rows:  # rows は時系列順なので後勝ちで最新になる
        latest[r["code"]] = r
    gaps: list[tuple[float, str]] = []
    for code in codes:
        r = latest.get(code)
        if not r:
            continue
        price = indicative_price(r)
        prev = r.get("previousclose")
        try:
            prev = float(prev) if prev is not None else None
        except (TypeError, ValueError):
            prev = None
        if price is None or not prev or prev <= 0:
            continue
        gaps.append(((price - prev) / prev, code))
    gaps.sort()
    return [c for _, c in gaps[:focus_n]]


def _parse_hm(day: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{day} {hm}:00")


def _out_path(day: str) -> Path:
    return OUT_DIR / f"{day}.parquet"


def _save(rows: list[dict], day: str) -> Path:
    """同日 parquet が既にあればマージして保存する（上書きしない）。

    5 分リトライ設計（premarket_task.cmd）ではプロセスが落ちて別プロセスが同日を
    録り直すことがあり、上書き型だと前プロセスの途中保存が消える（2026-08-21 実害:
    94 行を手動退避で復旧）。既存分と結合し (fetched_at, code) の重複だけ除く。
    同一プロセスの途中保存は rows 全体を毎回書くため、重複除去で従来と同じ内容になる。
    """
    path = _out_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    if path.exists():
        try:
            prev = pd.read_parquet(path)
            df = pd.concat([prev, df], ignore_index=True)
            df = df.drop_duplicates(subset=["fetched_at", "code"], keep="last")
        except Exception as e:  # noqa: BLE001 - 壊れた既存ファイルで収集を止めない
            log.warning("既存 %s とのマージに失敗（今回分のみで保存する）: %s", path.name, e)
    df.to_parquet(path, index=False)
    return path


def collect(cfg: dict, client, *, now_fn: Callable[[], datetime] = datetime.now,
            sleep_fn: Callable[[float], None] = time.sleep,
            duration: Optional[float] = None) -> Path:
    """巡回本体。duration 指定時は「今から duration 秒・全銘柄巡回のみ」（デバッグ用）。

    client は KabuStationTickSource（connect 済み）。board_snapshot(code) と unregister_all() を使う。
    巡回は 50 銘柄（_BOARD_SLOT_LIMIT）ごとのバッチに割り、各バッチの直前に unregister_all() で
    登録枠を空ける（/board が枠を消費するため。モジュール docstring 参照）。
    """
    codes: list[str] = list(cfg["codes"])
    throttle = float(cfg.get("throttle_sec", 0.15))
    snapshot_every = float(cfg.get("snapshot_every_sec", 300))
    day = now_fn().strftime("%Y-%m-%d")

    if duration is not None:
        t_start = now_fn()
        t_focus = t_end = t_start + timedelta(seconds=duration)
    else:
        t_start = _parse_hm(day, cfg["collect_start"])
        t_focus = _parse_hm(day, cfg["focus_start"])
        t_end = _parse_hm(day, cfg["end"])

    now = now_fn()
    if now >= t_end:
        log.error("終了時刻 %s を過ぎている（now=%s）。何もせず終了する。", t_end, now)
        return _out_path(day)
    if now < t_start:
        wait = (t_start - now).total_seconds()
        log.info("収集開始 %s まで %.0f 秒待機する", t_start.strftime("%H:%M"), wait)
        sleep_fn(wait)

    rows: list[dict] = []
    phase = "all"
    active = codes
    cursor = 0  # active の何番目からを次のバッチにするか
    errors = 0
    last_snapshot = now_fn()
    log.info("収集開始: %d 銘柄 / focus %s から下位 %s 銘柄 / 終了 %s",
             len(codes), cfg["focus_start"], cfg.get("focus_n", 50), cfg["end"])

    while now_fn() < t_end:
        # フェーズ判定・終了判定はバッチ境界（約50秒）ごとに行う。1周（約6分）ごとだと
        # focus 開始が最大6分遅れ、寄り直前のいちばん密に録りたい時間帯を落とす。
        if phase == "all" and now_fn() >= t_focus:
            focus = select_focus(rows, codes, int(cfg.get("focus_n", 50)))
            if focus:
                phase, active, cursor = "focus", focus, 0
                log.info("focus フェーズへ移行: 下位 %d 銘柄（例: %s）", len(focus), ", ".join(focus[:5]))
            else:  # 気配が1件も取れていない＝続けても選べないので all のまま粘る
                log.warning("focus 選定不能（気配が未取得）。全銘柄巡回を続ける")
                t_focus = now_fn() + timedelta(seconds=60)  # 1分後に再判定
        batch = active[cursor:cursor + _BOARD_SLOT_LIMIT]
        cursor += _BOARD_SLOT_LIMIT
        if cursor >= len(active):
            cursor = 0  # 1周したら先頭へ戻る
        # /board は内部で銘柄を自動登録して 50 枠を消費するので、毎バッチの直前に無条件で枠を空ける
        # （登録状態を自前で追跡しない＝実機検証済みの scripts/probe_kabu_rotation.py と同じ方式）。
        try:
            status, data = client.unregister_all()
            if status != 200:
                log.warning("unregister/all status=%d resp=%s（枠が空かない可能性。巡回は続行）",
                            status, data)
        except Exception as e:  # noqa: BLE001 - 失敗しても次のバッチで自己回復するので止めない
            log.warning("unregister/all 失敗: %s（巡回は続行）", e)
        for code in batch:
            if now_fn() >= t_end:
                break
            try:
                status, data = client.board_snapshot(code)
            except Exception:  # noqa: BLE001 - 1銘柄の失敗で巡回を止めない
                # 寄り前（特に 09:00 より前）はサーバが重くタイムアウトが集中する
                # （2026-08-28 実測: エラー 89 件の大半が序盤で、09:00 以降は正常化）。
                # 1 回だけ即時に再試行し、それでも失敗なら次の銘柄へ。
                try:
                    status, data = client.board_snapshot(code)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    if errors <= 5 or errors % 100 == 0:
                        log.warning("board %s 失敗(%d件目): %s", code, errors, e)
                    continue
            if status == 200:
                rows.append(flatten_board(code, data, phase, now_fn()))
            else:
                errors += 1
                if errors <= 5 or errors % 100 == 0:
                    log.warning("board %s status=%d resp=%s (%d件目)", code, status, data, errors)
            sleep_fn(throttle)
        if rows and (now_fn() - last_snapshot).total_seconds() >= snapshot_every:
            path = _save(rows, day)
            last_snapshot = now_fn()
            log.info("途中保存: %d 行 → %s", len(rows), path.name)

    path = _save(rows, day)
    log.info("収集終了: %d 行 / エラー %d 件 → %s", len(rows), errors, path)
    return path


def _setup_logging(day: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(LOG_DIR / f"premarket-{day}.log", encoding="utf-8")],
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="寄り前気配の記録（kabuステーション /board 巡回）")
    ap.add_argument("--check", action="store_true", help="1銘柄だけ /board して応答を表示（疎通確認）")
    ap.add_argument("--duration", type=float, default=None,
                    help="今から N 秒だけ全銘柄巡回（時間帯を無視するデバッグ用）")
    args = ap.parse_args()

    day = datetime.now().strftime("%Y-%m-%d")
    _setup_logging(day)
    if datetime.now().weekday() >= 5 and not args.check and args.duration is None:
        log.info("土日なので何もせず終了する")
        return 0

    cfg = load_config()
    live_cfg = load_live_config()  # live.local.yaml があればマシン固有設定で上書きされる
    client = KabuStationTickSource(live_cfg, auto_stream=False)
    # KabuS 起動直後は API ポートが開くまで 1 分超かかり、初回接続が拒否される
    # （2026-09-01 実測: 08:50:55 アプリ起動 → 08:51 接続拒否 → 08:55 成功）。ここで即死すると
    # 5 分後のタスク再実行まで寄り前の時間を丸ごと失い、focus 選定の材料（気配 1 周目）が
    # 間に合わなくなるので、短い間隔で粘る。--check は疎通確認なので即答する。
    deadline = time.monotonic() + (0 if args.check else 180)
    while True:
        try:
            client.connect()
            break
        except TickSourceUnavailableError as e:
            if time.monotonic() >= deadline:
                log.error("kabuステーション へ接続できない: %s", e)
                return 1
            log.warning("接続失敗（%s）。10 秒後に再試行する", e)
            time.sleep(10)

    if args.check:
        code = cfg["codes"][0]
        status, data = client.board_snapshot(code)
        print(f"board {code}: status={status}")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
        return 0 if status == 200 else 1

    collect(cfg, client, duration=args.duration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
