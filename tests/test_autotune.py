"""夜間自動チューナー（autotune）のユニット/結合テスト。

- build_grid: 組数が 200〜500 に収まり（fade 軸追加で 312）、チャンピオン（best-params-jp.yaml）が含まれる。
- find_challengers: 「最新から 3 セッション連続でチャンピオン超え」の境界判定。
- run_autotune: 合成 bars1m で CSV/md 生成・冪等・--force 差し替え。
"""
import numpy as np
import pandas as pd

from scalplab.backtest import autotune as at
from scalplab.backtest.walkforward import _config_path_jp, _load_champion_jp
from scalplab.strategy.rci_impulse_bar import merge_bar_params

from fixtures_synth import MiniLoader, write_parquet


def test_build_grid_size_and_contains_champion():
    grid = at.build_grid()
    # 主256 + 符号20 + σフェード18 + ボリンジャーフェード18 = 312（上限 500 内。fade 軸追加で 276→312）。
    assert 200 <= len(grid) <= 500, f"グリッド組数が範囲外: {len(grid)}"
    champ = _load_champion_jp(_config_path_jp())
    assert champ is not None
    champ_hash = at._combo_hash(merge_bar_params(champ["params"]))
    assert champ_hash in grid, "チャンピオンがグリッドに含まれていない（同日 test を必ず記録するため必須）"


def _wf_row(date, phash, test_tot):
    row = {"date": date, "params_hash": phash, "test_tot_bp": float(test_tot),
           "test_n": 10, "test_win": 0.5, "train_tot_bp": 0.0}
    for k in at._PARAM_COLS:
        row[k] = 0
    return row


def test_find_challengers_streak_boundary():
    """最新セッションを起点に 3 連続でチャンピオンを上回った組だけ挙げる。"""
    d1, d2, d3 = "2099-01-05", "2099-01-06", "2099-01-07"
    rows = []
    for d in (d1, d2, d3):
        rows.append(_wf_row(d, "CH", 100.0))       # champion: 各日 100
    for d in (d1, d2, d3):
        rows.append(_wf_row(d, "A", 110.0))        # A: 3 日とも超え → streak 3（候補）
    rows += [_wf_row(d1, "B", 90.0), _wf_row(d2, "B", 110.0), _wf_row(d3, "B", 110.0)]  # 直近2のみ
    rows += [_wf_row(d1, "C", 110.0), _wf_row(d2, "C", 110.0), _wf_row(d3, "C", 90.0)]  # 最新で負け
    df = pd.DataFrame(rows, columns=at.AT_COLUMNS)

    ch = at.find_challengers(df, "CH")
    hashes = {c["params_hash"] for c in ch}
    assert hashes == {"A"}, f"3連続超えは A のみのはず: {hashes}"
    assert ch[0]["streak"] == 3

    # 3 セッション未満なら誰も候補にならない
    df2 = df[df["date"].isin([d2, d3])]
    assert at.find_challengers(df2, "CH") == []


def _synth_bars_jp(seed, n=44):
    """JP 前場（09:00〜）の合成 1 分足。振動でトゲ/角度シグナルが多少出るようにする。"""
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp("2099-01-05 09:00:00")
    close = 1000.0
    o, h, l, c, tm = [], [], [], [], []
    for i in range(n):
        step = np.sin(i / 3.0) * 1.2 + rng.normal(0, 0.4)
        op = close
        close = round(close + step, 1)
        o.append(op); c.append(close)
        h.append(max(op, close) + 0.3); l.append(min(op, close) - 0.3)
        tm.append(t0 + pd.Timedelta(minutes=i))
    n2 = len(c)
    return pd.DataFrame({
        "time": tm, "open": np.array(o, float), "high": np.array(h, float),
        "low": np.array(l, float), "close": np.array(c, float),
        "volume": np.full(n2, 100, np.int64), "turnover": np.array(c, float) * 100.0,
        "source": np.array(["synth"] * n2, dtype=object),
    })


def _setup_autotune(tmp_path):
    dates = ["2099-01-05", "2099-01-06", "2099-01-07"]
    for i, d in enumerate(dates):
        for code in ("JP.AAA", "JP.BBB"):
            df = _synth_bars_jp(seed=i * 10 + (0 if code == "JP.AAA" else 5))
            # 日付ごとに time の日付部分を合わせる（filter_jp_session は時刻のみ見るが揃えておく）
            df = df.copy()
            df["time"] = df["time"] + (pd.Timestamp(d) - pd.Timestamp("2099-01-05"))
            write_parquet(df, tmp_path, "bars1m", d, code)
    loader = MiniLoader(tmp_path)
    reports = tmp_path / "reports"
    return loader, reports, dates


def test_run_autotune_writes_and_bycombo(tmp_path):
    loader, reports, dates = _setup_autotune(tmp_path)
    res = at.run_autotune("2099-01-07", reports_dir=reports, loader=loader, dates=dates)
    assert res is not None
    csv = reports / "autotune-jp.csv"
    md = reports / "autotune-jp.md"
    assert csv.exists() and md.exists()
    df = pd.read_csv(csv)
    assert list(df.columns) == at.AT_COLUMNS
    # 検証日 = 最新日、全組ぶんの行がある
    assert set(df["date"].astype(str)) == {"2099-01-07"}
    assert len(df) == len(at.build_grid())
    # チャンピオンの行が存在する
    champ = _load_champion_jp(_config_path_jp())
    champ_hash = at._combo_hash(merge_bar_params(champ["params"]))
    assert (df["params_hash"].astype(str) == champ_hash).any()
    assert "最新セッション" in md.read_text(encoding="utf-8")


def test_run_autotune_idempotent(tmp_path):
    loader, reports, dates = _setup_autotune(tmp_path)
    r1 = at.run_autotune("2099-01-07", reports_dir=reports, loader=loader, dates=dates)
    assert r1 is not None
    n1 = len(pd.read_csv(reports / "autotune-jp.csv"))

    # 同一検証日の再実行はスキップ（冪等）で行数不変
    r2 = at.run_autotune("2099-01-07", reports_dir=reports, loader=loader, dates=dates)
    assert r2 is None
    assert len(pd.read_csv(reports / "autotune-jp.csv")) == n1

    # --force は該当日を作り直す（二重計上しない）
    r3 = at.run_autotune("2099-01-07", reports_dir=reports, loader=loader, dates=dates, force=True)
    assert r3 is not None
    assert len(pd.read_csv(reports / "autotune-jp.csv")) == n1


def test_run_autotune_no_codes_returns_none(tmp_path):
    loader = MiniLoader(tmp_path)
    reports = tmp_path / "reports"
    res = at.run_autotune("2099-01-07", reports_dir=reports, loader=loader, dates=["2099-01-07"])
    assert res is None
    assert not reports.exists()
