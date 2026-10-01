"""OpenD 診断 CLI（`python -m scalplab.datafeed.check`）。

翌朝これ一発で「日本株の相場権限が点いたか」を判定するのが目的。
到達性・ログイン状態・購読クォータ・JP.7203/US.AAPL スナップショット可否を出し、
最後に「JP quotes: OK / BLOCKED (No permission)」を1行で結論する。
"""
from __future__ import annotations

from futu import Market

from .moomoo_client import (
    MoomooClient,
    NoPermissionError,
    OpenDUnavailableError,
    check_opend_reachable,
)

JP_PROBE = "JP.7203"  # トヨタ
US_PROBE = "US.AAPL"


def _probe_snapshot(client: MoomooClient, code: str) -> tuple[bool, str]:
    """スナップショット可否を (ok, 詳細) で返す。権限拒否は ok=False かつ理由を BLOCKED に。"""
    try:
        snap = client.get_market_snapshot([code])
        if snap is None or snap.empty:
            return False, "空応答"
        price = snap.iloc[0].get("last_price")
        return True, f"last_price={price}"
    except NoPermissionError as e:
        return False, f"BLOCKED (No permission): {e}"
    except Exception as e:  # noqa: BLE001 - 診断なので種別問わず表示
        return False, f"ERROR: {e}"


def main() -> int:
    print("=== scalp-lab OpenD 診断 ===")

    # 1) 到達性（socket 事前チェック）
    try:
        check_opend_reachable()
        print("OpenD 到達性     : OK")
    except OpenDUnavailableError as e:
        print(f"OpenD 到達性     : NG - {e}")
        print("\nJP quotes: UNKNOWN (OpenD 未到達)")
        return 2

    client = MoomooClient()
    try:
        client.connect()

        # 2) グローバル状態
        state = client.get_global_state()
        print(f"server_ver       : {state.get('server_ver')}")
        print(f"qot_logined      : {state.get('qot_logined')}")
        print(f"trd_logined      : {state.get('trd_logined')}")
        print(f"program_status   : {state.get('program_status_type')}")

        # 3) 購読クォータ
        try:
            sub = client.query_subscription()
            print(
                f"購読クォータ     : used={sub.get('total_used')} remain={sub.get('remain')}"
            )
        except Exception as e:  # noqa: BLE001
            print(f"購読クォータ     : 取得失敗 - {e}")

        # 4) 銘柄マスタ件数（日本株の basicinfo は権限不要で通る想定）
        try:
            master = client.get_stock_basicinfo(Market.JP)
            print(f"JP 銘柄マスタ    : {len(master)} 件")
        except NoPermissionError as e:
            print(f"JP 銘柄マスタ    : BLOCKED - {e}")
        except Exception as e:  # noqa: BLE001
            print(f"JP 銘柄マスタ    : ERROR - {e}")

        # 5) スナップショット（相場権限の点灯確認）
        jp_ok, jp_detail = _probe_snapshot(client, JP_PROBE)
        us_ok, us_detail = _probe_snapshot(client, US_PROBE)
        print(f"{JP_PROBE} snapshot : {'OK' if jp_ok else 'NG'} - {jp_detail}")
        print(f"{US_PROBE} snapshot : {'OK' if us_ok else 'NG'} - {us_detail}")

        # 6) 結論
        print()
        if jp_ok:
            print("JP quotes: OK")
        else:
            print("JP quotes: BLOCKED (No permission)")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
