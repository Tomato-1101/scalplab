"""Web UI の API ハンドラを合成データで検証する（サーバ起動不要）。

server.handle_api / build_chart を直接呼び、データアクセス先（history.HIST_DIR /
store.loader.DATA_DIR）を一時ディレクトリへ差し替えて、契約準拠の合成 parquet を読ませる。
指標・エントリー計算は本番と同じ既存モジュール（rci_impulse_bar / engine）を通る。
"""
import json

import numpy as np
import pandas as pd
import pytest

from scalplab.datafeed import history
from scalplab.store import loader as store_loader
from scalplab.store.writer import enforce_bars1m
from scalplab.webui import server

from fixtures_synth import make_ticks, write_parquet

HIST_DATE = "2099-01-05"  # 実データと衝突しない架空営業日（09:35 ET 起点＝RTH 内）


def _fire_day(start=f"{HIST_DATE} 09:35:00"):
    """既定パラメータ（rci_period=9 / slope=15 / eff_min=2 / cooldown=3）でロングが発火する
    単一営業日の合成 1 分足を作る。表示・エントリーの疎通を end-to-end で見るための最小データ。

    start を早い時刻（例 09:05）にすると、RTH フィルタ（09:31〜 米国東部）が誤って掛かった場合に
    先頭バーが消えることで検出できる（日本株 bars1m は RTH 非適用を検証するため）。"""
    closes = [100.0 + 0.06 * k for k in range(15)]           # 上昇で SAR ロング確立 + ATR ウォームアップ
    base = closes[-1]
    closes += [base + 0.01 * ((-1) ** k) for k in range(9)]  # もみ合いで RCI を寝かす
    closes += [base + 0.05 * k for k in range(1, 5)]         # 急伸（ΔRCI が跳ねる・レンジ小）
    closes += [closes[-1] + 0.02 * k for k in range(1, 5)]   # 余韻
    n = len(closes)
    t0 = pd.Timestamp(start)
    o = [closes[i - 1] if i > 0 else closes[0] - 0.02 for i in range(n)]
    h = [max(o[i], closes[i]) + 0.02 for i in range(n)]
    lo = [min(o[i], closes[i]) - 0.02 for i in range(n)]
    return pd.DataFrame({
        "time": [t0 + pd.Timedelta(minutes=i) for i in range(n)],
        "open": np.array(o, float), "high": np.array(h, float),
        "low": np.array(lo, float), "close": np.array(closes, float),
        "volume": np.full(n, 100, np.int64), "turnover": np.array(closes, float) * 100.0,
        "source": "moomoo_hist",
    })


@pytest.fixture
def synth_env(tmp_path, monkeypatch):
    """history.HIST_DIR と loader.DATA_DIR を一時ディレクトリへ向ける。"""
    data_dir = tmp_path / "data"
    hist_dir = data_dir / "hist1m"
    hist_dir.mkdir(parents=True)
    monkeypatch.setattr(history, "HIST_DIR", hist_dir)
    monkeypatch.setattr(store_loader, "DATA_DIR", data_dir)
    # hist1m: 発火する合成日を US.TEST として置く
    enforce_bars1m(_fire_day()).to_parquet(hist_dir / "US.TEST.parquet", index=False)
    # bars1m: 日本株 1 分足。09:05 起点にして RTH 非適用（先頭バーが残る）を検証できるようにする。
    jp_dir = data_dir / "bars1m" / "2099-01-05"
    jp_dir.mkdir(parents=True)
    jp = _fire_day(start="2099-01-05 09:05:00").copy()
    jp["source"] = "yfinance"
    enforce_bars1m(jp).to_parquet(jp_dir / "JP.TEST.parquet", index=False)
    # ticks: 3 分ぶんの合成ティック（5秒足で RCI が出る長さ）
    t0 = pd.Timestamp("2099-01-05 10:00:00")
    times = [t0 + pd.Timedelta(seconds=s) for s in range(180)]
    prices = [100.0 + 0.02 * s + (0.05 if s % 7 == 0 else 0.0) for s in range(180)]
    write_parquet(make_ticks(times, prices), tmp_path, "ticks", "2099-01-05", "US.TEST")
    return tmp_path


def _assert_json_clean(obj):
    """NaN/Inf が混ざっていないこと（allow_nan=False で通る）を確認する。"""
    json.dumps(obj, ensure_ascii=False, allow_nan=False)


