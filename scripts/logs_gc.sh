#!/usr/bin/env bash
# 増え続けて誰も消さないログの自動掃除。録画データ（data/）には一切触らない。
#
# 対象と理由:
#  - ~/.com.moomoo.OpenD/Log     : OpenD 本体のデバッグログ。7MB 単位でローテートされるが
#                                  自動削除されず、録画中は日 50〜80MB 積み上がる（5-17〜7-30 で 716MB）。
#  - ~/.com.futunn.FutuOpenD/Log : futu SDK のログ。debug_model=False（scalplab/__init__.py）で
#                                  ほぼ出なくなったが、SDK 更新で既定が戻る可能性があるので保険で対象に残す。
#  - logs/recorder-*.log         : 日次の録画ログ（正常時 1 日 326KB）。
#  - logs/launchd-*.log          : launchd が append し続けるだけでローテートが無い。肥大時のみ truncate
#                                  （プロセスが開いたままなので rm せず中身を空にする）。
#
# launchd（com.scalplab.logs-gc）から毎日 06:00 に呼ばれる。単体実行: bash scripts/logs_gc.sh
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPEND_LOG="${HOME}/.com.moomoo.OpenD/Log"
FUTU_LOG="${HOME}/.com.futunn.FutuOpenD/Log"

KEEP_DAYS=7                            # OpenD / futu SDK のログ（障害調査に要るのは直近だけ）
KEEP_DAYS_RECORDER=60                  # scalp-lab の日次録画ログ
TRUNCATE_BYTES=$((20 * 1024 * 1024))   # launchd ログをこれを超えたら空にする

used_kb() { du -sk "$@" 2>/dev/null | awk '{s+=$1} END {print s+0}'; }

TARGETS=("${OPEND_LOG}" "${FUTU_LOG}" "${PROJECT_ROOT}/logs")
before=$(used_kb "${TARGETS[@]}")

find "${OPEND_LOG}" -type f -mtime "+${KEEP_DAYS}" -delete 2>/dev/null
find "${FUTU_LOG}" -type f -mtime "+${KEEP_DAYS}" -delete 2>/dev/null
find "${PROJECT_ROOT}/logs" -type f -name 'recorder-*.log' -mtime "+${KEEP_DAYS_RECORDER}" -delete 2>/dev/null

for f in "${PROJECT_ROOT}"/logs/launchd-*.log; do
    [[ -f "${f}" ]] || continue
    size=$(stat -f '%z' "${f}" 2>/dev/null || echo 0)
    if (( size > TRUNCATE_BYTES )); then
        : > "${f}"
        echo "$(date '+%Y-%m-%d %H:%M:%S') truncate $(basename "${f}") ($((size / 1048576))MB)"
    fi
done

after=$(used_kb "${TARGETS[@]}")
freed=$(( (before - after) / 1024 ))
if (( freed > 0 )); then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ログ掃除: ${freed}MB 解放（残り $((after / 1024))MB）"
fi
