"""リアルタイムシグナル通知（scalplab.live）のユニットテスト。

OpenD 非依存で回す。検知（LiveDetector）はバックテストと同じ合成シナリオでロング発火を確認し、
通知（send_notification）は subprocess をモックして osascript の組み立てを検証、CSV（SignalLog）は
一時ファイルへの追記を検証する。handle_signal は通知呼び出しと CSV 追記の連携を検証する。
"""
import csv
import queue
from datetime import datetime

import pandas as pd
import pytest

from scalplab import live
from scalplab.strategy.rci_impulse import merge_params

from fixtures_synth import long_signal_scenario


# ---------------- パラメータ解決 ----------------

def test_resolve_params_defaults_to_champion():
    """params 未指定なら best-params.yaml のチャンピオンをロードする。"""
    p = live.resolve_params({"params": {}})
    assert p["slope_window_sec"] == 20
    assert p["slope_threshold"] == 30.0
    assert p["prev_diff_min"] == 25.0
    assert p["rci_period"] == 9


def test_resolve_params_explicit_overrides_champion():
    """live.yaml に params があればそれを DEFAULT にマージして使う（チャンピオンは読まない）。"""
    p = live.resolve_params({"params": {"slope_threshold": 0, "spike_depth_min": 100, "spike_turn_min": 40}})
    assert p["spike_depth_min"] == 100
    assert p["spike_turn_min"] == 40
    assert p["slope_threshold"] == 0
    # 未指定キーは DEFAULT_PARAMS で埋まる
    assert p["rci_period"] == 9


# ---------------- trigger ラベル ----------------

def test_trigger_label():
    assert live.trigger_label(merge_params({"spike_depth_min": 100})) == "spike"
    assert live.trigger_label(merge_params({"slope_threshold": 30})) == "angle"
    assert live.trigger_label(merge_params({"slope_threshold": 0, "prev_diff_min": 25})) == "prev_diff"
    assert live.trigger_label(merge_params({"slope_threshold": 0, "prev_diff_min": 0})) == "eff"


# ---------------- 時刻パース ----------------

def test_parse_tick_time_ns_matches_backtest():
    """ティック時刻文字列の ns 化が backtest の datetime64[ns].astype(int64) と一致する。"""
    s = "2026-07-15 09:30:00.123"
    assert live.parse_tick_time_ns(s) == int(pd.Timestamp(s).value)
    # 小数秒なしも受ける
    assert live.parse_tick_time_ns("2026-07-15 09:30:00") == int(pd.Timestamp("2026-07-15 09:30:00").value)


# ---------------- 検知（ティック → バー確定 → on_tick） ----------------

def test_detector_fires_long_signal():
    """合成シナリオ（backtest で 1 本ロング発火）を LiveDetector に流すとロングが出る。

    LiveDetector はティックから 1 分足を合成して on_bar_confirm を呼ぶ逐次経路。ベクトル化 engine と
    同一セマンティクスなので、同じティック列で同じロングが再現できる。
    """
    ticks, sig_start, sig_end = long_signal_scenario()
    t_ns = ticks["time"].to_numpy().astype("int64")
    px = ticks["price"].to_numpy()

    det = live.LiveDetector(["SYNTH.LONG"], merge_params(None))
    sigs = []
    for i in range(len(ticks)):
        s = det.process_tick("SYNTH.LONG", int(t_ns[i]), float(px[i]))
        if s is not None:
            sigs.append(s)

    longs = [s for s in sigs if s["direction"] == "long"]
    assert len(longs) >= 1, "ロングシグナルが 1 本も出ていない"
    # 仕込んだ分（sig_start〜sig_end）で発火している
    s0 = longs[0]
    assert int(sig_start.value) <= s0["t_ns"] < int(sig_end.value)
    assert s0["code"] == "SYNTH.LONG"
    assert s0["sar"] < s0["price"]  # ロング可（SAR が価格より下）


def test_detector_ignores_unknown_code():
    """監視外の銘柄コードは None を返す（誤購読の混入で落ちない）。"""
    det = live.LiveDetector(["US.TQQQ"], merge_params(None))
    assert det.process_tick("US.OTHER", 0, 100.0) is None