def test_sources_lists_synth(synth_env):
    st, body = server.handle_api("/api/sources", {})
    assert st == 200
    assert body["hist1m"] == ["US.TEST"]
    assert body["ticks"] == {"2099-01-05": ["US.TEST"]}
    # bars1m（日本株 1 分足）は JP.* のみ集計・保存日一覧を返す
    assert body["bars1m"] == ["JP.TEST"]
    assert body["bars1m_dates"] == ["2099-01-05"]
    d = body["defaults"]
    assert d["rci_short"] == 9 and d["rci_long"] == 14
    assert d["hist1m"]["cooldown"] == 3          # バー本数
    assert d["ticks"]["cooldown"] == 60          # 秒
    assert d["bars1m"]["code"] == "JP.TEST"      # DEFAULT_BARS1M_CODE 無ければ先頭銘柄
    assert d["bars1m"]["from"] == "2099-01-05" and d["bars1m"]["to"] == "2099-01-05"
    assert d["bars1m"]["prev_diff_min"] == 0.0   # 直前差ゲートは既定無効
    _assert_json_clean(body)


def test_sources_jp_order_follows_tick_ranking(tmp_path, monkeypatch):
    """JP サイドバー並び順が最新の Tick回数ランキング CSV 順になる（載らない銘柄は末尾）。"""
    from scalplab.datafeed import jp_tick_ranking

    data_dir = tmp_path / "data"
    hist_dir = data_dir / "hist1m"
    hist_dir.mkdir(parents=True)
    monkeypatch.setattr(history, "HIST_DIR", hist_dir)
    monkeypatch.setattr(store_loader, "DATA_DIR", data_dir)

    # bars1m に JP を3つ置く（stem が並び順の対象）
    jp_dir = data_dir / "bars1m" / "2099-01-05"
    jp_dir.mkdir(parents=True)
    for code in ("JP.1111", "JP.2222", "JP.3333"):
        enforce_bars1m(_fire_day(start="2099-01-05 09:05:00")).to_parquet(jp_dir / f"{code}.parquet", index=False)

    # ランキング CSV: 3333 → 1111 の順。2222 はランキング外（末尾に回る）。
    (data_dir / "universe").mkdir(parents=True)
    jp_tick_ranking.save_ranking(
        [{"rank": 1, "code": "JP.3333", "name": "c", "tick_count": 900},
         {"rank": 2, "code": "JP.1111", "name": "a", "tick_count": 500}],
        "2099-01-05", data_dir / "universe",
    )

    st, body = server.handle_api("/api/sources", {})
    assert st == 200
    assert body["bars1m"] == ["JP.3333", "JP.1111", "JP.2222"]


def test_chart_hist1m_structure_and_entries(synth_env):
    st, p = server.handle_api("/api/chart", {
        "source": "hist1m", "code": "US.TEST", "from": HIST_DATE, "to": HIST_DATE,
    })
    assert st == 200
    assert len(p["candles"]) > 0
    # RCI 短期・中期の両方が有限点として返る
    assert len(p["rci9"]) > 0 and len(p["rci14"]) > 0
    # SMA は指定期間ぶんのキーを持つ（既定は 5/25 固定）
    assert set(p["sma"].keys()) == {"5", "25"}
    assert len(p["sar"]) > 0
    # 既定パラメータでロングが 1 件以上出る
    assert len(p["entries"]) >= 1
    e = p["entries"][0]
    assert e["direction"] in ("long", "short")
    assert e["price"] is not None
    assert any(f["label"].startswith("+1本") for f in e["forward"])
    # 時刻はローソクの時刻集合に含まれる（マーカーが確実に載る）
    candle_times = {c["time"] for c in p["candles"]}
    assert e["time"] in candle_times
    # prev_diff（尖り）の実値が明細に載る（バー版では drci1 と一致）
    assert "prev_diff" in e and e["prev_diff"] == e["drci"]
    _assert_json_clean(p)


