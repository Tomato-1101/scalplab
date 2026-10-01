"""scalplab.premarket（寄り前気配の記録）の単体テスト。実機（kabuステーション）非依存。

/board のレスポンスはモックで注入する。時刻・sleep も DI で注入し、フェーズ切替
（all → focus）と終了判定を仮想時計で検証する。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from scalplab import premarket
from scalplab.premarket import (collect, flatten_board, indicative_price, select_focus)


def _board(symbol: str, prev_close: float, calc: float | None = None, **extra) -> dict:
    d = {
        "Symbol": symbol, "SymbolName": f"銘柄{symbol}", "PreviousClose": prev_close,
        "CurrentPrice": None, "CalcPrice": calc,
        "Sell1": {"Price": calc, "Qty": 100, "Sign": "0101"},
        "Buy1": {"Price": calc, "Qty": 200, "Sign": "0101"},
        "OverSellQty": 1000, "UnderBuyQty": 2000,
    }
    d.update(extra)
    return d


class _FakeClient:
    """board_snapshot / unregister_all を持つ偽クライアント。

    kabu 実機の「**/board が内部で銘柄を自動登録して 50 枠を消費する**」挙動（2026-08-19 実測）を
    模擬する。未登録銘柄の /board は枠を1つ使い、枠が満杯なら `Code 4002006 レジスト数エラー` を返す。
    unregister_all() で枠が空く。ローテーション（枠を空けてから次の50銘柄）の回帰用。
    """

    def __init__(self, boards: dict[str, dict], slot_limit: int = premarket._BOARD_SLOT_LIMIT):
        self.boards = boards
        self.calls: list[str] = []
        self.slot_limit = slot_limit
        self.registered: set[str] = set()
        self.log: list[tuple[str, str | None]] = []  # 呼び出し順（board / unregister の交互を検証する）
        self.statuses: list[tuple[str, int]] = []
        self.unregister_calls = 0
        self.unregister_fail = None  # int(status) か Exception を入れると unregister_all が失敗する

    def board_snapshot(self, code: str):
        self.calls.append(code)
        self.log.append(("board", code))
        sym = code.replace("JP.", "")
        if sym not in self.registered:
            if len(self.registered) >= self.slot_limit:
                self.statuses.append((code, 400))
                return 400, {"Code": 4002006, "Message": "レジスト数エラー"}
            self.registered.add(sym)
        if sym in self.boards:
            self.statuses.append((code, 200))
            return 200, self.boards[sym]
        self.statuses.append((code, 404))
        return 404, {"Code": 4002001, "Message": "銘柄未発見"}

    def unregister_all(self):
        self.unregister_calls += 1
        self.log.append(("unregister", None))
        if isinstance(self.unregister_fail, Exception):
            raise self.unregister_fail
        if self.unregister_fail is not None:
            return int(self.unregister_fail), {"Message": "unregister 失敗"}
        self.registered.clear()
        return 200, {}


class _Clock:
    """仮想時計。tick() を呼ぶたびに step 秒進む。sleep も時間を進める。"""

    def __init__(self, start: datetime, step: float = 1.0):
        self.t = start
        self.step = step

    def now(self) -> datetime:
        self.t += timedelta(seconds=self.step)
        return self.t

    def sleep(self, sec: float) -> None:
        self.t += timedelta(seconds=sec)


# ---------------- flatten / indicative_price / select_focus ----------------

def test_flatten_board_keeps_scalar_depth_and_raw():
    t = datetime(2026, 8, 18, 8, 30)
    row = flatten_board("JP.7203", _board("7203", 3000.0, calc=2900.0), "all", t)
    assert row["code"] == "JP.7203" and row["phase"] == "all" and row["fetched_at"] == t
    assert row["previousclose"] == 3000.0 and row["calcprice"] == 2900.0
    assert row["sell1_price"] == 2900.0 and row["buy1_qty"] == 200
    assert '"OverSellQty":1000' in row["raw"]  # 未知フィールドも raw に全部残る


def test_flatten_board_missing_fields_are_none():
    row = flatten_board("JP.9984", {"Symbol": "9984"}, "all", datetime.now())
    assert row["previousclose"] is None and row["sell2_price"] is None


def test_indicative_price_priority_calc_then_quotes():
    base = {"calcprice": 100.0, "sell1_price": 99.0, "buy1_price": 98.0, "currentprice": 97.0}
    assert indicative_price(base) == 100.0
    assert indicative_price({**base, "calcprice": None}) == 99.0
    assert indicative_price({"calcprice": 0, "sell1_price": None, "buy1_price": None,
                             "currentprice": None}) is None


def test_select_focus_picks_deepest_gaps():
    t = datetime(2026, 8, 18, 8, 50)
    rows = [
        flatten_board("JP.1111", _board("1111", 1000.0, calc=900.0), "all", t),   # -10%
        flatten_board("JP.2222", _board("2222", 1000.0, calc=1050.0), "all", t),  # +5%
        flatten_board("JP.3333", _board("3333", 1000.0, calc=950.0), "all", t),   # -5%
        flatten_board("JP.4444", {"Symbol": "4444"}, "all", t),                    # 気配なし→除外
    ]
    got = select_focus(rows, ["JP.1111", "JP.2222", "JP.3333", "JP.4444"], focus_n=2)
    assert got == ["JP.1111", "JP.3333"]  # 下位（最も下げている）から順


def test_select_focus_uses_latest_snapshot():
    t = datetime(2026, 8, 18, 8, 30)
    rows = [
        flatten_board("JP.1111", _board("1111", 1000.0, calc=900.0), "all", t),
        # 後の巡回で気配が +5% に切り上がった → 最新が使われ下位にならない
        flatten_board("JP.1111", _board("1111", 1000.0, calc=1050.0), "all",
                      t + timedelta(minutes=10)),
        flatten_board("JP.2222", _board("2222", 1000.0, calc=990.0), "all", t),
    ]
    got = select_focus(rows, ["JP.1111", "JP.2222"], focus_n=1)
    assert got == ["JP.2222"]


# ---------------- collect ループ ----------------

def _cfg(codes: list[str], **over) -> dict:
    cfg = {"collect_start": "08:00", "focus_start": "08:55", "end": "09:07",
           "focus_n": 1, "throttle_sec": 0.0, "snapshot_every_sec": 99999,
           "codes": codes}
    cfg.update(over)
    return cfg


def test_collect_switches_to_focus_and_saves(tmp_path, monkeypatch):
    monkeypatch.setattr(premarket, "OUT_DIR", tmp_path)
    boards = {"1111": _board("1111", 1000.0, calc=900.0),
              "2222": _board("2222", 1000.0, calc=1050.0)}
    client = _FakeClient(boards)
    # 08:54 開始・step 30秒 → 数周で 08:55 を跨いで focus へ移行し 09:07 で終わる
    clock = _Clock(datetime(2026, 8, 18, 8, 54), step=30.0)
    path = collect(_cfg(["JP.1111", "JP.2222"]), client, now_fn=clock.now, sleep_fn=clock.sleep)
    df = pd.read_parquet(path)
    assert set(df["phase"]) == {"all", "focus"}
    # focus フェーズは下位1銘柄（JP.1111）だけを巡回している
    assert set(df[df["phase"] == "focus"]["code"]) == {"JP.1111"}


def test_collect_exits_when_past_end(tmp_path, monkeypatch):
    monkeypatch.setattr(premarket, "OUT_DIR", tmp_path)
    client = _FakeClient({})
    clock = _Clock(datetime(2026, 8, 18, 9, 30), step=1.0)  # 既に end 超過
    path = collect(_cfg(["JP.1111"]), client, now_fn=clock.now, sleep_fn=clock.sleep)
    assert client.calls == []  # 1件も叩かない
    assert not path.exists()


def test_collect_error_status_does_not_stop_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(premarket, "OUT_DIR", tmp_path)
    boards = {"1111": _board("1111", 1000.0, calc=900.0)}  # 2222 は 404 になる
    client = _FakeClient(boards)
    clock = _Clock(datetime(2026, 8, 18, 8, 54), step=30.0)
    path = collect(_cfg(["JP.2222", "JP.1111"]), client, now_fn=clock.now, sleep_fn=clock.sleep)
    df = pd.read_parquet(path)
    assert (df["code"] == "JP.1111").any()  # 404 の後続銘柄も巡回できている
    assert not (df["code"] == "JP.2222").any()


def test_collect_duration_mode_ignores_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr(premarket, "OUT_DIR", tmp_path)
    boards = {"1111": _board("1111", 1000.0, calc=900.0)}
    client = _FakeClient(boards)
    clock = _Clock(datetime(2026, 8, 18, 15, 0), step=1.0)  # 市場外の時刻でも動く
    path = collect(_cfg(["JP.1111"]), client, now_fn=clock.now, sleep_fn=clock.sleep,
                   duration=10.0)
    df = pd.read_parquet(path)
    assert len(df) >= 1 and set(df["phase"]) == {"all"}


# ---------------- 50 枠ローテーション（/board が登録枠を消費する対策） ----------------

def _many_boards(n: int) -> tuple[list[str], dict[str, dict]]:
    """n 銘柄ぶんの巡回リストと board 応答を作る（後ろの銘柄ほどギャップが深い）。"""
    codes, boards = [], {}
    for i in range(n):
        sym = str(1000 + i)
        codes.append(f"JP.{sym}")
        boards[sym] = _board(sym, 1000.0, calc=1000.0 - i)
    return codes, boards


def test_collect_rotation_covers_codes_beyond_slot_limit(tmp_path, monkeypatch):
    """50 銘柄超（120）でも 1 周で全銘柄が取れ、レジスト数エラー(4002006)が出ない。"""
    monkeypatch.setattr(premarket, "OUT_DIR", tmp_path)
    codes, boards = _many_boards(120)
    client = _FakeClient(boards)
    clock = _Clock(datetime(2026, 8, 18, 8, 0), step=1.0)
    path = collect(_cfg(codes), client, now_fn=clock.now, sleep_fn=clock.sleep, duration=600.0)
    df = pd.read_parquet(path)
    assert set(df["code"]) == set(codes)
    assert [c for c, st in client.statuses if st == 400] == []   # 枠あふれゼロ
    assert client.unregister_calls >= 3                          # 120 銘柄 = 3 バッチ/周


def test_collect_unregisters_before_every_batch(tmp_path, monkeypatch):
    """呼び出し順が unregister → board×50 → unregister → board×50 → ... になる。"""
    monkeypatch.setattr(premarket, "OUT_DIR", tmp_path)
    codes, boards = _many_boards(120)
    client = _FakeClient(boards)
    clock = _Clock(datetime(2026, 8, 18, 8, 0), step=1.0)
    collect(_cfg(codes), client, now_fn=clock.now, sleep_fn=clock.sleep, duration=600.0)
    head = client.log[:123]  # 1 周ぶん（unregister 3 + board 120）
    assert [i for i, (kind, _c) in enumerate(head) if kind == "unregister"] == [0, 51, 102]


@pytest.mark.parametrize("fail", [500, RuntimeError("unregister boom")])
def test_collect_continues_when_unregister_fails(tmp_path, monkeypatch, fail):
    """unregister/all が非200でも例外でも巡回は止まらない（次バッチで自己回復する設計）。"""
    monkeypatch.setattr(premarket, "OUT_DIR", tmp_path)
    codes, boards = _many_boards(3)
    client = _FakeClient(boards)
    client.unregister_fail = fail
    clock = _Clock(datetime(2026, 8, 18, 8, 0), step=1.0)
    path = collect(_cfg(codes), client, now_fn=clock.now, sleep_fn=clock.sleep, duration=30.0)
    df = pd.read_parquet(path)
    assert set(df["code"]) == set(codes)
    assert client.unregister_calls >= 1


def test_collect_switches_phase_at_batch_boundary(tmp_path, monkeypatch):
    """focus 移行はバッチ境界で起きる（1 周＝約6分の完了を待たない）。"""
    monkeypatch.setattr(premarket, "OUT_DIR", tmp_path)
    codes, boards = _many_boards(120)
    client = _FakeClient(boards)
    # 08:53 開始・step 1秒 → 1 バッチ(50銘柄)が約103秒。08:55 を跨いだ次のバッチ境界で移行する
    clock = _Clock(datetime(2026, 8, 18, 8, 53), step=1.0)
    path = collect(_cfg(codes, focus_n=2), client, now_fn=clock.now, sleep_fn=clock.sleep)
    df = pd.read_parquet(path)
    n_all = int((df["phase"] == "all").sum())
    assert 0 < n_all < len(codes)                        # 全 120 銘柄の 1 周完了を待っていない
    assert n_all % premarket._BOARD_SLOT_LIMIT == 0      # 移行はバッチ境界で起きた
    assert len(set(df[df["phase"] == "focus"]["code"])) == 2


def test_collect_retries_board_timeout_once(tmp_path, monkeypatch):
    """board が例外（タイムアウト）でも 1 回だけ即時リトライして取り切る（2026-08-28 追加）。

    寄り前はサーバが重くタイムアウトが集中する実測（同日 89 件）への対策。
    1 回目失敗 → 2 回目成功なら行が残り、2 回連続失敗ならエラー計上して次の銘柄へ進む。
    """
    monkeypatch.setattr(premarket, "OUT_DIR", tmp_path)

    class _FlakyClient(_FakeClient):
        def __init__(self, boards, fail_counts: dict[str, int]):
            super().__init__(boards)
            self.remaining = dict(fail_counts)  # code → あと何回例外を投げるか

        def board_snapshot(self, code: str):
            if self.remaining.get(code, 0) > 0:
                self.remaining[code] -= 1
                self.calls.append(code)
                raise TimeoutError("timed out")
            return super().board_snapshot(code)

    boards = {"1111": _board("1111", 1000.0, calc=900.0),
              "2222": _board("2222", 1000.0, calc=950.0)}
    # 1111 は 1 回失敗→リトライで成功、2222 は 2 回失敗＝リトライも失敗
    client = _FlakyClient(boards, {"JP.1111": 1, "JP.2222": 2})
    clock = _Clock(datetime(2026, 8, 18, 8, 0), step=1.0)
    path = collect(_cfg(["JP.1111", "JP.2222"]), client,
                   now_fn=clock.now, sleep_fn=clock.sleep, duration=10.0)
    df = pd.read_parquet(path)
    assert (df["code"] == "JP.1111").any()      # リトライで救えた
    assert not (df["code"] == "JP.2222").any()  # 2 連続失敗はスキップ（巡回は止まらない）


def test_save_merges_existing_parquet(tmp_path, monkeypatch):
    """_save は同日 parquet を上書きせずマージする（2026-08-21 実害の再発防止）。

    5 分リトライで別プロセスが同日を録り直しても、前プロセスの途中保存が消えない。
    同一 (fetched_at, code) の重複は 1 行に畳まれる。
    """
    monkeypatch.setattr(premarket, "OUT_DIR", tmp_path)
    t1 = datetime(2026, 8, 28, 8, 10)
    t2 = datetime(2026, 8, 28, 8, 40)
    row_a = flatten_board("JP.1111", _board("1111", 1000.0, calc=900.0), "all", t1)
    row_b = flatten_board("JP.2222", _board("2222", 1000.0, calc=950.0), "all", t2)
    premarket._save([row_a], "2026-08-28")            # 前プロセスの途中保存
    premarket._save([row_a, row_b], "2026-08-28")     # 同一行を含む再保存（同一プロセスの途中保存相当）
    path = premarket._save([row_b], "2026-08-28")     # 別プロセスが新規分だけ保存
    df = pd.read_parquet(path)
    assert len(df) == 2                                # 重複は畳まれ、両プロセスの行が残る
    assert set(df["code"]) == {"JP.1111", "JP.2222"}


# ---------------- board_snapshot（live.py 側の追加分） ----------------

def test_board_snapshot_retries_on_401(monkeypatch):
    from scalplab.live import KabuStationTickSource
    calls = []

    def fake_http(method, path, headers, body):
        calls.append((method, path))
        if path.endswith("/token"):
            return 200, {"Token": "T2"}
        if len([c for c in calls if "/board/" in c[1]]) == 1:
            return 401, {"Message": "token expired"}
        return 200, {"Symbol": "7203"}

    src = KabuStationTickSource({}, password="x", http_request=fake_http, auto_stream=False)
    src._token = "T1"
    status, data = src.board_snapshot("JP.7203")
    assert status == 200 and data["Symbol"] == "7203"
    assert src._token == "T2"  # 401 → トークン再取得 → リトライ成功
