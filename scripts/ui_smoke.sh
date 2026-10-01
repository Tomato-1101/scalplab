#!/usr/bin/env bash
# scalp-lab Web UI スモークハーネス。
#
# 追加依存なしで UI の「見にくい／壊れた」を機械的に検出する:
#   1. 空きポートでサーバを起動し、終了時に必ず kill（trap）。
#   2. curl で /api/sources・/api/chart?code=JP.7203・/api/screens が 200 かつ JSON 妥当か検証。
#   3. ヘッドレス Chrome で
#        - DOM を dump し必須文字列（エントリー条件 / SAR 方向フィルタ / トヨタ自動車 / スクリーニング）を assert
#        - コンソール出力に JS エラー（Uncaught / SyntaxError / TypeError / ReferenceError）が無いか grep
#        - スクリーンショットを logs/ui-smoke.png に保存し、サイズ > 50KB（真っ黒/白紙でない proxy）を assert
#   4. 結果を PASS/FAIL 一覧で出し、fail が 1 件でもあれば exit 1。
#
# 稼働中の本番サーバ（127.0.0.1:8025）には触れない（専用ポートで一時起動し自分の PID だけ kill）。
# 使い方: bash scripts/ui_smoke.sh   （ポート変更は SMOKE_PORT=xxxxx bash scripts/ui_smoke.sh）
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
PORT="${SMOKE_PORT:-18055}"
BASE="http://127.0.0.1:$PORT"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOGDIR="$ROOT/logs"
SHOT="$LOGDIR/ui-smoke.png"
TMP="$(mktemp -d)"
mkdir -p "$LOGDIR"

PASS=0
FAIL=0
RESULTS=()
ok() { RESULTS+=("PASS  $1"); PASS=$((PASS + 1)); }
ng() { RESULTS+=("FAIL  $1"); FAIL=$((FAIL + 1)); }

SERVER_PID=""
cleanup() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

summary() {
  echo
  echo "==== ui_smoke 結果 ===="
  for r in "${RESULTS[@]}"; do echo "  $r"; done
  echo "-----------------------"
  echo "  PASS=$PASS  FAIL=$FAIL"
  if [ "$FAIL" -eq 0 ]; then
    echo "RESULT: PASS"
    exit 0
  else
    echo "RESULT: FAIL"
    exit 1
  fi
}

# --- 1. サーバ起動（専用ポート） ---
"$PY" -m scalplab.webui --port "$PORT" >"$TMP/server.log" 2>&1 &
SERVER_PID=$!

up=0
for _ in $(seq 1 60); do
  if curl -sf -o /dev/null "$BASE/api/sources"; then up=1; break; fi
  kill -0 "$SERVER_PID" 2>/dev/null || break   # サーバが落ちていたら即中断
  sleep 0.25
done
if [ "$up" -ne 1 ]; then
  ng "server-start ($BASE で起動せず)"
  echo "--- server.log 末尾 ---"; tail -8 "$TMP/server.log" 2>/dev/null || true
  summary
fi
ok "server-start ($BASE)"

# --- 2. API の 200 + JSON 妥当性 ---
check_api() {
  local path="$1" name="$2" code
  code=$(curl -s -o "$TMP/body" -w '%{http_code}' "$BASE$path")
  if [ "$code" != "200" ]; then ng "$name (HTTP $code)"; return; fi
  if "$PY" -c "import json,sys; json.load(open(sys.argv[1]))" "$TMP/body" 2>/dev/null; then
    ok "$name (200, JSON妥当)"
  else
    ng "$name (200 だが JSON 不正)"
  fi
}
check_api "/api/sources" "api-sources"
check_api "/api/chart?code=JP.7203" "api-chart-JP.7203"
check_api "/api/screens" "api-screens"
check_api "/api/annotations?code=JP.7203" "api-annotations"   # 注釈一覧（GET・読み取りのみ）
check_api "/api/regime" "api-regime"                          # レジーム判定（順張り/逆張りの日）

# レジーム判定 JSON に pick が含まれるか（順張り/逆張りの判定が返っているか）
if curl -s "$BASE/api/regime" | grep -q '"pick"'; then
  ok "regime-has-pick (/api/regime に pick フィールド)"
