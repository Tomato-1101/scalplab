#!/usr/bin/env bash
# moomoo_OpenD.app の死活監視。11111 が待ち受けていなければ起動する。
#
# OpenD は GUI アプリで、落ちても・Mac を再起動しても自動では上がらない。2026-07-28 に
# 停止したまま 3 営業日気付かず、ティック録画（日 250MB）が丸ごと欠落した。その再発防止。
# launchd（com.scalplab.opend）からログイン時と 5 分毎に呼ばれる。
#
# 単体実行: bash scripts/opend_guard.sh
set -uo pipefail

PORT=11111
APP="/Applications/moomoo_OpenD.app"

# nc の接続確認は OpenD が起動途中でも失敗しうるので、待ち受け確認だけを条件にする。
if nc -z -w 3 127.0.0.1 "${PORT}" 2>/dev/null; then
    exit 0
fi

if [[ ! -d "${APP}" ]]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ${APP} が見つからない。OpenD を再インストールすること。" >&2
    exit 1
fi

# プロセスは生きているのにポートだけ死んでいる状態がある（2026-07-28: OpenD 本体は 7/15 から
# 動き続けていたが 11111 への connect が EADDRNOTAVAIL で 2 営業日録画不能だった）。
# この状態では既存インスタンスがあるため `open` は何もしないので、先に終了させる。
PIDS="$(pgrep -f "${APP}/Contents/MacOS/moomoo_OpenD" || true)"
if [[ -n "${PIDS}" ]]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') OpenD は起動中だが ${PORT} が不達（API 停止）。PID ${PIDS//$'\n'/ } を終了して再起動する。"
    kill ${PIDS} 2>/dev/null || true
    for _ in $(seq 1 10); do
        sleep 1
        pgrep -f "${APP}/Contents/MacOS/moomoo_OpenD" >/dev/null || break
    done
    pkill -9 -f "${APP}/Contents/MacOS/moomoo_OpenD" 2>/dev/null || true
    sleep 2
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') OpenD 不達（127.0.0.1:${PORT}）。${APP} を起動する。"
fi

# -g: 前面に出さずバックグラウンドで起動（作業中にフォーカスを奪わない）
open -g "${APP}"

# 起動待ち（最大 60 秒）。上がりきらない場合はログインが切れている可能性が高い。
for _ in $(seq 1 30); do
    sleep 2
    if nc -z -w 3 127.0.0.1 "${PORT}" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') OpenD 起動確認: 127.0.0.1:${PORT} が待ち受け中。"
        exit 0
    fi
done

echo "$(date '+%Y-%m-%d %H:%M:%S') OpenD を起動したが ${PORT} が開かない。手動ログインが要るかもしれない。" >&2
exit 1