def test_rci_time_axis_matches_candles(synth_env):
    """軸ズレ対策: rci9/rci14/sar/sma がローソクと完全に同一の time 集合で返る（whitespace 込み）。

    ウォームアップ区間を間引かず value 無しの whitespace 点で埋めることで、メインと RCI ペインの
    time→論理インデックス対応が一致し、可視論理レンジ同期で同時刻が縦に揃う。"""
    st, p = server.handle_api("/api/chart", {
        "source": "hist1m", "code": "US.TEST", "from": HIST_DATE, "to": HIST_DATE,
    })
    assert st == 200
    ct = [c["time"] for c in p["candles"]]
    assert len(ct) > 0
    assert [x["time"] for x in p["rci9"]] == ct
    assert [x["time"] for x in p["rci14"]] == ct
    assert [x["time"] for x in p["sar"]] == ct
    for per in ("5", "25"):
        assert [x["time"] for x in p["sma"][per]] == ct
    # 先頭ウォームアップは value 無し（whitespace）、確定後は value 付き
    assert any("value" not in x for x in p["rci9"])
    assert any("value" in x for x in p["rci9"])


def test_chart_boll_series_present_and_time_aligned(synth_env):
    """ボリンジャーバンド: basis/upper/lower がローソクと同一 time 集合・whitespace 方式で返り、
    upper >= basis >= lower（有限点で）を満たす。meta.boll に window/z が入る。"""
    for src, code in (("hist1m", "US.TEST"), ("bars1m", "JP.TEST")):
        st, p = server.handle_api("/api/chart", {"source": src, "code": code})
        assert st == 200
        ct = [c["time"] for c in p["candles"]]
        assert len(ct) > 0
        b = p["boll"]
        assert [x["time"] for x in b["basis"]] == ct
        assert [x["time"] for x in b["upper"]] == ct
        assert [x["time"] for x in b["lower"]] == ct
        # 既定 z=3.0（fade_boll_z 未指定＝表示は既定 3.0）・window=20
        assert p["meta"]["boll"] == {"window": 20, "z": 3.0}
        # ウォームアップは whitespace、確定後は upper>=basis>=lower
        assert any("value" not in x for x in b["basis"])
        for i in range(len(ct)):
            up, mid, lo = b["upper"][i], b["basis"][i], b["lower"][i]
            if "value" in up and "value" in mid and "value" in lo:
                assert up["value"] >= mid["value"] >= lo["value"]
        _assert_json_clean(p)


def test_chart_boll_z_follows_fade_boll_z(synth_env):
    """fade_boll_z を指定するとバンドの z がその値になる（バンドに触れる==発火 の視覚一致）。"""
    st, p = server.handle_api("/api/chart", {"source": "bars1m", "code": "JP.TEST", "fade_boll_z": "2.0"})
    assert st == 200
    assert p["meta"]["boll"]["z"] == 2.0


def test_chart_bars1m_structure_and_rth_not_applied(synth_env):
    """bars1m（日本株）: 構造・エントリー・時間集合一致に加え、RTH フィルタ非適用を検証する。"""
    st, p = server.handle_api("/api/chart", {
        "source": "bars1m", "code": "JP.TEST", "from": "2099-01-05", "to": "2099-01-05",
    })
    assert st == 200
    assert p["meta"]["source"] == "bars1m"
    assert len(p["candles"]) > 0
    # RTH 非適用の証拠: 09:05（米国東部 RTH 09:31 より前）の先頭バーが残っている
    assert p["candles"][0]["time"] == server._epoch(pd.Timestamp("2099-01-05 09:05:00"))
    # 時間集合一致（軸ズレ対策）
    ct = [c["time"] for c in p["candles"]]
    assert [x["time"] for x in p["rci9"]] == ct
    assert [x["time"] for x in p["rci14"]] == ct
    # 既定パラメータでエントリーが 1 件以上（バー近似）
    assert len(p["entries"]) >= 1
    _assert_json_clean(p)


def test_chart_bars1m_includes_attention_list(synth_env):
    """bars1m の /api/chart に注目（事前アラート）配列 attention が付く（2026-07-19 追加）。

    注目はエントリーと独立の別配列。構造（list・各要素の必須キー）を固定する。件数は合成データ依存
    なので 0 件でも壊れないこと（キーは常に存在し list）を保証する。"""
    st, p = server.handle_api("/api/chart", {
        "source": "bars1m", "code": "JP.TEST", "from": "2099-01-05", "to": "2099-01-05",
    })
    assert st == 200
    assert isinstance(p["attention"], list)
    for a in p["attention"]:
        for key in ("time", "direction", "rci", "sar", "depth", "turn"):
            assert key in a
        assert a["direction"] in ("long", "short")
    _assert_json_clean(p)