else
  ng "regime-has-pick（/api/regime に pick が無い）"
fi

# チャート API にボリンジャーバンド系列（boll: basis/upper/lower）が含まれるか
BODY_CHART="$TMP/chart.json"
curl -s "$BASE/api/chart?code=JP.7203" -o "$BODY_CHART"
if grep -q '"boll"' "$BODY_CHART" && grep -q '"basis"' "$BODY_CHART" \
   && grep -q '"upper"' "$BODY_CHART" && grep -q '"lower"' "$BODY_CHART"; then
  ok "chart-boll-series (boll.basis/upper/lower を返す)"
else
  ng "chart-boll-series 欠落（boll / basis / upper / lower が /api/chart に無い）"
fi

# --- 3. ヘッドレス Chrome ---
if [ ! -x "$CHROME" ]; then
  ng "chrome-not-found ($CHROME が無い／実行不可)"
  summary
fi

# 3a. DOM dump + コンソールログ（JS 実行後の最終 DOM を取得）
DOM="$TMP/dom.html"
CLOG="$TMP/chrome.log"
"$CHROME" --headless=new --no-sandbox \
  --enable-logging=stderr --v=1 --virtual-time-budget=12000 \
  --dump-dom "$BASE/" >"$DOM" 2>"$CLOG" || true

missing=""
while IFS= read -r s; do
  grep -qF "$s" "$DOM" || missing="$missing 「$s」"
done <<'STRINGS'
エントリー条件
SAR 方向フィルタ
トヨタ自動車
スクリーニング
STRINGS
if [ -z "$missing" ]; then
  ok "dom-required-strings (4/4)"
else
  ng "dom-required-strings 欠落:$missing"
fi

# 3a-2. 右パネル内セクションの縦リサイズハンドル（条件/統計/一覧の高さ調整用グリップ）が存在するか
missing_grip=""
for g in resizeCond resizeStats; do
  grep -qF "id=\"$g\"" "$DOM" || missing_grip="$missing_grip $g"
done
if [ -z "$missing_grip" ]; then
  ok "sidebar-row-resizers (resizeCond/resizeStats 両方存在)"
else
  ng "sidebar-row-resizers 欠落:$missing_grip"
fi

# 3a-3. RCI 符号ゲートのトグル（要件 2026-07-18）がエントリー条件パネルに存在するか
if grep -qF 'id="rciSignOn"' "$DOM" && grep -qF 'RCI符号' "$DOM"; then
  ok "rci-sign-gate-toggle (rciSignOn チェックボックス＋ラベル存在)"
else
  ng "rci-sign-gate-toggle 欠落（rciSignOn / RCI符号 が DOM に無い）"
fi

# 3a-4. モード3択セグメント（要件 2026-07-21）＋トゲ深さ/戻り入力＋詳細設定折りたたみが存在するか
if grep -qF 'id="modeSeg"' "$DOM" && grep -qF 'data-mode="auto"' "$DOM" \
   && grep -qF 'data-mode="trend"' "$DOM" && grep -qF 'data-mode="revert"' "$DOM" \
   && grep -qF 'id="spikeDepthV"' "$DOM" && grep -qF 'id="spikeTurnV"' "$DOM" \
   && grep -qF 'id="detailToggle"' "$DOM" && grep -qF 'id="detailPanel"' "$DOM"; then
  ok "mode-segment (modeSeg auto/trend/revert＋トゲ深さ/戻り＋詳細設定折りたたみ 存在)"
else
  ng "mode-segment 欠落（modeSeg / data-mode / spikeDepthV / spikeTurnV / detailToggle / detailPanel が DOM に無い）"
fi

# 3a-4b. 第2トリガー「RCI同調」トグル＋符号ゲートのレベル入力（要件 2026-07-19・教師注釈由来）
if grep -qF 'id="dualOn"' "$DOM" && grep -qF 'id="dualV"' "$DOM" \
   && grep -qF 'RCI同調' "$DOM" && grep -qF 'id="rciSignLevelV"' "$DOM"; then
  ok "dual-rci-and-sign-level (dualOn/dualV/RCI同調＋rciSignLevelV 存在)"
