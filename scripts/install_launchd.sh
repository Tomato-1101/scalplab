#!/usr/bin/env bash
# scalp-lab ティック録画の launchd 常駐をインストールする（日本株 + 米国株の2つ）。
#
# テンプレート中の __ABSOLUTE_PATH__ をこのリポジトリの実パスに置換して
# ~/Library/LaunchAgents/ に生成し、launchctl でロードする。
# （リポジトリに絶対パスをコミットしない設計。実パスはインストール時に解決する）
#
#  - com.scalplab.recorder      : 東証セッション（平日 JST 8:50 起動）
#  - com.scalplab.recorder.us   : 米国東部セッション（平日 JST 22:20 起動）
#  - com.scalplab.opend         : OpenD の死活監視（ログイン時 + 5分毎）
#  - com.scalplab.logs-gc       : ログの自動掃除（毎日 06:00）
#
# 使い方: bash scripts/install_launchd.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST_DIR="${HOME}/Library/LaunchAgents"

# インストール対象のラベル一覧。テンプレートは "<LABEL>.plist.template" 命名。
LABELS=(
    "com.scalplab.recorder"
    "com.scalplab.recorder.us"
    "com.scalplab.opend"
    "com.scalplab.logs-gc"
)

if [[ ! -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
    echo "警告: ${PROJECT_ROOT}/.venv/bin/python が無い。venv を先に作成すること。" >&2
fi

mkdir -p "${DEST_DIR}"
mkdir -p "${PROJECT_ROOT}/logs"

install_one() {
    local label="$1"
    local template="${SCRIPT_DIR}/${label}.plist.template"
    local dest="${DEST_DIR}/${label}.plist"

    if [[ ! -f "${template}" ]]; then
        echo "テンプレートが見つからない: ${template}" >&2
        return 1
    fi

    # __ABSOLUTE_PATH__ を実パスに置換（区切りに | を使い / の衝突を避ける）
    sed "s|__ABSOLUTE_PATH__|${PROJECT_ROOT}|g" "${template}" > "${dest}"
    echo "生成: ${dest}"

    # 既存を unload してから load（冪等）。未ロードでも unload の失敗は無視してよい
    launchctl unload "${dest}" 2>/dev/null || true
    launchctl load "${dest}"
    echo "launchctl load 完了: ${label}"
}

for label in "${LABELS[@]}"; do
    install_one "${label}"
done

echo
echo "確認:            launchctl list | grep com.scalplab.recorder"
echo "手動起動テスト:  launchctl start com.scalplab.recorder.us"
echo "アンインストール: launchctl unload ${DEST_DIR}/<label>.plist && rm ${DEST_DIR}/<label>.plist"
