"""IPO 仮説の検証母集団を、再取得可能な公開情報だけから作る。"""

from __future__ import annotations

import re
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf


CACHE_DIR = Path("data/.cache")
IPO_LIST_PATH = CACHE_DIR / "ipo-list.csv"
IPO_DAILY_PATH = CACHE_DIR / "ipo-daily.parquet"
USER_AGENT = "Mozilla/5.0"
JPX_URLS = {
    2026: "https://www.jpx.co.jp/listing/stocks/new/index.html",
    2025: "https://www.jpx.co.jp/listing/stocks/new/00-archives-01.html",
    2024: "https://www.jpx.co.jp/listing/stocks/new/00-archives-02.html",
    2023: "https://www.jpx.co.jp/listing/stocks/new/00-archives-03.html",
    2022: "https://www.jpx.co.jp/listing/stocks/new/00-archives-04.html",
}
IPOKISO_URLS = [f"https://www.ipokiso.com/company/{year}.html" for year in range(2019, 2022)]
DATE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}$")
CODE_RE = re.compile(r"^\d{3}[0-9A-Z]$")
IPOKISO_CODE_RE = re.compile(r"\((\d{4})\)")
# 2022-04-04 の市場再編前（マザーズ・JASDAQ・一部/二部）も 2022 年ページに混在する
MARKETS = ("プライム", "スタンダード", "グロース", "マザーズ", "JASDAQ", "第一部", "第二部")
IGNORED_NAME_TEXT = (
    "代表者インタビュー",
    "会社概要",
    "上場日",
    "コード",
    "市場区分",
    "主幹事",
)


def fetch_html(url: str) -> str:
    """取得元の文字コード判定と HTTP エラーを曖昧なまま後段へ渡さない。"""
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    return response.text


def clean_cell(value: object) -> str:
    """rowspan 由来の欠損や余白差で、同一銘柄の突き合わせが壊れないよう正規化する。"""
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def company_name(cell: str) -> str:
    """会社名セルに連結されるリンク文言（代表者インタビュー等）を取り除く。"""
    name = cell
    for label in IGNORED_NAME_TEXT:
        name = name.replace(label, "")
    return name.strip()


def parse_jpx_page(html: str) -> list[dict[str, object]]:
    """JPX の2行組（1行目=コード、2行目=市場区分）を（日付、会社名）で突き合わせる。

    セル結合の影響で「上場日と承認日」「会社名とリンク文言」は同一セルに入り、
    公募価格などの4桁数字がコードの正規表現に誤マッチするため、全セル走査ではなく
    「コード/市場区分は3列目」という列位置で読む（2022〜2026 の全ページで実測確認）。
    """
    table = pd.read_html(StringIO(html))[0]
    fragments: list[dict[str, str | None]] = []

    for raw_row in table.itertuples(index=False, name=None):
        cells = [clean_cell(value) for value in raw_row]
        if len(cells) < 3:
            continue
        date_match = re.match(r"^(\d{4}/\d{2}/\d{2})", cells[0])
        date = date_match.group(1) if date_match else ""
        ident = cells[2]
        code = ident if CODE_RE.fullmatch(ident) else None
        market = next((market for market in MARKETS if market in ident), None)
        is_pro_market = "TOKYO PRO Market" in ident
        name = company_name(cells[1])

        if date and name and (code or market or is_pro_market):
            fragments.append(
                {"jpx_date": date, "name": name, "code": code, "market": market, "is_pro": is_pro_market}
            )

    merged: dict[tuple[str, str], dict[str, object]] = {}
    for fragment in fragments:
        key = (str(fragment["jpx_date"]), str(fragment["name"]))
        item = merged.setdefault(
            key,
            {"code": None, "name": key[1], "market": None, "jpx_date": key[0], "is_pro": False},
        )
        if fragment["code"]:
            item["code"] = fragment["code"]
        if fragment["market"]:
            item["market"] = fragment["market"]
        item["is_pro"] = bool(item["is_pro"] or fragment["is_pro"])

    return [
        {"code": item["code"], "name": item["name"], "market": item["market"], "jpx_date": item["jpx_date"]}
        for item in merged.values()
        if item["code"] and item["market"] and not item["is_pro"]
    ]