else
  ng "dual-rci-and-sign-level 欠落（dualOn / dualV / RCI同調 / rciSignLevelV が DOM に無い）"
fi

# 3a-4c. SAR 方向フィルタのトグル化＋前場の寄り抑制入力（要件 2026-07-19・利益最大化マンデート）
if grep -qF 'id="sarOn"' "$DOM" && grep -qF 'id="skipOpenV"' "$DOM" \
   && grep -qF '寄り抑制' "$DOM"; then
  ok "sar-toggle-and-skip-open (sarOn トグル＋skipOpenV/寄り抑制 存在)"
else
  ng "sar-toggle-and-skip-open 欠落（sarOn / skipOpenV / 寄り抑制 が DOM に無い）"
fi

# 3a-4d. 第3トリガー「σフェード」（急変逆張り。要件 2026-07-20・トゲと補完）のトグル＋k入力
if grep -qF 'id="fadeOn"' "$DOM" && grep -qF 'id="fadeV"' "$DOM" \
   && grep -qF 'σフェード' "$DOM"; then
  ok "sigma-fade-toggle (fadeOn トグル＋fadeV/σフェード 存在)"
else
  ng "sigma-fade-toggle 欠落（fadeOn / fadeV / σフェード が DOM に無い）"
fi

# 3a-4e. ボリンジャー z 入力（逆張りの閾値。2026-07-21 でトグルはモード3択に統合＝簡易パラメータの z 入力を確認）
if grep -qF 'id="fadeBollV"' "$DOM" && grep -qF 'ボリンジャー z' "$DOM"; then
  ok "bollinger-z-input (fadeBollV＋『ボリンジャー z』ラベル存在)"
else
  ng "bollinger-z-input 欠落（fadeBollV / 『ボリンジャー z』 が DOM に無い）"
fi

# 3a-5. 日送りナビ（要件 2026-07-21・1営業日単位表示）の ◀▶・日ラベル・全期間ボタンが存在するか
if grep -qF 'id="dayNav"' "$DOM" && grep -qF 'id="dayPrev"' "$DOM" \
   && grep -qF 'id="dayNext"' "$DOM" && grep -qF 'id="dayLabel"' "$DOM" \
   && grep -qF 'id="dayAll"' "$DOM" && grep -qF 'id="dayRegimeBadge"' "$DOM"; then
  ok "day-nav-controls (dayNav/dayPrev/dayNext/dayLabel/dayAll/dayRegimeBadge 存在)"
else
  ng "day-nav-controls 欠落（dayNav / dayPrev / dayNext / dayLabel / dayAll / dayRegimeBadge が DOM に無い）"
fi

# 3a-6. エントリー注釈トグル（機能A 2026-07-19）がヘッダに存在するか。注釈は4択（注目すべき含む）。
if grep -qF 'id="annotateBtn"' "$DOM" && grep -qF 'id="annotateModal"' "$DOM" \
   && grep -qF 'id="annotateReason"' "$DOM" && grep -qF 'value="attention"' "$DOM"; then
  ok "annotate-toggle (annotateBtn＋追加ダイアログ＋注目すべき4択目 存在)"
else
  ng "annotate-toggle 欠落（annotateBtn / annotateModal / annotateReason / value=\"attention\" が DOM に無い）"
fi

# 3a-6b. 注目シグナル表示トグル（2026-07-19）が条件パネルに存在するか
if grep -qF 'id="attnShowOn"' "$DOM" && grep -qF '注目シグナル表示' "$DOM"; then
  ok "attention-toggle (attnShowOn＋ラベル存在)"
else
  ng "attention-toggle 欠落（attnShowOn / 注目シグナル表示 が DOM に無い）"
fi

# 3a-7. 勝ちやすい銘柄ランキングの「この条件で実行」ボタン＋期間入力（機能B 2026-07-19）が存在するか
if grep -qF 'id="screenRunBtn"' "$DOM" && grep -qF 'id="screenFrom"' "$DOM" \
   && grep -qF 'id="screenTo"' "$DOM"; then
  ok "screen-run-controls (screenRunBtn/screenFrom/screenTo 存在)"
else
  ng "screen-run-controls 欠落（screenRunBtn / screenFrom / screenTo が DOM に無い）"