# ---------------- 通知（osascript のモック。macOS 経路を明示指定） ----------------
# system="Darwin" を渡すのは、Windows 実行機だと powershell 経路へ分岐して落ちるため
# （実装は正しく OS 分岐している。2026-08-17 Windows 実機の pytest で発覚）。

def test_send_notification_builds_osascript_with_sound():
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))

    live.send_notification("US.TQQQ LONG @ 45.2", sound="Glass", runner=fake_run,
                           system="Darwin")
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == "osascript"
    assert argv[1] == "-e"
    script = argv[2]
    assert "US.TQQQ LONG @ 45.2" in script
    assert 'with title "scalp-lab"' in script
    assert 'sound name "Glass"' in script


def test_send_notification_without_sound():
    calls = []
    live.send_notification("msg", sound=None, runner=lambda argv, **kw: calls.append(argv),
                           system="Darwin")
    assert "sound name" not in calls[0][2]


def test_send_notification_escapes_quotes():
    calls = []
    live.send_notification('a "b" c', sound=None, runner=lambda argv, **kw: calls.append(argv),
                           system="Darwin")
    # ダブルクォートが退避され、osascript リテラルを壊さない
    assert '\\"b\\"' in calls[0][2]


def test_send_notification_swallows_runner_error():
    """runner が例外を投げても検知ループを止めない（握って warning）。"""
    def boom(argv, **kwargs):
        raise RuntimeError("no osascript")

    # 例外が外へ出ないこと
    live.send_notification("msg", runner=boom)


# ---------------- CSV シンク ----------------

def _sample_sig(code="US.TQQQ", direction="long"):
    return {
        "t_ns": int(pd.Timestamp("2026-07-15 09:45:12.5").value),
        "code": code, "direction": direction, "price": 45.23,
        "rci": -82.5, "drci": 31.2,
    }


def test_signal_log_writes_header_and_rows(tmp_path):
    path = tmp_path / "live-signals-test.csv"
    slog = live.SignalLog(path)
    slog.append(_sample_sig(), "spike")
    slog.append(_sample_sig(direction="short"), "spike")
    slog.close()

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert list(rows[0].keys()) == live.SignalLog.COLUMNS
    assert rows[0]["code"] == "US.TQQQ"
    assert rows[0]["direction"] == "long"
    assert rows[0]["trigger"] == "spike"
    assert rows[1]["direction"] == "short"
    assert rows[0]["time"].startswith("2026-07-15 09:45:12")


