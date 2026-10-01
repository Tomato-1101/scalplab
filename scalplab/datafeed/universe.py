"""録画対象（universe）の決定。

方針:
- 銘柄マスタは OpenD の basicinfo（東証・普通株のみ）。data/universe/master.csv にキャッシュし、
  OpenD 不達時はキャッシュを使う。
- 売買代金ランキングは yfinance（.T サフィックス）の前営業日 Close×Volume。全 3,750 銘柄を毎日
  スキャンするのは重いので、週1回のフルスキャンで上位の「流動プール」（data/universe/pool.csv）を
  作り、毎日はプールだけを再ランキングする。yfinance がネットワーク失敗した場合は直近の
  universe ファイルにフォールバックする（録画は止めない）。
- 出力は data/universe/YYYY-MM-DD.yaml（codes と選定根拠）。fixed → auto の順で max_symbols にクリップ。

日本株の相場権限が無い間も本モジュールは動く（マスタ取得・ランキングは OpenD 相場権限に依存しない）。
"""
from __future__ import annotations

import argparse
import logging
from datetime import date as date_t
from pathlib import Path

import pandas as pd
import yaml

from futu import Market, SecurityType

from . import jp_tick_ranking
from .moomoo_client import MoomooClient, NoPermissionError, OpenDUnavailableError

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "universe.yaml"
CONFIG_US_PATH = PROJECT_ROOT / "config" / "universe_us.yaml"
UNIVERSE_DIR = PROJECT_ROOT / "data" / "universe"
MASTER_CSV = UNIVERSE_DIR / "master.csv"
POOL_CSV = UNIVERSE_DIR / "pool.csv"
TICKS_DIR = PROJECT_ROOT / "data" / "ticks"   # 録画済みティック（tick_count ランキングの実績元）

DEFAULT_POOL_SIZE = 300
DEFAULT_SCAN_CHUNK = 200


# --- 設定 ---

def load_config(path: Path = CONFIG_PATH) -> dict:
    """config/universe.yaml を読む。欠損キーは既定で補う。"""
    cfg = {}
    if path.exists():
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("fixed", [])
    cfg.setdefault("auto_top_n", 40)
    cfg.setdefault("max_symbols", 48)
    cfg.setdefault("pool_size", DEFAULT_POOL_SIZE)
    cfg.setdefault("scan_chunk", DEFAULT_SCAN_CHUNK)
    # auto ランキングの供給源。"matsui_tick"＝松井 Tick回数ランキング（本物の約定回数）、
    # None（またはその他）＝従来の売買代金プロキシ（yfinance Close×Volume）。
    cfg.setdefault("ranking_source", "matsui_tick")
    return cfg


# --- コード変換 ---

def moomoo_to_yf(code: str) -> str:
    """moomoo コード（JP.7203）を yfinance ティッカー（7203.T）に変換する。"""
    if code.startswith("JP."):
        return f"{code[3:]}.T"
    # US 等はそのまま数字部分ではなくシンボルを返す（JP 以外は補助用途）
    return code.split(".", 1)[-1]


# --- 銘柄マスタ ---

def load_master(use_cache_on_fail: bool = True) -> pd.DataFrame:
    """東証・普通株の銘柄マスタ（code,name,exchange_type）を返す。

    OpenD basicinfo を優先し master.csv にキャッシュ。OpenD 不達・権限不足時はキャッシュを使う。
    """
    try:
        client = MoomooClient()
        client.connect()
        try:
            df = client.get_stock_basicinfo(Market.JP, SecurityType.STOCK)
        finally:
            client.close()
        # 東証（JP_TSE）の普通株だけに絞る
        df = df[df["exchange_type"] == "JP_TSE"]
        master = df[["code", "name", "exchange_type"]].copy().reset_index(drop=True)
        UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)
        master.to_csv(MASTER_CSV, index=False)
        log.info("銘柄マスタを OpenD から取得: %d 件（%s に保存）", len(master), MASTER_CSV)
        return master
    except (OpenDUnavailableError, NoPermissionError) as e:
        if use_cache_on_fail and MASTER_CSV.exists():
            log.warning("OpenD からマスタ取得不可（%s）。キャッシュを使用: %s", e, MASTER_CSV)
            return pd.read_csv(MASTER_CSV, dtype={"code": str})
        raise


# --- ランキング（yfinance） ---