def fetch_ipo_universe() -> pd.DataFrame:
    """上場日が必要な近年分は JPX、欠損年の母集団は ipokiso で補完する。"""
    rows: list[dict[str, object]] = []
    for year, url in JPX_URLS.items():
        page_rows = parse_jpx_page(fetch_html(url))
        rows.extend(page_rows)
        print(f"JPX {year}: {len(page_rows)}銘柄", flush=True)

    for year, url in zip(range(2019, 2022), IPOKISO_URLS, strict=True):
        codes = list(dict.fromkeys(IPOKISO_CODE_RE.findall(fetch_html(url))))
        rows.extend({"code": code, "name": pd.NA, "market": pd.NA, "jpx_date": pd.NA} for code in codes)
        print(f"ipokiso {year}: {len(codes)}銘柄", flush=True)

    universe = pd.DataFrame(rows, columns=["code", "name", "market", "jpx_date"])
    universe["code"] = universe["code"].astype(str).str.upper()
    universe["jpx_date"] = pd.to_datetime(universe["jpx_date"], errors="coerce")
    universe = universe.drop_duplicates("code", keep="first").sort_values("code").reset_index(drop=True)
    return universe


def normalize_history(history: pd.DataFrame) -> pd.DataFrame:
    """yfinance の列形式差を吸収し、検証コードへ一定の OHLCV だけを渡す。"""
    if isinstance(history.columns, pd.MultiIndex):
        history.columns = history.columns.get_level_values(0)
    daily = history[["Open", "High", "Low", "Close", "Volume"]].copy()
    dates = pd.DatetimeIndex(pd.to_datetime(daily.index))
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    daily.index = dates
    daily.index.name = "date"
    daily.columns = ["open", "high", "low", "close", "volume"]
    return daily.sort_index()


def fetch_daily(universe: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """取得不能銘柄を母集団から黙って消さず、生存バイアスの件数を観測可能にする。"""
    daily_parts: list[pd.DataFrame] = []
    list_rows: list[dict[str, object]] = []
    skipped = 0

    for number, row in enumerate(universe.itertuples(index=False), start=1):
        code = str(row.code)
        try:
            history = yf.Ticker(f"{code}.T").history(period="max", interval="1d", auto_adjust=True)
            if history.empty:
                raise ValueError("0行")
            daily = normalize_history(history)
            if daily.empty:
                raise ValueError("OHLCV 0行")
        except Exception as exc:
            skipped += 1
            print(f"警告: {code} をスキップ ({exc})", flush=True)
        else:
            yf_first = daily.index[0].normalize()
            jpx_date = row.jpx_date if not pd.isna(row.jpx_date) else pd.NaT
            mismatch = bool(pd.notna(jpx_date) and abs((yf_first - jpx_date).days) > 30)
            daily.insert(0, "code", code)
            daily_parts.append(daily.reset_index())
            list_rows.append(
                {
                    "code": code,
                    "name": row.name,
                    "market": row.market,
                    "jpx_date": jpx_date,
                    "yf_first": yf_first,
                    "n_bars": len(daily),
                    "flag_date_mismatch": mismatch,
                }
            )

        if number % 50 == 0:
            print(f"日足取得: {number}/{len(universe)}件", flush=True)
        time.sleep(0.1)

    ipo_list = pd.DataFrame(
        list_rows,
        columns=["code", "name", "market", "jpx_date", "yf_first", "n_bars", "flag_date_mismatch"],
    )
    daily_all = pd.concat(daily_parts, ignore_index=True) if daily_parts else pd.DataFrame(
        columns=["code", "date", "open", "high", "low", "close", "volume"]
    )
    return ipo_list, daily_all, skipped


def main() -> None:
    """相対パスをリポジトリ直下に固定し、検証入力の置き場所を一意にする。"""
    universe = fetch_ipo_universe()
    print(f"IPOリスト合計: {len(universe)}銘柄", flush=True)
    ipo_list, daily, skipped = fetch_daily(universe)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ipo_list.to_csv(IPO_LIST_PATH, index=False, date_format="%Y-%m-%d")
    daily.to_parquet(IPO_DAILY_PATH, index=False)
    print(f"保存: {IPO_LIST_PATH} ({len(ipo_list)}銘柄)", flush=True)
    print(f"保存: {IPO_DAILY_PATH} ({len(daily)}行)", flush=True)
    print(f"スキップ件数: {skipped}", flush=True)


if __name__ == "__main__":
    main()
