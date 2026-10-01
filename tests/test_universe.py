"""universe 選定ロジックのテスト（yfinance はモック）。"""
from __future__ import annotations

import sys
import types

import pandas as pd

from scalplab.datafeed import universe


def test_moomoo_to_yf():
    assert universe.moomoo_to_yf("JP.7203") == "7203.T"
    assert universe.moomoo_to_yf("US.AAPL") == "AAPL"


def test_select_universe_fixed_priority_and_clip():
    """fixed を最優先し、auto は fixed 重複を除いて上位 auto_top_n、合計 max_symbols で打ち切る。"""
    fixed = ["JP.7203", "JP.6758"]
    auto_ranked = [
        ("JP.9984", 1_000_000_000.0),
        ("JP.7203", 500_000_000.0),  # fixed と重複 → auto から除外される
        ("JP.8306", 200_000_000.0),
        ("JP.6501", 100_000_000.0),
    ]
    codes, rationale = universe.select_universe(fixed, auto_ranked, auto_top_n=2, max_symbols=3)
    # fixed 2 + auto 上位（7203 を除く 9984, 8306 の2件）= [7203,6758,9984,8306] を 3 で打ち切り
    assert codes == ["JP.7203", "JP.6758", "JP.9984"]
    assert rationale["fixed"] == ["JP.7203", "JP.6758"]
    # rationale.auto は最終 codes に残ったものだけ
    assert [a["code"] for a in rationale["auto"]] == ["JP.9984"]


def test_select_universe_dedup_fixed():
    fixed = ["JP.7203", "JP.7203", "JP.6758"]  # 重複入力
    codes, _ = universe.select_universe(fixed, [], auto_top_n=5, max_symbols=10)
    assert codes == ["JP.7203", "JP.6758"]


def test_select_universe_fixed_exceeds_max():
    fixed = ["JP.1", "JP.2", "JP.3"]
    codes, rationale = universe.select_universe(
        fixed, [("JP.9", 1.0)], auto_top_n=5, max_symbols=2
    )
    assert codes == ["JP.1", "JP.2"]  # fixed だけで上限に達し auto は入らない
    assert rationale["auto"] == []