def test_signal_log_appends_without_duplicate_header(tmp_path):
    """既存ファイルへの再オープンでヘッダを二重に書かない。"""
    path = tmp_path / "live-signals-test.csv"
    s1 = live.SignalLog(path)
    s1.append(_sample_sig(), "angle")
    s1.close()
    s2 = live.SignalLog(path)
    s2.append(_sample_sig(), "angle")
    s2.close()
    with open(path, newline="", encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    # ヘッダ 1 行 + データ 2 行
    assert len(lines) == 3
    assert lines[0].startswith("time,")


# ---------------- handle_signal（通知 + CSV の連携） ----------------

def test_handle_signal_notifies_and_logs(tmp_path):
    path = tmp_path / "live-signals-test.csv"
    slog = live.SignalLog(path)
    notified = []

    def fake_notifier(message, *, sound=None):
        notified.append((message, sound))

    handle_sig = _sample_sig()
    live.handle_signal(handle_sig, trigger="angle", signal_log=slog,
                       sound="Glass", notifier=fake_notifier)
    slog.close()

    assert len(notified) == 1
    msg, sound = notified[0]
    assert "US.TQQQ" in msg and "LONG" in msg and "[angle]" in msg
    assert sound == "Glass"

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["trigger"] == "angle"


# ---------------- 設定ロード ----------------

def test_load_live_config_defaults(tmp_path):
    """存在しないパスでも既定（watch/params/notify）が埋まる。"""
    cfg = live.load_live_config(tmp_path / "nope.yaml")
    assert cfg["watch"] == live.DEFAULT_WATCH
    assert cfg["params"] == {}
    assert cfg["notify"]["sound"] is True


def test_load_live_config_local_overlay(tmp_path):
    """隣の live.local.yaml がトップレベルキー単位で live.yaml を上書きする（二拠点の衝突回避）。"""
    (tmp_path / "live.yaml").write_text(
        "source: moomoo\nwatch:\n  - US.TQQQ\nnotify:\n  sound: false\n", encoding="utf-8")
    (tmp_path / "live.local.yaml").write_text(
        "source: kabu\nwatch:\n  - JP.7203\n", encoding="utf-8")

    cfg = live.load_live_config(tmp_path / "live.yaml")
    assert cfg["source"] == "kabu"          # local が上書き
    assert cfg["watch"] == ["JP.7203"]      # local が上書き
    assert cfg["notify"]["sound"] is False  # local に無いキーは live.yaml のまま


# ---------------- 通知の OS 分岐（platform / subprocess をモック） ----------------

def test_send_notification_macos_via_system_param():
    """system='Darwin' 明示で osascript 経路に入る（従来挙動）。"""
    calls = []
    live.send_notification("US.TQQQ LONG", sound="Glass",
                           runner=lambda argv, **kw: calls.append(argv), system="Darwin")
    assert calls[0][0] == "osascript"
    assert 'with title "scalp-lab"' in calls[0][2]
    assert 'sound name "Glass"' in calls[0][2]


def test_send_notification_windows_builds_powershell_toast():
    """system='Windows' で PowerShell の WinRT トーストを組み立てる（外部モジュール非依存）。"""
    calls = []
    live.send_notification("US.TQQQ LONG @ 45.2", sound="Glass",
                           runner=lambda argv, **kw: calls.append(argv), system="Windows")
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "powershell"
    assert "-Command" in argv
    script = argv[-1]
    assert "Windows.UI.Notifications" in script       # WinRT トースト API
    assert "US.TQQQ LONG @ 45.2" in script            # 本文
    assert "scalp-lab" in script                      # タイトル
    assert "[console]::beep" in script                # sound 有り→フォールバック beep 付き
    assert "catch" in script                          # トースト不可時のフォールバック節がある


def test_send_notification_windows_no_sound_omits_beep():
    """Windows で sound=None のときは beep を入れない。"""
    calls = []
    live.send_notification("msg", sound=None,
                           runner=lambda argv, **kw: calls.append(argv), system="Windows")
    assert "[console]::beep" not in calls[0][-1]


def test_send_notification_windows_escapes_single_quote():
    """Windows 経路で ' が '' に退避され PowerShell の単一引用符リテラルを壊さない。"""
    calls = []
    live.send_notification("a'b", sound=None,
                           runner=lambda argv, **kw: calls.append(argv), system="Windows")
    assert "a''b" in calls[0][-1]


def test_send_notification_windows_swallows_error():
    """Windows 経路でも runner の例外を握って検知ループを止めない。"""
    def boom(argv, **kwargs):
        raise RuntimeError("no powershell")

    live.send_notification("msg", runner=boom, system="Windows")  # 例外が外へ出ないこと


def test_send_notification_other_os_logs_only():
    """その他 OS（Linux 等）はデスクトップ通知コマンドを呼ばずログのみ。"""
    calls = []
    live.send_notification("msg", runner=lambda argv, **kw: calls.append(argv), system="Linux")
    assert calls == []


def test_send_notification_dispatches_by_platform(monkeypatch):
    """system 未指定なら platform.system() で分岐する（Windows をモックして確認）。"""
    calls = []
    monkeypatch.setattr(live.platform, "system", lambda: "Windows")
    live.send_notification("msg", runner=lambda argv, **kw: calls.append(argv))
    assert calls[0][0] == "powershell"


# ---------------- ティック供給源アダプタ（TickSource 抽象） ----------------

class _FakeTickSource(live.TickSource):
    """OpenD 非依存で run_live / check_mode を回すためのフェイク供給源。

    subscribe 時に合成シナリオのティックを (code, t_ns, price) で tick_queue に流し込み、
    実供給源の push を模す（state["ticks"] も加算）。connect/close の呼び出しも記録する。
    """

    name = "fake"

    def __init__(self, ticks_df, code="SYNTH.LONG"):
        self._ticks = ticks_df
        self._code = code
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True

    def subscribe(self, codes, tick_queue, state):
        t_ns = self._ticks["time"].to_numpy().astype("int64")
        px = self._ticks["price"].to_numpy()
        for i in range(len(self._ticks)):
            tick_queue.put((self._code, int(t_ns[i]), float(px[i])))
            state["ticks"] += 1
        return list(codes), []

    def subscription_stats(self):
        return {"total_used": 1, "remain": 99}

    def close(self):
        self.closed = True


def test_run_live_uses_tick_source_and_notifies(tmp_path):
    """run_live が source_factory 経由でティックを受け取り、発火→通知→CSV まで通る。"""
    ticks, _s, _e = long_signal_scenario()
    src = _FakeTickSource(ticks)
    notified = []
    path = tmp_path / "live-signals.csv"

    rc = live.run_live(
        ["SYNTH.LONG"], merge_params(None),
        duration=0.4, session=False,
        signal_log_path=path,
        notifier=lambda msg, *, sound=None: notified.append((msg, sound)),
        source_factory=lambda: src,
    )

    assert rc == 0
    assert src.connected and src.closed  # 供給源のライフサイクルが回っている
    assert len(notified) >= 1
    assert "SYNTH.LONG" in notified[0][0] and "LONG" in notified[0][0]
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) >= 1
    assert rows[0]["code"] == "SYNTH.LONG"


