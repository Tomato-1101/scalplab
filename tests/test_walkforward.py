"""ウォークフォワード検証のユニット/結合テスト。

- aggregate: 手計算した DataFrame で勝率・期待値・中央値・打切り除外・銘柄別集計を検証。
- run_walkforward: 合成ティックで CSV/md 生成、冪等性、best-params 欠損時の graceful skip。
- record フック: walkforward をモックし、例外を握る／JP・アドホックで呼ばないことを検証。
"""
import numpy as np
import pandas as pd
import pytest
import yaml

from scalplab.backtest import walkforward as wf

from fixtures_synth import BASE_DATE, MiniLoader, long_signal_scenario, write_parquet


def _entries_fixture() -> pd.DataFrame:
    """手計算しやすい最小 entries。code A に 4 件（有効 3・打切り 1）、code B に 1 件。

    有効な A の +60s net = [+2, -1, +3]（打切り行の +100 は除外される）。
      勝率(net>0) = 2/3、平均 = 4/3、中央値 = median([-1,2,3]) = 2。
    B は net = [+5] → 勝率 1.0、平均 5、中央値 5。
    全体（有効 4 件）net = [+2, -1, +3, +5] → 勝率 3/4、平均 9/4、中央値 median([-1,2,3,5]) = 2.5。
    """
    rows = [
        {"code": "A", "eval_truncated": False, "ret_60s_net": 2.0, "ret_60s_gross": 3.0, "mfe_bp": 6.0, "mae_bp": -1.0},
        {"code": "A", "eval_truncated": False, "ret_60s_net": -1.0, "ret_60s_gross": 0.0, "mfe_bp": 1.0, "mae_bp": -4.0},
        {"code": "A", "eval_truncated": False, "ret_60s_net": 3.0, "ret_60s_gross": 4.0, "mfe_bp": 8.0, "mae_bp": -0.5},
        {"code": "A", "eval_truncated": True, "ret_60s_net": 100.0, "ret_60s_gross": 101.0, "mfe_bp": 200.0, "mae_bp": 0.0},
        {"code": "B", "eval_truncated": False, "ret_60s_net": 5.0, "ret_60s_gross": 6.0, "mfe_bp": 9.0, "mae_bp": -2.0},
    ]
    return pd.DataFrame(rows)


def test_aggregate_hand_calculation():
    entries = _entries_fixture()
    agg = wf.aggregate(entries, horizon=60, codes=["A", "B"])

    a = agg["by_code"]["A"]
    assert a["n"] == 3  # 打切り 1 件が除外される
    assert a["win60"] == pytest.approx(2 / 3)
    assert a["exp60_net_bp"] == pytest.approx(4 / 3)
    assert a["med60_net_bp"] == pytest.approx(2.0)
    assert a["gross_mean_bp"] == pytest.approx((3.0 + 0.0 + 4.0) / 3)
    assert a["mfe_med_bp"] == pytest.approx(6.0)   # median([6,1,8]) = 6
    assert a["mae_med_bp"] == pytest.approx(-1.0)  # median([-1,-4,-0.5]) = -1

    b = agg["by_code"]["B"]
    assert b["n"] == 1
    assert b["win60"] == pytest.approx(1.0)
    assert b["exp60_net_bp"] == pytest.approx(5.0)

    ov = agg["overall"]
    assert ov["n"] == 4
    assert ov["win60"] == pytest.approx(3 / 4)
    assert ov["exp60_net_bp"] == pytest.approx(9 / 4)
    assert ov["med60_net_bp"] == pytest.approx(2.5)


def test_aggregate_includes_zero_signal_code():
    """録画はあったがシグナル 0 件の銘柄も n=0 行として必ず含める。"""
    entries = _entries_fixture()
    agg = wf.aggregate(entries, horizon=60, codes=["A", "B", "C"])
    assert agg["by_code"]["C"]["n"] == 0
    assert np.isnan(agg["by_code"]["C"]["win60"])


def test_params_hash_stable_and_order_independent():
    h1 = wf.params_hash({"rci_period": 9, "eff_min": 4.0})
    h2 = wf.params_hash({"eff_min": 4.0, "rci_period": 9})  # キー順が違っても同じ
    assert h1 == h2 and len(h1) == 8
    assert wf.params_hash({"rci_period": 10, "eff_min": 4.0}) != h1  # 値が違えば別