fi

# 3a-8. 手法適性フィルタ「適性銘柄のみ表示」トグル（実装A 2026-07-20）が存在するか
if grep -qF 'id="aptOnly"' "$DOM" && grep -qF '適性銘柄のみ表示' "$DOM"; then
  ok "aptitude-filter (aptOnly＋ラベル存在)"
else
  ng "aptitude-filter 欠落（aptOnly / 適性銘柄のみ表示 が DOM に無い）"
fi

# 3a-9. エントリー履歴パネル（実装B 2026-07-20）の DOM が存在するか
if grep -qF 'id="historyPanel"' "$DOM" && grep -qF 'id="historyBody"' "$DOM" \
   && grep -qF 'id="historyClose"' "$DOM"; then
  ok "history-panel (historyPanel/historyBody/historyClose 存在)"
else
  ng "history-panel 欠落（historyPanel / historyBody / historyClose が DOM に無い）"
fi

# 3a-10. レジームバッジ（実装A 2026-07-20）がヘッダに存在し、判定文言が出ているか。
# ヘッダの #regimeBadge は JS が /api/regime 取得後に「順張りの日/逆張りの日」を書き込む。
if grep -qF 'id="regimeBadge"' "$DOM" \
   && { grep -qF '順張りの日' "$DOM" || grep -qF '逆張りの日' "$DOM"; }; then
  ok "regime-badge (regimeBadge＋順張り/逆張りの日 表示)"
else
  ng "regime-badge 欠落（regimeBadge が無い or 順張り/逆張りの日 文言が未描画）"
fi

# 3a-11. 表示日レジームバッジ（要件 2026-07-21）: 非最新の営業日を開くと日送り隣の #dayRegimeBadge に
# その日のレジーム（judge_regime(その日)）が「順張り/逆張りの日」で出る（最新日はヘッダバッジと統合＝非表示）。
NONLATEST=$(curl -s "$BASE/api/sources" | "$PY" -c "import json,sys; d=json.load(sys.stdin).get('bars1m_dates') or []; print(d[-2] if len(d)>=2 else '')")
if [ -n "$NONLATEST" ]; then
  DOM_DAY="$TMP/dom_day.html"
  "$CHROME" --headless=new --no-sandbox --virtual-time-budget=12000 \
    --dump-dom "$BASE/?code=JP.7203&date=$NONLATEST" >"$DOM_DAY" 2>>"$CLOG" || true
  if "$PY" - "$DOM_DAY" <<'PY' 2>/dev/null
import re, sys
html = open(sys.argv[1], encoding="utf-8", errors="replace").read()
m = re.search(r'id="dayRegimeBadge"[^>]*>(.*?)</button>', html, re.S)
assert m, "dayRegimeBadge not found"
inner = m.group(1)
assert ("順張りの日" in inner) or ("逆張りの日" in inner), "no regime text in dayRegimeBadge"
print("OK")
PY
  then
    ok "day-regime-badge (非最新日 $NONLATEST で表示日バッジに判定表示)"
  else
    ng "day-regime-badge（$NONLATEST で dayRegimeBadge に順張り/逆張りの日 が出ない）"
  fi
else
  ok "day-regime-badge (skip: bars1m 日付が2日未満)"
fi

# 3a-12. マーカー形状の凡例（要件 2026-07-21・トゲ順張り=矢印 / BB逆張り=丸を区別）が描画されるか。
# 凡例はチャート描画後に JS が入れるので、既定ロード（JP.7203）の DOM に「●=BB」が出ているかを見る。
if grep -qF '●=BB' "$DOM"; then
  ok "marker-legend (トゲ/BB マーカー形状の凡例が描画)"
else
  ng "marker-legend 欠落（凡例に『●=BB』が無い＝マーカー種別の区別が未描画）"
fi

# 3b. JS エラー検出（ページ由来のコンソールエラーだけを対象に token を grep）
if grep -E 'CONSOLE' "$CLOG" | grep -Eq 'Uncaught|SyntaxError|TypeError|ReferenceError'; then
  ng "js-errors 検出（下に該当行）"
  echo "--- JS エラー行 ---"
  grep -E 'CONSOLE' "$CLOG" | grep -E 'Uncaught|SyntaxError|TypeError|ReferenceError' | head -5