def test_rank_by_turnover_single_ticker(monkeypatch):
    """yfinance をモックし、直近営業日 Close×Volume で売買代金を計算するか。"""
    fake = types.ModuleType("yfinance")

    def download(tickers, **kwargs):
        # 単一ティッカー時は group_by なしのフラット DataFrame が返る
        return pd.DataFrame(
            {"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [10.0, 20.0], "Volume": [100, 200]}
        )

    fake.download = download
    monkeypatch.setitem(sys.modules, "yfinance", fake)

    res = universe.rank_by_turnover(["JP.7203"])
    assert res == [("JP.7203", 20.0 * 200)]  # 最終行 Close×Volume = 4000


def test_rank_by_turnover_no_yfinance(monkeypatch):
    """yfinance 未導入時は空リストを返す（呼び手がフォールバックを判断）。"""
    # import を失敗させる
    monkeypatch.setitem(sys.modules, "yfinance", None)
    res = universe.rank_by_turnover(["JP.7203"])
    assert res == []


# --- JP auto ランキング源（松井Tick回数 ⇄ 売買代金プロキシ） ---

def test_jp_auto_ranked_uses_matsui_tick(monkeypatch):
    """ranking_source=matsui_tick なら松井 Tick回数を (code, tick数) の降順で返す。"""
    from scalplab.datafeed import jp_tick_ranking

    rows = [
        {"rank": 1, "code": "JP.285A", "name": "x", "tick_count": 205097},
        {"rank": 2, "code": "JP.9984", "name": "y", "tick_count": 93544},
    ]
    monkeypatch.setattr(jp_tick_ranking, "get_ranking", lambda date, **k: (rows, "fetched"))
    ranked = universe.jp_auto_ranked("2099-01-06", {"ranking_source": "matsui_tick"}, ["JP.7203"])
    assert ranked == [("JP.285A", 205097.0), ("JP.9984", 93544.0)]


def test_jp_auto_ranked_falls_back_to_turnover_proxy(monkeypatch):
    """松井が使えない（unavailable）ときは売買代金プロキシへフォールバックする。"""
    from scalplab.datafeed import jp_tick_ranking

    monkeypatch.setattr(jp_tick_ranking, "get_ranking", lambda date, **k: ([], "unavailable"))
    monkeypatch.setattr(universe, "rank_by_turnover", lambda codes: [("JP.7203", 999.0)])
    ranked = universe.jp_auto_ranked("2099-01-06", {"ranking_source": "matsui_tick"}, ["JP.7203"])
    assert ranked == [("JP.7203", 999.0)]


def test_jp_auto_ranked_proxy_when_source_not_matsui(monkeypatch):
    """ranking_source が matsui_tick 以外なら松井を一切叩かず売買代金プロキシを使う。"""
    from scalplab.datafeed import jp_tick_ranking

    def _boom(*a, **k):
        raise AssertionError("matsui_tick 以外では松井を叩いてはいけない")

    monkeypatch.setattr(jp_tick_ranking, "get_ranking", _boom)
    monkeypatch.setattr(universe, "rank_by_turnover", lambda codes: [("JP.7203", 1.0)])
    ranked = universe.jp_auto_ranked("2099-01-06", {"ranking_source": None}, ["JP.7203"])
    assert ranked == [("JP.7203", 1.0)]


# --- ティック回数ランキング（改修4） ---

def _write_tick_parquet(ticks_root, session, code, n_rows):
    """指定セッション日・code に n_rows 行の合成ティック parquet を書く（行数だけが意味を持つ）。"""
    d = ticks_root / session
    d.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({
        "time": pd.date_range("2099-01-05 22:30:00", periods=n_rows, freq="s"),
        "price": [100.0] * n_rows,
    })
    df.to_parquet(d / f"{code}.parquet", index=False)


def test_latest_tick_session_picks_newest(tmp_path):
    """parquet を持つ日付ディレクトリのうち最新を選ぶ（空ディレクトリは無視）。"""
    root = tmp_path / "ticks"
    _write_tick_parquet(root, "2026-07-14", "US.NVDA", 10)
    _write_tick_parquet(root, "2026-07-15", "US.AAPL", 5)
    (root / "2026-07-16").mkdir()  # parquet 無し → 無視される
    assert universe.latest_tick_session(root) == "2026-07-15"


def test_latest_tick_session_empty_returns_none(tmp_path):
    """録画実績が1つも無ければ None（初回夜のフォールバック判定に使う）。"""
    assert universe.latest_tick_session(tmp_path / "ticks") is None


def test_tick_counts_reads_row_counts(tmp_path):
    """parquet の行数をそのままティック数として返す。"""
    root = tmp_path / "ticks"
    _write_tick_parquet(root, "2026-07-15", "US.NVDA", 6252)
    _write_tick_parquet(root, "2026-07-15", "US.AAPL", 1629)
    counts = universe.tick_counts("2026-07-15", root)
    assert counts == {"US.NVDA": 6252, "US.AAPL": 1629}


def test_select_universe_tick_ranked_order():
    """コアは fixed 順で固定、非コアはティック数降順（同数は fixed 順）で埋める。"""
    fixed = ["US.A", "US.B", "US.C", "US.D", "US.E"]
    counts = {"US.C": 100, "US.D": 900, "US.E": 0}  # 非コアのティック数（B は未録画=0）
    codes, rationale = universe.select_universe_tick_ranked(
        fixed, counts, core=2, max_symbols=5
    )
    # コア = 先頭2（A,B）。非コア C,D,E をティック数降順 → D(900) > C(100) > E(0)
    assert codes == ["US.A", "US.B", "US.D", "US.C", "US.E"]
    assert rationale["core"] == ["US.A", "US.B"]
    assert [r["code"] for r in rationale["ranked"]] == ["US.D", "US.C", "US.E"]
    assert rationale["ranked"][0] == {"code": "US.D", "ticks": 900}


def test_select_universe_tick_ranked_clip_and_tie():
    """max_symbols で打ち切り、同ティック数は fixed 順を保つ（安定ソート）。"""
    fixed = ["US.A", "US.B", "US.C", "US.D"]
    counts = {"US.B": 0, "US.C": 0, "US.D": 0}  # 非コア全て同数(0) → fixed 順のまま
    codes, _ = universe.select_universe_tick_ranked(fixed, counts, core=1, max_symbols=3)
    assert codes == ["US.A", "US.B", "US.C"]  # D は max で切られる


def test_build_us_tick_count_uses_ranking(tmp_path):
    """build_us が ranking=tick_count のとき前回セッションのティック数で並べ替える。"""
    ticks_root = tmp_path / "ticks"
    _write_tick_parquet(ticks_root, "2026-07-15", "US.D", 900)
    _write_tick_parquet(ticks_root, "2026-07-15", "US.C", 100)

    cfg = tmp_path / "universe_us.yaml"
    cfg.write_text(
        "fixed: [US.A, US.B, US.C, US.D, US.E]\n"
        "ranking: tick_count\n"
        "core: 2\n"
        "max_symbols: 5\n",
        encoding="utf-8",
    )
    monkey_dir = tmp_path / "out"
    # UNIVERSE_DIR を tmp に逃がして書き出しを隔離
    orig = universe.UNIVERSE_DIR
    universe.UNIVERSE_DIR = monkey_dir
    try:
        doc = universe.build_us("2099-01-06", config_path=cfg, ticks_dir=ticks_root)
    finally:
        universe.UNIVERSE_DIR = orig
    assert doc["rationale"]["source"] == "tick_count"
    assert doc["rationale"]["session"] == "2026-07-15"
    # コア(A,B) 固定 + 非コア D(900)>C(100)>E(0)
    assert doc["codes"] == ["US.A", "US.B", "US.D", "US.C", "US.E"]


def test_build_us_no_ticks_falls_back_to_fixed(tmp_path):
    """録画実績が無い（初回夜）は fixed 全体（先頭から max_symbols）にフォールバックする。"""
    cfg = tmp_path / "universe_us.yaml"
    cfg.write_text(
        "fixed: [US.A, US.B, US.C]\n"
        "ranking: tick_count\n"
        "core: 2\n"
        "max_symbols: 10\n",
        encoding="utf-8",
    )
    monkey_dir = tmp_path / "out"
    orig = universe.UNIVERSE_DIR
    universe.UNIVERSE_DIR = monkey_dir
    try:
        doc = universe.build_us("2099-01-06", config_path=cfg, ticks_dir=tmp_path / "no_ticks")
    finally:
        universe.UNIVERSE_DIR = orig
    assert doc["rationale"]["source"] == "fixed_only"
    assert doc["codes"] == ["US.A", "US.B", "US.C"]