def _write_best_params(path, codes, params=None):
    params = params or {"rci_period": 9, "slope_window_sec": 20, "slope_threshold": 15.0,
                        "eff_min": 3.0, "prev_diff_min": 0.0}
    path.write_text(yaml.safe_dump({"params": params, "codes": codes, "horizon": 60}),
                    encoding="utf-8")


def _setup_synth(tmp_path, code="SYNTH.LONG"):
    """合成ロングシグナルの ticks を契約パスに置き、MiniLoader と best-params を用意する。"""
    ticks, _s, _e = long_signal_scenario()
    write_parquet(ticks, tmp_path, "ticks", BASE_DATE, code)
    loader = MiniLoader(tmp_path)
    cfg = tmp_path / "best-params.yaml"
    _write_best_params(cfg, [code])
    reports = tmp_path / "reports"
    return loader, cfg, reports, code


def test_run_walkforward_writes_csv_and_md(tmp_path):
    loader, cfg, reports, code = _setup_synth(tmp_path)
    res = wf.run_walkforward(BASE_DATE, config_path=cfg, reports_dir=reports, loader=loader)

    assert res is not None
    csv = reports / "walkforward.csv"
    md = reports / "walkforward.md"
    assert csv.exists() and md.exists()

    df = pd.read_csv(csv)
    # 銘柄行 + ALL 行
    assert set(df["code"]) == {code, "ALL"}
    assert list(df.columns) == wf.WF_COLUMNS
    all_row = df[df["code"] == "ALL"].iloc[0]
    # 仕込んだロングは +60s ネットが正 → 全勝（勝率 1.0）
    assert all_row["n"] >= 1
    assert all_row["win60"] == pytest.approx(1.0)
    assert all_row["exp60_net_bp"] > 0
    assert "累計サマリー" in md.read_text(encoding="utf-8")


def test_run_walkforward_idempotent(tmp_path):
    loader, cfg, reports, code = _setup_synth(tmp_path)
    r1 = wf.run_walkforward(BASE_DATE, config_path=cfg, reports_dir=reports, loader=loader)
    assert r1 is not None
    n_rows_1 = len(pd.read_csv(reports / "walkforward.csv"))

    # 同一 (date, hash) の再実行はスキップ（None）で行数不変
    r2 = wf.run_walkforward(BASE_DATE, config_path=cfg, reports_dir=reports, loader=loader)
    assert r2 is None
    assert len(pd.read_csv(reports / "walkforward.csv")) == n_rows_1

    # --force は上書き（差し替え）で二重計上しない
    r3 = wf.run_walkforward(BASE_DATE, config_path=cfg, reports_dir=reports, loader=loader, force=True)
    assert r3 is not None
    assert len(pd.read_csv(reports / "walkforward.csv")) == n_rows_1


def test_run_walkforward_missing_config_returns_none(tmp_path):
    loader = MiniLoader(tmp_path)
    reports = tmp_path / "reports"
    res = wf.run_walkforward(BASE_DATE, config_path=tmp_path / "nope.yaml",
                             reports_dir=reports, loader=loader)
    assert res is None
    assert not reports.exists()  # 何も書かない


def test_run_walkforward_no_recorded_codes_returns_none(tmp_path):
    reports = tmp_path / "reports"
    cfg = tmp_path / "best-params.yaml"
    _write_best_params(cfg, ["SYNTH.LONG"])
    loader = MiniLoader(tmp_path)  # ticks を置いていない → 録画なし
    res = wf.run_walkforward(BASE_DATE, config_path=cfg, reports_dir=reports, loader=loader)
    assert res is None


# --- record.py フック ---------------------------------------------------------

def test_record_hook_swallows_exception(monkeypatch):
    """walkforward が例外を投げてもフックは外へ伝播させない（録画終了処理を壊さない）。"""
    from scalplab import record
    import scalplab.backtest.walkforward as wfmod

    def boom(date):
        raise RuntimeError("intentional")

    monkeypatch.setattr(wfmod, "run_walkforward", boom)
    # 例外が漏れないこと（漏れれば pytest が失敗する）
    record._walkforward_after_finalize("2026-07-15", "US", None)


def test_record_hook_skips_jp_and_adhoc(monkeypatch):
    """JP モード・アドホック（duration 指定）では walkforward を呼ばない。"""
    from scalplab import record
    import scalplab.backtest.walkforward as wfmod

    calls = []
    monkeypatch.setattr(wfmod, "run_walkforward", lambda date: calls.append(date))

    record._walkforward_after_finalize("2026-07-15", "JP", None)      # JP → 呼ばない
    record._walkforward_after_finalize("2026-07-15", "US", 60.0)      # アドホック → 呼ばない
    assert calls == []

    record._walkforward_after_finalize("2026-07-15", "US", None)      # US セッション → 呼ぶ
    assert calls == ["2026-07-15"]