def rank_by_turnover(codes: list[str], prev_days: int = 5) -> list[tuple[str, float]]:
    """yfinance で各 code の直近営業日の売買代金（Close×Volume）を計算し降順で返す。

    ネットワーク・データ欠損した銘柄はスキップ。yfinance は遅延 EOD だがランキング用途には十分。
    yfinance 未導入・全滅時は空リストを返す（呼び手がフォールバックを判断する）。
    """
    try:
        import yfinance as yf  # type: ignore[import-not-found]
    except ImportError:
        log.warning("yfinance 未導入のためランキングをスキップ（pip install yfinance）")
        return []

    yf_map = {moomoo_to_yf(c): c for c in codes}
    tickers = list(yf_map)
    if not tickers:
        return []
    data = yf.download(
        tickers,
        period=f"{prev_days}d",
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    results: list[tuple[str, float]] = []
    for yft, code in yf_map.items():
        try:
            df = data[yft] if len(tickers) > 1 else data
            df = df.dropna()
            if df.empty:
                continue
            last = df.iloc[-1]
            turnover = float(last["Close"]) * float(last["Volume"])
            if turnover > 0:
                results.append((code, turnover))
        except (KeyError, ValueError, TypeError):
            continue
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def jp_auto_ranked(date: str, cfg: dict, pool_codes: list[str]) -> list[tuple[str, float]]:
    """日本株の auto ランキング (code, スコア) を降順で返す。

    ranking_source=="matsui_tick" のときは松井の Tick回数ランキング（本物の約定回数）を優先し、
    取得不可なら jp_tick_ranking 内で「直近保存」までフォールバックする。そこも空なら、さらに
    従来の売買代金プロキシ（pool を yfinance で再ランキング）へ落とす。どのソースを使ったかは
    ログに残す（録画開始 8:50 に取れるのは前営業日ベースのランキングでよい＝それが最新）。
    """
    if cfg.get("ranking_source") == "matsui_tick":
        rows, src = jp_tick_ranking.get_ranking(date)
        if rows:
            log.info("JP auto ランキング: 松井Tick回数を使用（%s, %d件）", src, len(rows))
            return [(r["code"], float(r["tick_count"])) for r in rows]
        log.warning("JP auto ランキング: 松井Tick回数が使えず売買代金プロキシへフォールバック")
    return rank_by_turnover(pool_codes) if pool_codes else []


def full_scan(master_codes: list[str], pool_size: int, chunk: int) -> list[tuple[str, float]]:
    """全銘柄を chunk 分割でランキングし、上位 pool_size を流動プールとして pool.csv に保存する。"""
    ranked: list[tuple[str, float]] = []
    for i in range(0, len(master_codes), chunk):
        part = master_codes[i : i + chunk]
        ranked.extend(rank_by_turnover(part))
        log.info("フルスキャン進捗: %d/%d 銘柄", min(i + chunk, len(master_codes)), len(master_codes))
    ranked.sort(key=lambda x: x[1], reverse=True)
    pool = ranked[:pool_size]
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(pool, columns=["code", "turnover"]).to_csv(POOL_CSV, index=False)
    log.info("流動プールを更新: %d 件（%s）", len(pool), POOL_CSV)
    return pool


def load_pool() -> list[str]:
    """流動プールの code 一覧を返す（無ければ空）。"""
    if not POOL_CSV.exists():
        return []
    return pd.read_csv(POOL_CSV, dtype={"code": str})["code"].tolist()


# --- 選定（純粋関数・テスト対象） ---

def select_universe(
    fixed: list[str],
    auto_ranked: list[tuple[str, float]],
    auto_top_n: int,
    max_symbols: int,
) -> tuple[list[str], dict]:
    """fixed → auto の順で銘柄を選び、max_symbols にクリップする。

    - fixed は重複を除いて先頭に置く（ユーザー指定を最優先）。
    - auto は fixed に含まれない上位 auto_top_n。
    - 合計を max_symbols で打ち切る（fixed が超える場合は fixed だけで打ち切る）。
    戻り値は (codes, 選定根拠 dict)。
    """
    seen: set[str] = set()
    fixed_unique: list[str] = []
    for c in fixed:
        if c not in seen:
            seen.add(c)
            fixed_unique.append(c)

    auto_selected: list[tuple[str, float]] = []
    for code, turnover in auto_ranked:
        if len(auto_selected) >= auto_top_n:
            break
        if code in seen:
            continue
        seen.add(code)
        auto_selected.append((code, turnover))

    codes = (fixed_unique + [c for c, _ in auto_selected])[:max_symbols]
    codes_set = set(codes)
    rationale = {
        "fixed": [c for c in fixed_unique if c in codes_set],
        "auto": [
            {"code": c, "turnover": round(t, 2)}
            for c, t in auto_selected
            if c in codes_set
        ],
        "auto_top_n": auto_top_n,
        "max_symbols": max_symbols,
    }
    return codes, rationale


# --- ビルド ---

def _latest_universe_before(date: str) -> Path | None:
    """指定日より前で最新の universe yaml を返す（フォールバック用）。"""
    if not UNIVERSE_DIR.exists():
        return None
    candidates = sorted(
        p for p in UNIVERSE_DIR.glob("*.yaml") if p.stem < date
    )
    return candidates[-1] if candidates else None


def build(date: str, do_full_scan: bool = False, config_path: Path = CONFIG_PATH) -> dict:
    """その日の universe を決定し data/universe/YYYY-MM-DD.yaml に書く。戻り値は書いた内容。"""
    cfg = load_config(config_path)
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)

    # プールを用意（フルスキャン指定 or プール未作成なら全銘柄スキャン）
    pool_codes = load_pool()
    if do_full_scan or not pool_codes:
        try:
            master = load_master()
            full_scan(master["code"].tolist(), cfg["pool_size"], cfg["scan_chunk"])
            pool_codes = load_pool()
        except Exception as e:  # noqa: BLE001 - マスタ取得やスキャンの失敗は下でフォールバック
            log.warning("フルスキャン失敗: %s", e)

    # 毎日: auto ランキングを解決（松井Tick回数優先 → 売買代金プロキシ）。空なら直近ファイルへ。
    auto_ranked = jp_auto_ranked(date, cfg, pool_codes)
    if not auto_ranked:
        prev = _latest_universe_before(date)
        if prev is not None:
            prev_doc = yaml.safe_load(prev.read_text(encoding="utf-8")) or {}
            doc = {
                "date": date,
                "codes": prev_doc.get("codes", []),
                "rationale": {"source": "fallback_previous", "from": prev.name},
            }
            out = universe_yaml_path(date)
            out.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
            log.warning("ランキング不可のため %s にフォールバック: %d 銘柄", prev.name, len(doc["codes"]))
            return doc
        log.error("ランキングもフォールバック先も無い。fixed のみで構成する。")

    codes, rationale = select_universe(
        cfg["fixed"], auto_ranked, cfg["auto_top_n"], cfg["max_symbols"]
    )
    doc = {"date": date, "codes": codes, "rationale": rationale}
    out = universe_yaml_path(date)
    out.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    log.info("universe を書き出し: %s（%d 銘柄）", out, len(codes))
    return doc


