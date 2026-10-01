"""レジーム判定（順張りの日 / 逆張りの日）のユニット/結合テスト。

ネットワーク非依存。合成 bars1m を tmp に書き、MiniLoader を loader= で注入して検証する
（本番は store_loader モジュール／テストは MiniLoader＝ともに regime が duck-typing で扱える）。
"""
import numpy as np
import pandas as pd
import pytest

from scalplab.backtest import regime

from fixtures_synth import MiniLoader, write_parquet

DATES = ["2099-01-05", "2099-01-06", "2099-01-07", "2099-01-08"]


def _bars(date, hl_bp, n=40, price=100.0, start="09:35"):
    """レンジ (high-low) を hl_bp bp に固定した JP 前場の合成 bars1m を返す。

    aptitude の range_med_bp を狙い撃ちできるよう、各バーの high-low を price×hl_bp/1e4 に揃える。
    """
    t0 = pd.Timestamp(f"{date} {start}:00")
    hl = price * hl_bp / 1e4
    close = np.full(n, price, dtype=float)
    return pd.DataFrame({
        "time": [t0 + pd.Timedelta(minutes=i) for i in range(n)],
        "open": close.copy(),
        "high": close + hl / 2.0,
        "low": close - hl / 2.0,
        "close": close.copy(),
        "volume": np.full(n, 100, np.int64),
        "turnover": close * 100.0,
        "source": np.array(["synth"] * n, dtype=object),
    })


@pytest.fixture
def env(tmp_path):
    """4 立会日ぶん JP.HI（40bp=◎）・JP.LO（5bp=×）と US.X を書いた MiniLoader を返す。"""
    for d in DATES:
        write_parquet(_bars(d, hl_bp=40.0), tmp_path, "bars1m", d, "JP.HI")
        write_parquet(_bars(d, hl_bp=5.0), tmp_path, "bars1m", d, "JP.LO")
        write_parquet(_bars(d, hl_bp=40.0), tmp_path, "bars1m", d, "US.X")
    return MiniLoader(tmp_path)


def test_prior_dates_none_uses_latest_three():
    assert regime.prior_dates(DATES, None) == DATES[-3:]


def test_prior_dates_specific_uses_strictly_before():
    # 2099-01-08 の直前3営業日は 05/06/07（08 自身は含まない）
    assert regime.prior_dates(DATES, "2099-01-08") == ["2099-01-05", "2099-01-06", "2099-01-07"]
    # 直前が3日に満たなければあるだけ返す
    assert regime.prior_dates(DATES, "2099-01-06") == ["2099-01-05"]


def test_available_dates_scans_loader(env):
    assert regime.available_dates(env) == DATES


def test_select_omega_only_high_range_jp(env):
    """◎は JP かつレンジ中央値 >= 25bp のみ。低ボラ JP.LO と US.X は除外。"""
    omega = regime.select_omega_symbols(DATES[-3:], loader=env)
    assert omega == ["JP.HI"]


def test_judge_regime_shape_and_dates(env):
    r = regime.judge_regime(loader=env)
    assert set(r) == {"pick", "trend_bp", "revert_bp", "dates", "n_symbols"}
    assert r["pick"] in ("trend", "revert")
    assert r["dates"] == DATES[-3:]          # date=None は最新3日
    assert r["n_symbols"] == 1               # ◎は JP.HI のみ


def test_judge_regime_specific_date_prior_window(env):
    r = regime.judge_regime("2099-01-08", loader=env)
    assert r["dates"] == ["2099-01-05", "2099-01-06", "2099-01-07"]


def test_decide_picks_higher_net(env, monkeypatch):
    """_decide は trend(=+2本net合計) と revert(=+1本net合計) の大きい方を選ぶ（同点は trend）。"""
    def _mk(net1, net2, n=1):
        return pd.DataFrame({
            "direction": ["long"] * n, "eval_truncated": [False] * n,
            "ret_1bar_net": [net1] * n, "ret_2bar_net": [net2] * n,
        })

    # 呼び出し順は trend(champion) → revert。revert(+1本)=+30 > trend(+2本)=+10 → revert。
    seq = iter([_mk(999.0, 10.0), _mk(30.0, 999.0)])
    monkeypatch.setattr(regime, "_system_entries", lambda *a, **k: next(seq))
    d = regime._decide(["JP.HI"], DATES[-3:], env)
    assert d["pick"] == "revert"
    assert d["trend_bp"] == pytest.approx(10.0)   # trend は +2本 net 合計
    assert d["revert_bp"] == pytest.approx(30.0)  # revert は +1本 net 合計

    # trend(+2本)=+50 > revert(+1本)=+5 → trend
    seq2 = iter([_mk(999.0, 50.0), _mk(5.0, 999.0)])
    monkeypatch.setattr(regime, "_system_entries", lambda *a, **k: next(seq2))
    d2 = regime._decide(["JP.HI"], DATES[-3:], env)
    assert d2["pick"] == "trend"


def test_judge_regime_cache_reuse_and_force(env, monkeypatch):
    """同一日付集合ならキャッシュ再利用（_decide 非呼出）、force=True で再計算する。"""
    calls = []
    orig = regime._decide

    def counting(omega, dates, loader):
        calls.append(dates)
        return orig(omega, dates, loader)

    monkeypatch.setattr(regime, "_decide", counting)

    r1 = regime.judge_regime(loader=env)
    assert len(calls) == 1
    r2 = regime.judge_regime(loader=env)     # キャッシュヒット＝_decide 呼ばれない
    assert len(calls) == 1
    assert r2 == r1
    regime.judge_regime(loader=env, force=True)  # force で再計算
    assert len(calls) == 2

    # キャッシュファイルが data/.cache に出来ている
    cache = regime._cache_path(env, DATES[-1])
    assert cache is not None and cache.exists()


def test_meta_entries_empty_when_no_prior(env):
    """直前3日が取れない最古日（05）は◎銘柄選抜不能 → 空 entries と pick=trend。"""
    entries, pick = regime.meta_entries_for_date("2099-01-05", loader=env)
    assert len(entries) == 0
    assert pick == "trend"


def test_meta_entries_for_date_returns_dataframe(env):
    """直前3日がある日は entries（空でも可）と pick を返す。列は bar_entry_columns 互換。"""
    entries, pick = regime.meta_entries_for_date("2099-01-08", loader=env)
    assert isinstance(entries, pd.DataFrame)
    assert pick in ("trend", "revert")
    # 空でも +1/+2 バー net 列を持つ（walkforward の集計が回る）
    for col in ("ret_1bar_net", "ret_2bar_net"):
        assert col in entries.columns