# --- 日本株（bars1m バー近似版）ウォークフォワード -----------------------------

def _bars1m_from_closes(closes, start="2099-01-05 09:35:00"):
    """close 列から bars1m 契約スキーマの合成バーを作る（JP 前場内・1分刻み）。"""
    t0 = pd.Timestamp(start)
    n = len(closes)
    o, h, l = [], [], []
    for i, cl in enumerate(closes):
        op = closes[i - 1] if i > 0 else cl - 0.02
        o.append(op)
        h.append(max(op, cl) + 0.03)
        l.append(min(op, cl) - 0.03)
    c = list(closes)
    return pd.DataFrame({
        "time": [t0 + pd.Timedelta(minutes=i) for i in range(n)],
        "open": np.array(o, float), "high": np.array(h, float),
        "low": np.array(l, float), "close": np.array(c, float),
        "volume": np.full(n, 100, np.int64), "turnover": np.array(c, float) * 100.0,
        "source": np.array(["synth"] * n, dtype=object),
    })


# 上昇→もみ合い→急伸でロングが1本出る close 列（test_strategy_bar 準拠）。
_JP_LONG_CLOSES = [98.0, 98.4, 98.8, 99.2, 99.6, 100.0,
                   100.02, 99.98, 100.03, 99.99, 100.04, 100.00, 100.30,
                   100.42, 100.55, 100.60, 100.7]
# シグナルが出ない平坦な close 列（n=0 行になる銘柄）。
_JP_FLAT_CLOSES = [100.0 + 0.0001 * (k % 2) for k in range(17)]


def _write_best_params_jp(path, codes="auto", params=None):
    """テスト用の best-params-jp.yaml を書く（角度ゲートで簡単に発火する軽いパラメータ）。"""
    params = params or {
        "rci_period": 5, "atr_period": 3, "slope_threshold": 15.0,
        "eff_min": 1.0, "cooldown_bars": 10, "prev_diff_min": 0.0,
        "rci_sign_gate": 0, "spike_depth_min": 0.0, "spike_turn_min": 0.0,
    }
    path.write_text(yaml.safe_dump({
        "params": params, "codes": codes, "session": "jp",
        "cost_bp": 3.0, "eval_bars": [1, 2],
    }), encoding="utf-8")


def _setup_synth_jp(tmp_path):
    """JP 合成 bars1m（発火する JP.AAA と発火しない JP.BBB）と best-params-jp を用意する。"""
    write_parquet(_bars1m_from_closes(_JP_LONG_CLOSES), tmp_path, "bars1m", BASE_DATE, "JP.AAA")
    write_parquet(_bars1m_from_closes(_JP_FLAT_CLOSES), tmp_path, "bars1m", BASE_DATE, "JP.BBB")
    loader = MiniLoader(tmp_path)
    cfg = tmp_path / "best-params-jp.yaml"
    _write_best_params_jp(cfg)
    reports = tmp_path / "reports"
    return loader, cfg, reports


def test_run_walkforward_jp_writes_csv_and_bycode(tmp_path):
    loader, cfg, reports = _setup_synth_jp(tmp_path)
    res = wf.run_walkforward_jp(BASE_DATE, config_path=cfg, reports_dir=reports, loader=loader)

    assert res is not None
    csv = reports / "walkforward-jp.csv"
    md = reports / "walkforward-jp.md"
    assert csv.exists() and md.exists()

    df = pd.read_csv(csv)
    # 銘柄別行（録画 2 銘柄）+ ALL 行 + META 行（レジーム選択の当日成績）が揃う
    assert set(df["code"]) == {"JP.AAA", "JP.BBB", "ALL", "META"}
    assert list(df.columns) == wf.wf_jp_columns((1, 2))
    # 発火銘柄はシグナルあり、平坦銘柄は n=0 行として含まれる
    assert int(df[df["code"] == "JP.AAA"].iloc[0]["n"]) >= 1
    assert int(df[df["code"] == "JP.BBB"].iloc[0]["n"]) == 0
    all_row = df[df["code"] == "ALL"].iloc[0]
    assert all_row["n"] == df[df["code"] == "JP.AAA"].iloc[0]["n"]  # BBB は 0 なので ALL==AAA
    # 単日データ（直前3営業日が無い）では META は◎銘柄選抜不能 → n=0 行
    assert int(df[df["code"] == "META"].iloc[0]["n"]) == 0
    assert "累計サマリー" in md.read_text(encoding="utf-8")