def test_bars1m_missing_code_returns_message_with_day_count(synth_env):
    """存在しない銘柄は 200 + 空 + 「現在 N 日分」メッセージ（合成では 1 日分）。"""
    st, p = server.handle_api("/api/chart", {"source": "bars1m", "code": "JP.NOPE"})
    assert st == 200
    assert p["candles"] == []
    assert "自動保存" in p["message"]
    assert "1 日分" in p["message"]


def test_bars1m_prev_diff_min_filters_entries(synth_env):
    """直前差ゲート（prev_diff_min）を上げるとエントリーが減る（尖りフィルタが効く）。"""
    base = server.handle_api("/api/chart", {"source": "bars1m", "code": "JP.TEST"})[1]
    tight = server.handle_api("/api/chart", {
        "source": "bars1m", "code": "JP.TEST", "prev_diff_min": "200",  # 到達不能な閾値
    })[1]
    assert len(base["entries"]) >= 1
    assert len(tight["entries"]) == 0
    assert tight["meta"]["params"]["prev_diff_min"] == 200.0


def test_chart_ticks_structure(synth_env):
    st, p = server.handle_api("/api/chart", {
        "source": "ticks", "code": "US.TEST", "date": "2099-01-05", "tf": "5",
    })
    assert st == 200
    assert p["meta"]["tf"] == 5
    assert len(p["candles"]) > 0
    assert len(p["rci9"]) > 0
    # entries は engine 実行の結果（短いサンプルでは 0 でも構造が壊れないこと）
    assert isinstance(p["entries"], list)
    _assert_json_clean(p)


def test_bad_source_returns_400(synth_env):
    st, p = server.handle_api("/api/chart", {"source": "bogus"})
    assert st == 400
    assert "error" in p


def test_missing_hist_data_returns_200_with_message(synth_env):
    # 存在しない期間 → 200 + 空配列 + メッセージ（データ無しはエラーにしない）
    st, p = server.handle_api("/api/chart", {
        "source": "hist1m", "code": "US.TEST", "from": "2099-02-01", "to": "2099-02-02",
    })
    assert st == 200
    assert p["candles"] == []
    assert "message" in p
    assert p["meta"]["available_from"] is not None


def test_missing_ticks_returns_200_with_message(synth_env):
    st, p = server.handle_api("/api/chart", {
        "source": "ticks", "code": "US.TEST", "date": "2099-12-31", "tf": "10",
    })
    assert st == 200
    assert p["candles"] == []
    assert "録画" in p["message"]


def test_invalid_tf_is_400(synth_env):
    st, p = server.handle_api("/api/chart", {
        "source": "ticks", "code": "US.TEST", "date": "2099-01-05", "tf": "7",
    })
    assert st == 400
    assert "error" in p


def test_invalid_sma_is_400(synth_env):
    st, p = server.handle_api("/api/chart", {
        "source": "hist1m", "code": "US.TEST", "sma": "20,abc",
    })
    assert st == 400


def test_chart_hist1m_includes_stats(synth_env):
    """/api/chart に統計カード stats が付き、件数・勝率・期待値が JSON 化可能で載る。"""
    st, p = server.handle_api("/api/chart", {
        "source": "hist1m", "code": "US.TEST", "from": HIST_DATE, "to": HIST_DATE,
    })
    assert st == 200
    s = p["stats"]
    assert s["kind"] == "bar"
    assert s["n_signals"] == len(p["entries"])
    assert s["n_valid"] + s["n_truncated"] == s["n_signals"]
    assert s["n_long"] + s["n_short"] == s["n_valid"]
    # バー系の評価窓は +1本・+2本
    assert [h["label"] for h in s["horizons"]] == ["+1本", "+2本"]
    # 主指標（+1本）がトップレベルに展開される
    assert "expectancy_bp" in s and "median_net_bp" in s and "sum_net_bp" in s
    # 時間帯別は米国なので午前/午後
    assert [x["label"] for x in s["session"]] == ["午前", "午後"]
    assert "max_losing_streak" in s
    _assert_json_clean(p)


def test_chart_bars1m_stats_session_labels(synth_env):
    """日本株（bars1m）の時間帯別は前場/後場ラベルになる。"""
    st, p = server.handle_api("/api/chart", {"source": "bars1m", "code": "JP.TEST"})
    assert st == 200
    assert [x["label"] for x in p["stats"]["session"]] == ["前場", "後場"]