def test_check_mode_with_fake_source(capsys):
    """check_mode も source_factory 差し替えで OpenD 非依存に疎通確認できる。"""
    ticks, _s, _e = long_signal_scenario()
    src = _FakeTickSource(ticks)

    rc = live.check_mode(["SYNTH.LONG"], seconds=0, source_factory=lambda: src)

    assert rc == 0
    assert src.closed
    out = capsys.readouterr().out
    assert "接続 : OK" in out
    assert "購読成功        : 1/1" in out


def test_tachibana_source_is_dummy():
    """立花枠は未実装のため接続系は NotImplementedError（セッションだけは東証で返せる）。"""
    src = live.TachibanaTickSource()
    with pytest.raises(NotImplementedError):
        src.connect()
    with pytest.raises(NotImplementedError):
        src.subscribe(["X"], queue.Queue(), {"ticks": 0})
    with pytest.raises(NotImplementedError):
        src.subscription_stats()
    with pytest.raises(NotImplementedError):
        src.close()
    # 日本株枠なのでセッションは東証を返す
    assert src.session_segments("2026-07-16")[-1][1].strftime("%H:%M") == "15:30"


# ---------------- kabu STATION 供給源（モック REST + モック WebSocket） ----------------

def _kabu_push(symbol="7203", price=2000.0, t="2026-07-16T09:00:01+09:00"):
    """kabu の WebSocket PUSH（価格更新）1件を JSON 文字列で作る。"""
    import json as _json
    return _json.dumps({"Symbol": symbol, "Exchange": 1, "CurrentPrice": price,
                        "CurrentPriceTime": t})


def _kabu_ok_http(method, path, headers, body):
    """token/register/unregister をすべて成功で返すモック REST。"""
    if path.endswith("/token"):
        return 200, {"ResultCode": 0, "Token": "TK"}
    if path.endswith("/register"):
        syms = [s["Symbol"] for s in (body or {}).get("Symbols", [])]
        return 200, {"RegistList": [{"Symbol": s, "Exchange": 1} for s in syms]}
    return 200, {}


def test_kabu_handle_message_normalizes_tick():
    """PUSH を (JP.コード, JST壁時計ns, price) に正規化して tick_queue に投入する。"""
    src = live.KabuStationTickSource(password="pw", http_request=_kabu_ok_http,
                                     ws_connect=lambda: iter(()), auto_stream=False)
    q = queue.Queue()
    state = {"ticks": 0}
    src._tick_queue = q
    src._state = state
    src._handle_message(_kabu_push("7203", 2000.5, "2026-07-16T09:00:01+09:00"))
    code, t_ns, price = q.get_nowait()
    assert code == "JP.7203"
    assert price == 2000.5
    # moomoo/backtest と同じ「JST 壁時計 naive の ns」
    assert t_ns == int(pd.Timestamp("2026-07-16 09:00:01").value)
    assert state["ticks"] == 1


def test_kabu_handle_message_skips_non_price_and_broken():
    """価格の無い PUSH（板のみ）と壊れた JSON は投入しない。"""
    src = live.KabuStationTickSource(password="pw", http_request=_kabu_ok_http, auto_stream=False)
    q = queue.Queue()
    state = {"ticks": 0}
    src._tick_queue = q
    src._state = state
    import json as _json
    src._handle_message(_json.dumps({"Symbol": "7203", "CurrentPrice": None}))
    src._handle_message("not-json")
    assert q.empty() and state["ticks"] == 0