def test_run_walkforward_jp_idempotent(tmp_path):
    loader, cfg, reports = _setup_synth_jp(tmp_path)
    r1 = wf.run_walkforward_jp(BASE_DATE, config_path=cfg, reports_dir=reports, loader=loader)
    assert r1 is not None
    n_rows_1 = len(pd.read_csv(reports / "walkforward-jp.csv"))

    # 同一 (date, hash) の再実行はスキップ（None）で行数不変
    r2 = wf.run_walkforward_jp(BASE_DATE, config_path=cfg, reports_dir=reports, loader=loader)
    assert r2 is None
    assert len(pd.read_csv(reports / "walkforward-jp.csv")) == n_rows_1

    # --force は上書き（差し替え）で二重計上しない
    r3 = wf.run_walkforward_jp(BASE_DATE, config_path=cfg, reports_dir=reports, loader=loader, force=True)
    assert r3 is not None
    assert len(pd.read_csv(reports / "walkforward-jp.csv")) == n_rows_1


def test_run_walkforward_jp_missing_config_returns_none(tmp_path):
    loader = MiniLoader(tmp_path)
    reports = tmp_path / "reports"
    res = wf.run_walkforward_jp(BASE_DATE, config_path=tmp_path / "nope.yaml",
                                reports_dir=reports, loader=loader)
    assert res is None
    assert not reports.exists()


def test_run_walkforward_jp_no_recorded_codes_returns_none(tmp_path):
    reports = tmp_path / "reports"
    cfg = tmp_path / "best-params-jp.yaml"
    _write_best_params_jp(cfg)
    loader = MiniLoader(tmp_path)  # bars1m を置いていない → 録画なし
    res = wf.run_walkforward_jp(BASE_DATE, config_path=cfg, reports_dir=reports, loader=loader)
    assert res is None


def test_record_hook_jp_swallows_exception(monkeypatch):
    """JP walkforward が例外を投げてもフックは外へ伝播させない（録画終了処理を壊さない）。"""
    from scalplab import record
    import scalplab.backtest.walkforward as wfmod
    import scalplab.backtest.autotune as atmod

    def boom(date):
        raise RuntimeError("intentional")

    monkeypatch.setattr(wfmod, "run_walkforward_jp", boom)
    monkeypatch.setattr(atmod, "run_autotune", lambda date: None)  # 実チューナーは走らせない
    record._walkforward_jp_after_finalize("2026-07-15", "JP", None)  # 例外が漏れないこと


def test_record_hook_jp_autotune_swallows_exception(monkeypatch):
    """autotune が例外を投げてもフックは外へ伝播させない（walkforward 成功は保つ）。"""
    from scalplab import record
    import scalplab.backtest.walkforward as wfmod
    import scalplab.backtest.autotune as atmod

    monkeypatch.setattr(wfmod, "run_walkforward_jp", lambda date: None)

    def boom(date):
        raise RuntimeError("intentional")

    monkeypatch.setattr(atmod, "run_autotune", boom)
    record._walkforward_jp_after_finalize("2026-07-15", "JP", None)  # 例外が漏れないこと


def test_record_hook_jp_only_fires_for_jp_session(monkeypatch):
    """JP セッションのみ walkforward+autotune を呼び、US・アドホックでは呼ばない。"""
    from scalplab import record
    import scalplab.backtest.walkforward as wfmod
    import scalplab.backtest.autotune as atmod

    wf_calls = []
    at_calls = []
    monkeypatch.setattr(wfmod, "run_walkforward_jp", lambda date: wf_calls.append(date))
    monkeypatch.setattr(atmod, "run_autotune", lambda date: at_calls.append(date))

    record._walkforward_jp_after_finalize("2026-07-15", "US", None)     # US → 呼ばない
    record._walkforward_jp_after_finalize("2026-07-15", "JP", 60.0)     # アドホック → 呼ばない
    assert wf_calls == [] and at_calls == []

    record._walkforward_jp_after_finalize("2026-07-15", "JP", None)     # JP セッション → 両方呼ぶ
    assert wf_calls == ["2026-07-15"] and at_calls == ["2026-07-15"]