def test_chart_ticks_stats_kind_tick(synth_env):
    """ティックの統計は kind=tick・評価窓が +Ns で MFE/MAE キーを持つ。"""
    st, p = server.handle_api("/api/chart", {
        "source": "ticks", "code": "US.TEST", "date": "2099-01-05", "tf": "5",
    })
    assert st == 200
    s = p["stats"]
    assert s["kind"] in ("tick",) or s.get("empty")  # 0件でも構造は壊れない
    if not s.get("empty"):
        assert [h["label"] for h in s["horizons"]] == ["+30s", "+60s", "+120s"]
        assert "mfe_median_bp" in s and "mae_median_bp" in s
    _assert_json_clean(p)


def test_empty_chart_has_zero_stats(synth_env):
    """データ無しでも stats は n_signals=0・empty=True で返る（UI の「エントリーなし」表示用）。"""
    st, p = server.handle_api("/api/chart", {
        "source": "hist1m", "code": "US.TEST", "from": "2099-02-01", "to": "2099-02-02",
    })
    assert st == 200
    assert p["candles"] == []
    assert p["stats"]["n_signals"] == 0
    assert p["stats"]["empty"] is True


def test_screens_and_screen_empty(synth_env, monkeypatch):
    """reports が無い環境では screens は空、screen は message を返す。"""
    monkeypatch.setattr(server, "REPORTS_DIR", synth_env / "reports_none")
    st, body = server.handle_api("/api/screens", {})
    assert st == 200
    assert body["runs"] == [] and body["latest"] is None
    st2, sc = server.handle_api("/api/screen", {})
    assert st2 == 200
    assert sc["rows"] == [] and "message" in sc


def test_screens_and_screen_reads_ranking(synth_env, monkeypatch):
    """合成 ranking.csv を置くと screens に run が並び、screen が行を返す（US はコード表示）。"""
    reports = synth_env / "reports"
    run_dir = reports / "screen-20990105-120000"
    run_dir.mkdir(parents=True)
    pd.DataFrame([
        {"code": "US.SOXL", "n_signals_median": 600, "median_exp1_net_bp": -0.5,
         "median_win1": 0.47, "median_exp2_net_bp": -1.6, "median_win2": 0.48,
         "best_exp1_net_bp": 0.16, "best_win1": 0.48, "best_rci_period": 9,
         "best_slope_threshold": 20, "best_eff_min": 1.0},
    ]).to_csv(run_dir / "ranking.csv", index=False)
    monkeypatch.setattr(server, "REPORTS_DIR", reports)

    st, body = server.handle_api("/api/screens", {})
    assert st == 200
    assert body["runs"] == ["screen-20990105-120000"]
    st2, sc = server.handle_api("/api/screen", {"run": "screen-20990105-120000"})
    assert st2 == 200
    assert len(sc["rows"]) == 1
    r = sc["rows"][0]
    assert r["code"] == "US.SOXL" and r["name"] is None
    assert r["median_exp1"] == -0.5 and r["best_rci_period"] == 9.0
    _assert_json_clean(sc)


def test_screen_rejects_unknown_run(synth_env, monkeypatch):
    """未知の run 名はホワイトリスト外なので最新にフォールバック（パストラバーサル防止）。"""
    reports = synth_env / "reports"
    run_dir = reports / "screen-20990105-120000"
    run_dir.mkdir(parents=True)
    pd.DataFrame([{"code": "US.X", "n_signals_median": 1}]).to_csv(run_dir / "ranking.csv", index=False)
    monkeypatch.setattr(server, "REPORTS_DIR", reports)
    st, sc = server.handle_api("/api/screen", {"run": "../../etc"})
    assert st == 200
    assert sc["run"] == "screen-20990105-120000"  # 不正 run は最新に丸められる