def test_kabu_connect_requires_password(monkeypatch):
    """APIPassword 未設定なら TickSourceUnavailableError（環境変数もキーも無い）。"""
    monkeypatch.delenv("KABU_API_PASSWORD", raising=False)
    src = live.KabuStationTickSource()
    with pytest.raises(live.TickSourceUnavailableError):
        src.connect()


def test_kabu_password_from_env(monkeypatch):
    """config の password_env で指定した環境変数から APIPassword を読む。"""
    monkeypatch.setenv("MY_KABU_PW", "secret")
    src = live.KabuStationTickSource({"kabu": {"password_env": "MY_KABU_PW"}},
                                     http_request=_kabu_ok_http)
    assert src._password == "secret"


def test_kabu_subscribe_token_and_register_flow():
    """subscribe が token 取得 → register（X-API-KEY にトークン）を行い、RegistList で ok/failed を分ける。"""
    calls = []

    def fake_http(method, path, headers, body):
        calls.append((method, path, body, headers.get("X-API-KEY")))
        if path.endswith("/token"):
            return 200, {"Token": "TK1"}
        if path.endswith("/register"):
            return 200, {"RegistList": [{"Symbol": "7203", "Exchange": 1}]}  # 9999 は反映されない
        return 200, {}

    src = live.KabuStationTickSource(password="pw", http_request=fake_http,
                                     ws_connect=lambda: iter(()), auto_stream=False)
    ok, failed = src.subscribe(["JP.7203", "JP.9999"], queue.Queue(), {"ticks": 0})
    assert ok == ["JP.7203"]
    assert failed and failed[0][0] == "JP.9999"
    reg = [c for c in calls if c[1].endswith("/register")][0]
    assert reg[3] == "TK1"  # X-API-KEY にトークンを載せている
    assert reg[2] == {"Symbols": [{"Symbol": "7203", "Exchange": 1},
                                  {"Symbol": "9999", "Exchange": 1}]}
    assert src.subscription_stats() == {"total_used": 1, "remain": 49}


def test_kabu_register_refreshes_token_on_401():
    """register が 401（トークン失効）なら token を取り直して1回だけ再登録する。"""
    reg_count = {"n": 0}
    tokens = iter(["TK1", "TK2"])

    def fake_http(method, path, headers, body):
        if path.endswith("/token"):
            return 200, {"Token": next(tokens)}
        if path.endswith("/register"):
            reg_count["n"] += 1
            if reg_count["n"] == 1:
                return 401, {"Code": 4001, "Message": "token expired"}
            return 200, {"RegistList": [{"Symbol": "7203", "Exchange": 1}]}
        return 200, {}

    src = live.KabuStationTickSource(password="pw", http_request=fake_http,
                                     ws_connect=lambda: iter(()), auto_stream=False)
    src.connect()  # TK1
    ok, _failed = src.subscribe(["JP.7203"], queue.Queue(), {"ticks": 0})
    assert ok == ["JP.7203"]
    assert reg_count["n"] == 2       # 401 で1回リトライした
    assert src._token == "TK2"       # トークンを取り直した


def test_kabu_unregister_all_clears_slots():
    """unregister_all が PUT /unregister/all を X-API-KEY 付きで叩き (status, data) を返す。"""
    calls = []

    def fake_http(method, path, headers, body):
        calls.append((method, path, body, headers.get("X-API-KEY")))
        return 200, {"ResultCode": 0}

    src = live.KabuStationTickSource(password="pw", http_request=fake_http, auto_stream=False)
    src._token = "TK1"
    status, data = src.unregister_all()
    assert status == 200 and data == {"ResultCode": 0}
    assert calls == [("PUT", "/kabusapi/unregister/all", None, "TK1")]


def test_kabu_unregister_all_refreshes_token_on_401():
    """unregister_all が 401（トークン失効）なら token を取り直して1回だけ再試行する。"""
    unreg_count = {"n": 0}
    tokens = iter(["TK2"])

    def fake_http(method, path, headers, body):
        if path.endswith("/token"):
            return 200, {"Token": next(tokens)}
        if path.endswith("/unregister/all"):
            unreg_count["n"] += 1
            if unreg_count["n"] == 1:
                return 401, {"Code": 4001007, "Message": "ログイン認証エラー"}
            return 200, {"ResultCode": 0}
        return 200, {}

    src = live.KabuStationTickSource(password="pw", http_request=fake_http, auto_stream=False)
    src._token = "TK1"
    status, _data = src.unregister_all()
    assert status == 200
    assert unreg_count["n"] == 2   # 401 で1回リトライした
    assert src._token == "TK2"     # トークンを取り直した