def universe_yaml_path(date: str) -> Path:
    return UNIVERSE_DIR / f"{date}.yaml"


def resolve_codes(date: str, config_path: Path = CONFIG_PATH) -> list[str]:
    """その日の録画対象 code を返す。yaml が無ければ build して作る（recorder から使う）。"""
    p = universe_yaml_path(date)
    if p.exists():
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return doc.get("codes", [])
    doc = build(date, config_path=config_path)
    return doc.get("codes", [])


# --- 米国株 universe（固定リストのみ・自動ランキングなし） ---
#
# 米国株はデイトレ向けの固定リスト（config/universe_us.yaml の fixed）だけで開始する。
# 日本株のような売買代金ランキングは行わない（ユーザー要件）。JP と衝突しないよう
# universe ファイルは data/universe/US-YYYY-MM-DD.yaml に書き出す。

def load_config_us(path: Path = CONFIG_US_PATH) -> dict:
    """config/universe_us.yaml を読む。欠損キーは既定で補う。"""
    cfg = {}
    if path.exists():
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("fixed", [])
    cfg.setdefault("max_symbols", 48)
    cfg.setdefault("ranking", None)   # None / "tick_count"
    cfg.setdefault("core", 0)         # 常時コアとして先頭から確保する fixed 銘柄数
    return cfg


def universe_us_yaml_path(date: str) -> Path:
    return UNIVERSE_DIR / f"US-{date}.yaml"


# --- ティック回数ランキング（録画実績ベースの選定） ---
#
# 前回録画セッションの実ティック数（data/ticks/<直近NYセッション日>/ の各 parquet 行数）で
# 非コア銘柄を降順に並べ替え、上位から埋める。録画が活発だった銘柄を継続録画するのが狙い。
# universe は必ず fixed の範囲内に収める（購読クォータ・許可銘柄の制約を守るため）。

def latest_tick_session(ticks_dir: Path | None = None) -> str | None:
    """data/ticks/ 直下で最新の録画セッション日（YYYY-MM-DD）を返す。parquet を1つも持たない
    日付ディレクトリは無視する。録画実績が1つも無ければ None。"""
    d = ticks_dir if ticks_dir is not None else TICKS_DIR
    if not d.exists():
        return None
    valid = [x.name for x in d.iterdir() if x.is_dir() and any(x.glob("*.parquet"))]
    return sorted(valid)[-1] if valid else None


def _parquet_row_count(path: Path) -> int:
    """parquet の行数をメタデータから O(1) で取得する（本体を読み込まない）。"""
    import pyarrow.parquet as pq
    return int(pq.ParquetFile(path).metadata.num_rows)