def test_annotations_crud_and_context(synth_env):
    """注釈の作成（context 自動採取）→ 一覧（bar_epoch 付与）→ 削除の一連。DB は synth_env の DATA_DIR に隔離。"""
    # JP.TEST の実バー時刻（末尾）を epoch で取得
    _st, chart = server.handle_api("/api/chart", {"source": "bars1m", "code": "JP.TEST"})
    epoch = chart["candles"][-1]["time"]

    st, body = server.handle_api_post("/api/annotations", {
        "code": "JP.TEST", "bar_time": epoch, "kind": "entry_long", "reason": "V底の切り返し",
    })
    assert st == 200
    ann = body["annotation"]
    assert ann["id"] >= 1 and ann["kind"] == "entry_long"
    assert ann["bar_epoch"] == epoch
    assert ann["source"] == "bars1m"
    # context が自動採取されている（表示と同じ指標）
    assert ann["context"].get("price") is not None
    assert "rci9" in ann["context"] and "sar" in ann["context"]
    _assert_json_clean(body)

    st, lst = server.handle_api("/api/annotations", {"code": "JP.TEST"})
    assert st == 200 and len(lst["annotations"]) == 1
    assert lst["annotations"][0]["bar_epoch"] == epoch

    st, d = server.handle_api_post("/api/annotations/delete", {"id": ann["id"]})
    assert st == 200 and d["deleted"] is True
    _st, lst2 = server.handle_api("/api/annotations", {"code": "JP.TEST"})
    assert lst2["annotations"] == []


def test_annotation_attention_kind_via_api(synth_env):
    """注釈 API が kind='attention'（注目すべき・4 択目）を受理する（2026-07-19 追加）。"""
    _st, chart = server.handle_api("/api/chart", {"source": "bars1m", "code": "JP.TEST"})
    epoch = chart["candles"][-1]["time"]
    st, body = server.handle_api_post("/api/annotations", {
        "code": "JP.TEST", "bar_time": epoch, "kind": "attention", "reason": "落差が7割",
    })
    assert st == 200
    assert body["annotation"]["kind"] == "attention"


def test_annotations_bad_kind_is_400(synth_env):
    st, body = server.handle_api_post("/api/annotations", {
        "code": "JP.TEST", "bar_time": 1000000000, "kind": "bogus",
    })
    assert st == 400 and "error" in body


def test_screen_run_returns_aptitude_columns(synth_env, monkeypatch):
    """/api/screen/run が全 JP 銘柄に単一組を適用し、適性列（売買代金・レンジ・bars/日）付きで返す。"""
    monkeypatch.setattr(server, "REPORTS_DIR", synth_env / "reports_run")
    st, res = server.handle_api_post("/api/screen/run", {
        "slope_threshold": 15, "eff_min": 1, "spike_depth_min": 0, "spike_turn_min": 0,
    })
    assert st == 200
    assert res["n_symbols"] >= 1
    assert res["period"]["from"] == "2099-01-05"
    codes = {r["code"] for r in res["rows"]}
    assert "JP.TEST" in codes
    row = next(r for r in res["rows"] if r["code"] == "JP.TEST")
    for k in ("turnover_oku", "range_bp", "bars_per_day"):
        assert k in row and row[k] is not None
    # 手法適性バッジ（機能A）。◎/○/× のいずれか（レンジ中央値から算出）。
    assert row["aptitude"] in ("◎", "○", "×")
    _assert_json_clean(res)


def test_screen_run_empty_period_message(synth_env, monkeypatch):
    monkeypatch.setattr(server, "REPORTS_DIR", synth_env / "reports_run")
    st, res = server.handle_api_post("/api/screen/run", {"from": "2099-02-01", "to": "2099-02-02"})
    assert st == 200
    assert res["rows"] == [] and "message" in res


def test_screen_run_locked_returns_409(synth_env):
    """実行中（ロック取得済み）の /api/screen/run は 409 で弾く（二重実行防止）。"""
    server._SCREEN_RUN_LOCK.acquire()
    try:
        st, res = server.handle_api_post("/api/screen/run", {})
        assert st == 409 and "error" in res
    finally:
        server._SCREEN_RUN_LOCK.release()


def test_screen_run_bad_period_is_400(synth_env, monkeypatch):
    monkeypatch.setattr(server, "REPORTS_DIR", synth_env / "reports_run")
    st, res = server.handle_api_post("/api/screen/run", {"from": "not-a-date"})
    assert st == 400 and "error" in res


# --- エントリー履歴（機能: チャンピオン設定固定の全エントリー一覧） ---