def test_kabu_ws_loop_reconnects_once():
    """WebSocket が途中で切れたら1回だけ再接続して残りのティックを拾う（reconnect_tries=1）。"""
    def gen_drop():
        yield _kabu_push("7203", 100.0)
        raise ConnectionError("dropped")

    def gen_rest():
        yield _kabu_push("7203", 101.0)

    conns = iter([gen_drop(), gen_rest()])
    src = live.KabuStationTickSource(password="pw", http_request=_kabu_ok_http,
                                     ws_connect=lambda: next(conns),
                                     reconnect_tries=1, auto_stream=False)
    q = queue.Queue()
    state = {"ticks": 0}
    src._tick_queue = q
    src._state = state
    src._registered = ["JP.7203"]
    src._token = "TK"
    src._ws_loop()  # 同期実行（stop は立てない・再接続上限で自然終了）

    prices = []
    while not q.empty():
        prices.append(q.get_nowait()[2])
    assert prices == [100.0, 101.0]  # 切断前と再接続後の両方を受信


def test_kabu_check_mode(capsys):
    """--check が kabu 供給源でも通る（token→register→統計→close）。"""
    def make():
        return live.KabuStationTickSource(password="pw", http_request=_kabu_ok_http,
                                          ws_connect=lambda: iter(()), auto_stream=False)

    rc = live.check_mode(["JP.7203"], seconds=0, source_factory=make)
    assert rc == 0
    out = capsys.readouterr().out
    assert "kabu STATION 接続 : OK" in out
    assert "購読成功        : 1/1" in out


def test_kabu_session_is_tokyo():
    """kabu のセッションは東証立会（前場 09:00 / 大引け 15:30）。"""
    src = live.KabuStationTickSource(password="pw", http_request=_kabu_ok_http)
    segs = src.session_segments("2026-07-16")
    assert segs[0][0].strftime("%H:%M") == "09:00"
    assert segs[-1][1].strftime("%H:%M") == "15:30"
    assert src.session_day(datetime(2026, 7, 16, 10, 0)) == "2026-07-16"


def test_moomoo_session_delegates_to_us_helpers():
    """moomoo のセッションは US ヘルパに委譲される（互換）。"""
    from scalplab.record import us_session_day, us_session_segments
    src = live.MoomooTickSource()
    assert src.session_day() == us_session_day()
    day = us_session_day()
    assert src.session_segments(day) == us_session_segments(day)


def test_run_live_with_kabu_source(tmp_path):
    """run_live が kabu 供給源でも発火→通知→CSV まで通る（WebSocket は合成ティックをモック注入）。"""
    ticks, _s, _e = long_signal_scenario()
    t_ns = ticks["time"].to_numpy().astype("int64")
    px = ticks["price"].to_numpy()
    msgs = [_kabu_push("SYNTH", float(px[i]), pd.Timestamp(int(t_ns[i])).isoformat())
            for i in range(len(ticks))]

    def make():
        # RegistList に SYNTH を含める register モック（コードは JP.SYNTH で監視）
        def http(method, path, headers, body):
            if path.endswith("/token"):
                return 200, {"Token": "TK"}
            if path.endswith("/register"):
                return 200, {"RegistList": [{"Symbol": "SYNTH", "Exchange": 1}]}
            return 200, {}
        return live.KabuStationTickSource(password="pw", http_request=http,
                                          ws_connect=lambda: iter(msgs), reconnect_tries=0)

    notified = []
    path = tmp_path / "live-signals.csv"
    rc = live.run_live(
        ["JP.SYNTH"], merge_params(None),
        duration=0.5, session=False,
        signal_log_path=path,
        notifier=lambda m, *, sound=None: notified.append(m),
        source_factory=make,
    )
    assert rc == 0
    assert len(notified) >= 1
    assert "JP.SYNTH" in notified[0] and "LONG" in notified[0]