else
  ok "js-errors なし"
fi

# 3c. スクリーンショットのサイズ健全性（真っ黒/白紙でない proxy）
"$CHROME" --headless=new --no-sandbox \
  --hide-scrollbars --window-size=1600,1000 --virtual-time-budget=12000 \
  --screenshot="$SHOT" "$BASE/" >/dev/null 2>"$TMP/shot.log" || true
if [ -f "$SHOT" ]; then
  bytes=$(stat -f%z "$SHOT" 2>/dev/null || stat -c%s "$SHOT" 2>/dev/null || echo 0)
  if [ "${bytes:-0}" -gt 51200 ]; then
    ok "screenshot ($bytes bytes > 50KB → $SHOT)"
  else
    ng "screenshot 小さすぎ ($bytes bytes ≤ 50KB。真っ黒/白紙の疑い)"
  fi
else
  ng "screenshot 生成失敗"
fi

# --- 4. エントリー詳細インラインアコーディオン（?autoselect=1 で先頭を自動展開） ---
# 旧・最下部固定ペイン #detail を廃止し、クリックした行の直下に詳細を展開する改修の回帰防止。
# ヘッドレスはクリック注入ができないので ?autoselect=1 起動フラグで先頭エントリーを自動展開させ、
#   (a) 必須文字列（エントリー詳細 / 直前RCI差）が entriesList の中に出ていること、
#   (b) 旧 #detail 固定ペインが DOM に存在しないこと、
#   (c) その表示のスクリーンショットが健全（>50KB）なこと、
# を機械的に検証する（詳細が「画面外で見えない」回帰を捕まえる）。
DOM2="$TMP/dom_autoselect.html"
"$CHROME" --headless=new --no-sandbox \
  --virtual-time-budget=12000 \
  --dump-dom "$BASE/?autoselect=1" >"$DOM2" 2>>"$CLOG" || true

if "$PY" - "$DOM2" <<'PY' 2>"$TMP/acc.log"
import re, sys
html = open(sys.argv[1], encoding="utf-8", errors="replace").read()
# 旧・最下部固定ペインが無いこと（改修で廃止済み）
assert 'id="detail"' not in html, "legacy #detail pane still present"
# entriesList の内側だけを切り出す（入れ子 ul は無いので最初の </ul> で閉じる）
m = re.search(r'id="entriesList".*?>(.*?)</ul>', html, re.S)
assert m, "entriesList not found in DOM"
inner = m.group(1)
# 展開ノードが entriesList の子として入っていること
assert 'class="entry-detail"' in inner, "entry-detail accordion not inside entriesList"
for s in ("エントリー詳細", "直前RCI差"):
    assert s in inner, f"required string not inside entriesList: {s}"
print("OK")
PY
then
  ok "autoselect-accordion (詳細が entriesList 内に展開・旧#detail無し)"
else
  ng "autoselect-accordion (詳細が entriesList 内に展開されない/旧#detail残存)"
  echo "--- accordion check ---"; cat "$TMP/acc.log" 2>/dev/null || true
fi

# 4b. 展開ビューのスクリーンショット健全性（アコーディオン表示が真っ黒/白紙でない proxy）
SHOT2="$LOGDIR/ui-smoke-autoselect.png"
"$CHROME" --headless=new --no-sandbox \
  --hide-scrollbars --window-size=1600,1000 --virtual-time-budget=12000 \
  --screenshot="$SHOT2" "$BASE/?autoselect=1" >/dev/null 2>>"$TMP/shot.log" || true
if [ -f "$SHOT2" ]; then
  bytes2=$(stat -f%z "$SHOT2" 2>/dev/null || stat -c%s "$SHOT2" 2>/dev/null || echo 0)
  if [ "${bytes2:-0}" -gt 51200 ]; then
    ok "screenshot-autoselect ($bytes2 bytes > 50KB → $SHOT2)"
  else
    ng "screenshot-autoselect 小さすぎ ($bytes2 bytes ≤ 50KB。真っ黒/白紙の疑い)"
  fi
else
  ng "screenshot-autoselect 生成失敗"
fi

summary