def test_history_champion_structure(synth_env):
    """/api/history?code= がチャンピオン設定で全保存日のエントリーを日別＋累計付きで返す。"""
    st, h = server.handle_api("/api/history", {"code": "JP.TEST"})
    assert st == 200
    assert h["code"] == "JP.TEST"
    assert isinstance(h["days"], list)
    assert isinstance(h["eval_bars"], list) and len(h["eval_bars"]) >= 1
    # チャンピオン設定の要点が載る（履歴はチャート条件と独立の固定設定）
    for k in ("spike_depth_min", "spike_turn_min", "cooldown_bars", "cost_bp"):
        assert k in h["champion"]
    # 集計の構造（0 件でも壊れない）
    tot = h["totals"]
    assert {"n", "n_long", "n_short", "ret"} <= set(tot)
    for day in h["days"]:
        assert {"date", "entries", "subtotal"} <= set(day)
        for e in day["entries"]:
            assert e["direction"] in ("long", "short")
            assert "ret" in e and "cum" in e
    _assert_json_clean(h)


def test_history_missing_code_is_400(synth_env):
    st, res = server.handle_api("/api/history", {})
    assert st == 400 and "error" in res


def test_sources_includes_aptitude_hints(synth_env):
    """/api/sources に bars1m_aptitude（サイドバー適性印の code→◎/○ ヒント）が dict で載る。"""
    st, body = server.handle_api("/api/sources", {})
    assert st == 200
    assert isinstance(body["bars1m_aptitude"], dict)   # スクリーン結果が無ければ空 dict


def test_hist_entries_mapping_fields():
    """バー近似 entries → UI 明細の写像（方向・価格・前方リターン・時刻）を単体で確認。"""
    df = pd.DataFrame([{
        "time": pd.Timestamp("2099-01-05 09:50:00"), "code": "T", "direction": "short",
        "signal_close": 100.0, "entry_price": 99.9, "rci": -60.0, "drci1": -40.0,
        "prev_diff": -40.0, "efficiency": 12.3, "sar": 100.5, "trend": -1, "atr": 0.5,
        "atr_pct": 0.005, "bar_range_pct": 0.001, "norm_range": 0.2, "cost_bp": 3.0,
        "eval_truncated": False,
        "ret_1bar_gross": 5.0, "ret_1bar_net": 2.0,
        "ret_2bar_gross": 8.0, "ret_2bar_net": 5.0,
        "ret_3bar_gross": 1.0, "ret_3bar_net": -2.0,
    }])
    recs = server.hist_entries_to_records(df)
    assert len(recs) == 1
    r = recs[0]
    assert r["direction"] == "short"
    assert r["price"] == 99.9
    assert r["drci"] == -40.0
    assert r["prev_diff"] == -40.0
    labels = [f["label"] for f in r["forward"]]
    assert labels == ["+1本 net(bp)", "+2本 net(bp)", "+3本 net(bp)"]
    assert r["forward"][0]["bp"] == 2.0


def test_tick_entries_snap_to_bin():
    """ティック entries の時刻が表示足ビン開始にスナップされる。"""
    df = pd.DataFrame([{
        "time": pd.Timestamp("2099-01-05 10:00:07"), "code": "T", "direction": "long",
        "signal_price": 100.0, "entry_price": 100.1, "rci": 50.0, "drci": 30.0,
        "prev_diff": 45.0, "efficiency": 5.0, "sar": 99.0, "trend": 1, "atr": 0.3,
        "atr_pct": 0.003, "price_range_pct": 0.001, "norm_range": 0.3, "slope_elapsed_sec": 20.0,
        "half_spread_bp": 1.0, "cost_bp": 2.0, "mfe_bp": 10.0, "mae_bp": -3.0,
        "eval_truncated": False,
        "ret_30s_gross": 4.0, "ret_30s_net": 2.0, "ret_60s_gross": 6.0, "ret_60s_net": 4.0,
        "ret_120s_gross": 8.0, "ret_120s_net": 6.0,
    }])
    recs = server.tick_entries_to_records(df, tf_sec=5)
    assert len(recs) == 1
    r = recs[0]
    # 10:00:07 は 5 秒足なら 10:00:05 ビン開始にスナップされる
    assert r["time"] % 5 == 0
    assert r["time"] == server._epoch(pd.Timestamp("2099-01-05 10:00:05"))
    assert r["mfe"] == 10.0 and r["mae"] == -3.0
    assert r["prev_diff"] == 45.0   # ティック版は直前確定足 RCI との差を明細に出す
    assert any(f["label"] == "+60s net(bp)" for f in r["forward"])