def tick_counts(session: str, ticks_dir: Path | None = None) -> dict[str, int]:
    """指定セッション日の各 code の実ティック数（parquet 行数）を返す。"""
    d = (ticks_dir if ticks_dir is not None else TICKS_DIR) / session
    counts: dict[str, int] = {}
    if not d.exists():
        return counts
    for p in sorted(d.glob("*.parquet")):
        try:
            counts[p.stem] = _parquet_row_count(p)
        except Exception as e:  # noqa: BLE001 - 壊れた parquet は 0 扱いで録画を止めない
            log.warning("ティック行数の取得に失敗 %s: %s", p, e)
    return counts


def select_universe_tick_ranked(
    fixed: list[str],
    tick_counts_map: dict[str, int],
    core: int,
    max_symbols: int,
) -> tuple[list[str], dict]:
    """fixed の先頭 core をコアとして確保し、残りをティック数降順（同数は fixed 順）で
    max_symbols まで埋める。

    - fixed は重複を除いてから使う。
    - core 銘柄はティック数に関わらず必ず採用する（常時録画したいコア）。
    - 非コアは前回セッションのティック数が多い順。未録画（count 無し）は 0 扱いで末尾に寄る。
    戻り値は (codes, 選定根拠 dict)。
    """
    seen: set[str] = set()
    fixed_unique: list[str] = []
    for c in fixed:
        if c not in seen:
            seen.add(c)
            fixed_unique.append(c)

    core_n = max(0, min(int(core), len(fixed_unique)))
    core_codes = fixed_unique[:core_n]
    rest = fixed_unique[core_n:]
    # 安定ソート: Python の sorted は reverse=True でも同順キーの元順序を保つ（＝fixed 順）。
    rest_ranked = sorted(rest, key=lambda c: tick_counts_map.get(c, 0), reverse=True)

    codes = (core_codes + rest_ranked)[:max_symbols]
    codes_set = set(codes)
    rationale = {
        "core": [c for c in core_codes if c in codes_set],
        "ranked": [
            {"code": c, "ticks": int(tick_counts_map.get(c, 0))}
            for c in rest_ranked if c in codes_set
        ],
        "core_n": core_n,
        "max_symbols": max_symbols,
    }
    return codes, rationale


def build_us(date: str, config_path: Path = CONFIG_US_PATH, ticks_dir: Path | None = None) -> dict:
    """その日の US universe を決定し US-YYYY-MM-DD.yaml に書く。戻り値は書いた内容。

    config の ranking が "tick_count" かつ前回録画セッションが存在すれば、実ティック数で
    非コア銘柄を並べ替えて選定する。録画実績が1日も無い（初回夜）場合や ranking 無効時は
    従来どおり fixed のみ（先頭から max_symbols）で構成する。
    """
    cfg = load_config_us(config_path)
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)

    if cfg.get("ranking") == "tick_count":
        session = latest_tick_session(ticks_dir)
        if session:
            counts = tick_counts(session, ticks_dir)
            codes, rationale = select_universe_tick_ranked(
                cfg["fixed"], counts, cfg["core"], cfg["max_symbols"]
            )
            rationale.update(source="tick_count", session=session)
            doc = {"date": date, "market": "US", "codes": codes, "rationale": rationale}
            out = universe_us_yaml_path(date)
            out.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
            log.info("US universe を書き出し（tick_count/%s）: %s（%d 銘柄）", session, out, len(codes))
            return doc
        log.info("録画実績が無いため fixed のみで US universe を構成する（初回夜）")

    # フォールバック: 自動ランキング無し（auto_ranked=[]）。fixed を重複除去して max_symbols でクリップ。
    codes, rationale = select_universe(cfg["fixed"], [], auto_top_n=0, max_symbols=cfg["max_symbols"])
    doc = {
        "date": date,
        "market": "US",
        "codes": codes,
        "rationale": {"source": "fixed_only", "fixed": rationale["fixed"], "max_symbols": cfg["max_symbols"]},
    }
    out = universe_us_yaml_path(date)
    out.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    log.info("US universe を書き出し: %s（%d 銘柄）", out, len(codes))
    return doc


def resolve_codes_us(date: str, config_path: Path = CONFIG_US_PATH) -> list[str]:
    """その日の US 録画対象 code を返す。yaml が無ければ build_us で作る（recorder から使う）。"""
    p = universe_us_yaml_path(date)
    if p.exists():
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return doc.get("codes", [])
    doc = build_us(date, config_path=config_path)
    return doc.get("codes", [])


def main() -> None:
    parser = argparse.ArgumentParser(description="録画対象 universe を決定する")
    parser.add_argument("--date", default=date_t.today().isoformat(), help="対象営業日 YYYY-MM-DD")
    parser.add_argument("--full-scan", action="store_true", help="全銘柄で流動プールを再構築する")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    doc = build(args.date, do_full_scan=args.full_scan)
    print(f"{args.date}: {len(doc.get('codes', []))} 銘柄")
    for c in doc.get("codes", []):
        print(f"  {c}")


if __name__ == "__main__":
    main()
