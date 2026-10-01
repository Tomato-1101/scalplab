/* scalp-lab エントリー確認 — フロント制御。
 *
 * 方針（要件・2026-07-16 全面改修）:
 *  - タブは「チャート」「スクリーニング」の 2 つだけ。日本株/米国株/ティックのソース区別は見せない。
 *  - 銘柄は左サイドバーの常設リストからクリックで選ぶ（サーバが code だけでソースを自動解決）。
 *  - 日本株1分足(bars1m)はヘッダの期間指定(from/to)で表示範囲を選ぶ。既定は全期間（days=0 で全保存日を
 *    一括ロードし、チャート・エントリー・統計をその期間で計算）。期間指定時はページングを使わない。
 *  - 米国株の過去1分足(hist1m)は従来どおり最新から表示し、左スクロールで過去を追加読み込みする。
 *    ティック(ticks)は足種セレクタのみで、期間指定の影響を受けない。
 *  - エントリー条件（角度・直前RCI差・効率下限）は各行で個別にオン/オフ。OFF は閾値 0 を送る。
 *  - リロードしても前回の状態（タブ・銘柄・足種・各条件）を localStorage から完全復元する。
 *
 * lightweight-charts(v4) は 1 チャート=1 ペインなので、ローソク＋SMA＋SAR の「メイン」と
 * RCI 専用の「下段」の 2 チャートを作り、時間軸とクロスヘアを同期する。指標値はサーバが
 * 計算済みの座標で渡すので、ここは描画と操作だけを担う。
 */
'use strict';

const LWC = window.LightweightCharts;

const COLORS = {
  bg: '#0e1116', panel: '#161b22', text: '#c9d1d9', muted: '#7d8896',
  grid: '#1c2129', border: '#2a3038',
  up: '#26a69a', down: '#ef5350', long: '#26c281', short: '#ef5350',
  sar: '#b07be0', rci9: '#4c9aff', rci14: '#f5a742',
  sma: ['#f5c542', '#4c9aff'],   // SMA5 / SMA25 の 2 本固定
  ann: '#ffd24a', annNo: '#8b93a7',   // 注釈マーカー（金＝エントリー推奨 / 灰＝入っちゃダメ。シグナルと区別）
  attention: '#ffe066',               // 注目（事前アラート）マーカー: 黄・四角・「!」でシグナル/注釈と区別
  bollBasis: '#6f8299',               // ボリンジャー中心線（点線・控えめ）
  bollBand: 'rgba(120,150,190,0.5)',  // 上下バンド（半透明）
};

// 初期（localStorage 無し時）の既定条件。日本株チャンピオン（2026-07-19 walkforward で選定）に合わせる:
//   トゲ反転 ON（深さ80 / 戻り25）を主トリガーにし、V 底とは両立しない角度・直前RCI差・RCI符号は OFF。
//   効率 OFF / クールダウン 30 / コスト 3bp。spike は値2つ（depth/turn）を持つ ON/OFF。
//   rciSign は値を持たない ON/OFF（＋はショートのみ・−はロングのみ）。
//   旧既定（深100/戻40/クールダウン60）を保存済みのユーザーだけ loadState で一度きり新既定へ引き上げる。
const DEFAULT_COND = {
  angle: { on: false, v: 30 },
  prevDiff: { on: false, v: 25 },
  rciSign: { on: false, level: 0 },   // level=符号ゲート境界 ±（0=従来 0 境界）。教師注釈の高値圏ショート対応
  dual: { on: false, v: 15 },         // 第2トリガー RCI 同調（短期9・長期14 の同時トレンド）。既定 OFF
  spike: { on: true, depth: 80, turn: 25 },
  fade: { on: false, v: 3.0 },        // 第3トリガー σフェード（急変逆張り）。k=σの倍率。既定 OFF・k=3.0
  fadeBoll: { on: false, v: 3.0 },    // 第4トリガー ボリンジャーフェード（逆張り）。z=|(close-SMA20)/std20|。既定 OFF・z=3.0
  eff: { on: false, v: 3 },
  sar: { on: true },   // SAR 方向フィルタ。既定 ON（従来どおり）。OFF で SAR で方向を絞らない（バー版のみ効く）
  skipOpen: 0,         // 前場の寄り N 本を抑制。0=無効（従来）。バー版のみ効く
  cooldown: 30,
  cost: 3,
};
// チャンピオン既定の一度きり移行番号（saveState が localStorage に書き、次回以降は移行を打ち切る）。
const COND_MIGRATION = 1;

const LS_KEY = 'scalplab.ui.v2';
const INITIAL_VIEW_BARS = 180;   // 初期表示は最新側 N 本だけ（残りは左スクロールで遡る）
const PAGE_EDGE_BARS = 30;       // 可視左端がデータ先頭からこの本数以内に近づいたら次ページ取得

// --- ペイン幅・高さ（ドラッグリサイズ。localStorage に保存して復元） ---
// left=左サイドバー幅 / right=右パネル幅 / rci=RCI ペイン高さ(px, null は CSS 既定=メイン3:RCI1)。
// condH=右パネルのエントリー条件ブロック高さ / statsH=統計ブロック高さ（px, null は自然高さ＝内容が
// 収まる高さ。余りはエントリー一覧が全部吸収するので、既定では一覧が最も広く取れる）。
const DEFAULT_LAYOUT = { left: 220, right: 380, rci: null, condH: null, statsH: null };
const MIN_LEFT = 140, MIN_RIGHT = 280, MIN_CHART = 400, GUTTER = 6;  // 幅の最小ガード
const MIN_RCI = 80, MIN_MAIN = 160;                                  // チャート/RCI 高さの最小ガード
const MIN_COND = 96, MIN_STATS = 84, MIN_ENTRIES = 120, SIDE_HEAD_H = 44;  // 右パネル縦リサイズの最小ガード

const state = {
  sources: null,
  tab: 'chart',
  code: null,
  tf: 10,
  // bars1m の表示は「1営業日単位」（2026-07-21）。viewAll=true で全期間（連結）表示に切替。
  // viewDate=null は「最新営業日」を意味し、sources 取得後に最新日へ解決する。ticks/hist1m は無視。
  viewDate: null, viewAll: false,
  cond: JSON.parse(JSON.stringify(DEFAULT_COND)),
  layout: JSON.parse(JSON.stringify(DEFAULT_LAYOUT)),

  chartMain: null, chartRci: null,
  candle: null, sar: null, smaSeries: [],
  rci9: null, rci14: null,
  bollBasis: null, bollUpper: null, bollLower: null,
  bollShow: true,                   // バンド表示の可否（既定 ON・2026-07-21。凡例クリックでトグル・localStorage 保存）
  selPriceLine: null,               // 選択中エントリーの価格ライン（どこで入ったか強調）
  syncing: false,

  // エントリーモード（2026-07-21）: 'auto'（表示日のレジームに連動）/'trend'（トゲ固定）/'revert'（BB固定）。
  mode: 'auto',
  detailOpen: false,                // 詳細設定の開閉（既定 閉）
  regimeCache: {},                  // date(または'today')→レジーム判定。auto モードとバッジで共有
  lastPrimary: null,                // 直近の実効トリガー（'spike'|'boll'）。変化時だけ SAR を同期するため

  regime: null,                     // 今日のレジーム判定 {pick, trend_bp, revert_bp, dates, n_symbols}
  dayRegime: null,                  // 表示中の日のレジーム判定（日送りナビ隣のバッジ用）

  meta: null,
  data: null,                       // {candles, sma:{p:[]}, rci9, rci14, sar, boll:{basis,upper,lower}}
  entries: [], entryByTime: new Map(), selectedIdx: -1,
  autoselect: false,                // ?autoselect=1（ハーネス用: 読込後に先頭を自動展開）
  pendingJump: null,                // 履歴パネルからのジャンプ先 epoch（次の描画後に selectByTime する）

  attention: [],                    // 注目（事前アラート）シグナル（bars1m のみ）
  showAttention: true,              // 注目マーカーの表示 ON/OFF（既定 ON・表示のみ。サーバへは送らない）

  annotate: false,                  // 注釈モード（ON でチャートクリックが注釈追加になる）
  annotations: [],                  // 現在銘柄の保存済み注釈（マーカー表示・クリック閲覧に使う）
  pendingBarTime: null,             // 追加ダイアログが対象にしているバーの epoch

  loadingMore: false, noMore: false, pagingArmed: false, reqSeq: 0,

  screen: { runs: [], rows: [], sort: { key: 'n_signals', dir: 'desc' }, from: null, to: null, aptOnly: false },
  history: { code: null, data: null },
};

const $ = (id) => document.getElementById(id);

// --- 時刻整形（epoch は「市場ローカルを UTC とみなした秒」なので UTC ゲッタで復元） ---
function fmtTime(sec, withSec) {
  const d = new Date(sec * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}` + (withSec ? `:${p(d.getUTCSeconds())}` : '');
}
function fmtDate(sec) {
  const d = new Date(sec * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
}

// --- localStorage への保存・復元 ---
function saveState() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({
      tab: state.tab, code: state.code, tf: state.tf,
      viewDate: state.viewDate, viewAll: state.viewAll,
      mode: state.mode, detailOpen: state.detailOpen,
      cond: state.cond, layout: state.layout, condMigr: COND_MIGRATION,
      showAttention: state.showAttention, bollShow: state.bollShow,
    }));
  } catch (e) { /* プライベートモード等では保存不可でも動作は継続 */ }
}
function loadState() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return;
    const s = JSON.parse(raw);
    if (s.tab === 'chart' || s.tab === 'screen') state.tab = s.tab;
    if (typeof s.code === 'string') state.code = s.code;
    if ([5, 10, 30, 60].includes(Number(s.tf))) state.tf = Number(s.tf);
    if (typeof s.showAttention === 'boolean') state.showAttention = s.showAttention;
    // bollShow は新キー。旧保存値（bollShow 無し）は既定 true のまま＝一度きりの「表示 ON」移行。
    // ユーザーが凡例で消すと bollShow:false が保存され、以後それを尊重する。
    if (typeof s.bollShow === 'boolean') state.bollShow = s.bollShow;
    // mode/detailOpen は新キー（2026-07-21）。旧保存値（mode 無し）は既定 'auto'＝一度きりの自動モード移行
    // （詳細設定内の各値は cond としてそのまま温存される）。
    if (s.mode === 'auto' || s.mode === 'trend' || s.mode === 'revert') state.mode = s.mode;
    if (typeof s.detailOpen === 'boolean') state.detailOpen = s.detailOpen;
    // 表示日/全期間フラグ（2026-07-21 の日送りナビ。旧 period キーは廃止＝無ければ既定＝最新1日）。
    const isDate = (v) => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v);
    if (isDate(s.viewDate)) state.viewDate = s.viewDate;
    if (typeof s.viewAll === 'boolean') state.viewAll = s.viewAll;
    // トゲ反転導入前の保存値（spike キーなし）は条件ブロックだけ新既定（トゲ単独モード）へ移行する。
    // 旧トグル状態とマージすると全条件 AND で発火ほぼ 0 の壊れた画面になるため。
    if (s.cond && typeof s.cond === 'object' && s.cond.spike) {
      const c = state.cond;
      const num = (v, d) => (Number.isFinite(Number(v)) ? Number(v) : d);
      if (s.cond.angle) c.angle = { on: !!s.cond.angle.on, v: num(s.cond.angle.v, DEFAULT_COND.angle.v) };
      if (s.cond.prevDiff) c.prevDiff = { on: !!s.cond.prevDiff.on, v: num(s.cond.prevDiff.v, DEFAULT_COND.prevDiff.v) };
      if (s.cond.rciSign) c.rciSign = { on: !!s.cond.rciSign.on, level: num(s.cond.rciSign.level, DEFAULT_COND.rciSign.level) };
      if (s.cond.dual) c.dual = { on: !!s.cond.dual.on, v: num(s.cond.dual.v, DEFAULT_COND.dual.v) };
      if (s.cond.spike) c.spike = { on: !!s.cond.spike.on, depth: num(s.cond.spike.depth, DEFAULT_COND.spike.depth), turn: num(s.cond.spike.turn, DEFAULT_COND.spike.turn) };
      if (s.cond.fade) c.fade = { on: !!s.cond.fade.on, v: num(s.cond.fade.v, DEFAULT_COND.fade.v) };  // σフェードは後発追加。無ければ DEFAULT（OFF・k=3.0）
      if (s.cond.fadeBoll) c.fadeBoll = { on: !!s.cond.fadeBoll.on, v: num(s.cond.fadeBoll.v, DEFAULT_COND.fadeBoll.v) };  // ボリンジャーフェードも後発追加
      if (s.cond.eff) c.eff = { on: !!s.cond.eff.on, v: num(s.cond.eff.v, DEFAULT_COND.eff.v) };
      // sar/skipOpen は後発追加。旧保存値に無ければ DEFAULT（SAR ON・寄り抑制 0＝従来）へフォールバック。
      if (s.cond.sar) c.sar = { on: !!s.cond.sar.on };
      c.skipOpen = num(s.cond.skipOpen, DEFAULT_COND.skipOpen);
      c.cooldown = num(s.cond.cooldown, DEFAULT_COND.cooldown);
      c.cost = num(s.cond.cost, DEFAULT_COND.cost);
      // 一度きりのチャンピオン移行（2026-07-19）: 旧既定のトゲ深100/戻40・クールダウン60 を
      // そのまま使っているユーザーだけ、新チャンピオン既定（深80/戻25・クールダウン30）へ引き上げる。
      // 自分で変えた値は尊重する。s.condMigr が付いた保存値（移行済み）は二度と触らない。
      if (!s.condMigr) {
        if (c.spike.depth === 100 && c.spike.turn === 40) {
          c.spike.depth = DEFAULT_COND.spike.depth;
          c.spike.turn = DEFAULT_COND.spike.turn;
        }
        if (c.cooldown === 60) c.cooldown = DEFAULT_COND.cooldown;
      }
    }
    if (s.layout && typeof s.layout === 'object') {
      const L = state.layout;
      const num = (v, d) => (Number.isFinite(Number(v)) ? Number(v) : d);
      L.left = num(s.layout.left, DEFAULT_LAYOUT.left);
      L.right = num(s.layout.right, DEFAULT_LAYOUT.right);
      L.rci = (s.layout.rci == null ? null : num(s.layout.rci, null));
      L.condH = (s.layout.condH == null ? null : num(s.layout.condH, null));
      L.statsH = (s.layout.statsH == null ? null : num(s.layout.statsH, null));
    }
  } catch (e) {
    // 壊れた保存値は捨てて既定にフォールバック
    state.cond = JSON.parse(JSON.stringify(DEFAULT_COND));
    state.layout = JSON.parse(JSON.stringify(DEFAULT_LAYOUT));
  }
}

// --- チャート初期化（1 回だけ） ---
function makeChart(el, opts) {
  return LWC.createChart(el, Object.assign({
    autoSize: true,
    layout: { background: { color: COLORS.bg }, textColor: COLORS.text, fontSize: 11 },
    grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
    rightPriceScale: { borderColor: COLORS.border, minimumWidth: 76 },
    timeScale: { borderColor: COLORS.border, timeVisible: true, secondsVisible: false, rightOffset: 4 },
    crosshair: { mode: LWC.CrosshairMode.Normal },
  }, opts));
}

function initCharts() {
  state.chartMain = makeChart($('mainChart'), {});
  state.chartRci = makeChart($('rciChart'), {
    timeScale: { borderColor: COLORS.border, timeVisible: true, secondsVisible: false, visible: false },
  });

  state.candle = state.chartMain.addCandlestickSeries({
    upColor: COLORS.up, downColor: COLORS.down, wickUpColor: COLORS.up, wickDownColor: COLORS.down,
    borderVisible: false,
  });
  state.sar = state.chartMain.addLineSeries({
    color: COLORS.sar, lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 1.6,
    lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
  });
  // ボリンジャーバンド（中心=点線・上下=半透明の細線）。SMA は後から addLineSeries するので上に載る。
  const bollLineOpts = { lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false };
  state.bollUpper = state.chartMain.addLineSeries(Object.assign({ color: COLORS.bollBand }, bollLineOpts));
  state.bollLower = state.chartMain.addLineSeries(Object.assign({ color: COLORS.bollBand }, bollLineOpts));
  state.bollBasis = state.chartMain.addLineSeries(Object.assign({ color: COLORS.bollBasis, lineStyle: LWC.LineStyle.Dotted }, bollLineOpts));

  state.rci9 = state.chartRci.addLineSeries({
    color: COLORS.rci9, lineWidth: 1, lastValueVisible: false, priceLineVisible: false,
    autoscaleInfoProvider: () => ({ priceRange: { minValue: -100, maxValue: 100 } }),
  });
  state.rci14 = state.chartRci.addLineSeries({
    color: COLORS.rci14, lineWidth: 1, lastValueVisible: false, priceLineVisible: false,
  });
  for (const [price, color, dashed] of [[80, '#57606a', true], [0, '#3a424c', false], [-80, '#57606a', true]]) {
    state.rci9.createPriceLine({
      price, color, lineWidth: 1, lineStyle: dashed ? LWC.LineStyle.Dashed : LWC.LineStyle.Solid,
      axisLabelVisible: true, title: String(price),
    });
  }

  // 時間軸を双方向同期（再入ガード付き）。
  const syncFrom = (dst) => (range) => {
    if (state.syncing || !range) return;
    state.syncing = true;
    dst.timeScale().setVisibleLogicalRange(range);
    state.syncing = false;
  };
  state.chartMain.timeScale().subscribeVisibleLogicalRangeChange(syncFrom(state.chartRci));
  state.chartRci.timeScale().subscribeVisibleLogicalRangeChange(syncFrom(state.chartMain));

  // 過去方向の遅延読み込み: 可視左端がデータ先頭に近づいたら次ページを取得。
  state.chartMain.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    if (!range || !state.pagingArmed || state.loadingMore || state.noMore) return;
    if (range.from < PAGE_EDGE_BARS) loadMoreHistory();
  });

  state.chartMain.subscribeCrosshairMove(onCrosshair);
  state.chartMain.subscribeClick((p) => {
    if (!p || p.time == null) return;
    // 注釈モード中はクリックで注釈追加ダイアログ。通常時は注釈マーカーなら閲覧、無ければエントリー選択。
    if (state.annotate) { openAnnotateDialog(p.time); return; }
    const ann = annotationAtTime(p.time);
    if (ann) { openAnnotateView(ann); return; }
    selectByTime(p.time);
  });
}

// --- ペインのドラッグリサイズ ---
// 幅/高さは flex-basis で当て、lightweight-charts は autoSize（内部 ResizeObserver）が
// コンテナのサイズ変化を検知して自動リサイズするので、ここで chart.resize は呼ばない。
function applyLayout() {
  const L = state.layout;
  $('symbolList').style.flexBasis = L.left + 'px';
  $('sidebar').style.flexBasis = L.right + 'px';
  // rci=null は CSS 既定（メイン flex:3 / RCI flex:1）に戻す。px 指定時は固定高さにしてメインが残りを取る。
  $('rciChart').style.flex = (L.rci == null) ? '' : ('0 0 ' + L.rci + 'px');
  // 右パネル内: 条件・統計は px 指定なら固定高さ（内部スクロール）、null なら自然高さ。
  // どちらの場合も余りはエントリー一覧（flex:1）が吸収する。
  $('conditions').style.flex = (L.condH == null) ? '' : ('0 0 ' + L.condH + 'px');
  $('statsCard').style.flex = (L.statsH == null) ? '' : ('0 0 ' + L.statsH + 'px');
}

function workspaceInnerWidth() { return $('workspace').clientWidth; }
function clampLeft(px) {
  const maxLeft = workspaceInnerWidth() - state.layout.right - MIN_CHART - 2 * GUTTER;
  return Math.max(MIN_LEFT, Math.min(px, Math.max(MIN_LEFT, maxLeft)));
}
function clampRight(px) {
  const maxRight = workspaceInnerWidth() - state.layout.left - MIN_CHART - 2 * GUTTER;
  return Math.max(MIN_RIGHT, Math.min(px, Math.max(MIN_RIGHT, maxRight)));
}

// 右パネル内セクション（条件/統計）の高さクランプ。掴んだセクションが潰れず、かつ下の
// セクション（統計・一覧）の最小高さも確保できる範囲に収める。
function sidebarInnerHeight() { return $('sidebar').clientHeight; }
function clampCondHeight(px) {
  const H = sidebarInnerHeight() || 800;
  const max = Math.max(MIN_COND, H - MIN_STATS - MIN_ENTRIES - SIDE_HEAD_H - 2 * GUTTER);
  return Math.max(MIN_COND, Math.min(px, max));
}
function clampStatsHeight(px) {
  const H = sidebarInnerHeight() || 800;
  // 条件が px 固定ならその高さ、null（自然高さ）なら実測 offsetHeight を差し引いて一覧の最小を確保。
  const condH = (state.layout.condH != null) ? state.layout.condH : ($('conditions').offsetHeight || 0);
  const max = Math.max(MIN_STATS, H - condH - MIN_ENTRIES - SIDE_HEAD_H - 2 * GUTTER);
  return Math.max(MIN_STATS, Math.min(px, max));
}

// 復元値・ウィンドウ変化に対して現在サイズで再クランプ（潰れ防止）してから反映。
function reclampLayout() {
  const L = state.layout;
  L.left = clampLeft(L.left);
  L.right = clampRight(L.right);
  if (L.rci != null) {
    const h = $('chartArea').clientHeight;
    if (h > 0) L.rci = Math.max(MIN_RCI, Math.min(L.rci, Math.max(MIN_RCI, h - MIN_MAIN - GUTTER)));
  }
  if (L.condH != null) L.condH = clampCondHeight(L.condH);
  if (L.statsH != null) L.statsH = clampStatsHeight(L.statsH);
  applyLayout();
}

// 縦グリップ（列幅）。which='left' は左サイドバー、'right' は右パネルの幅を動かす。
function setupColDrag(id, which) {
  const el = $(id);
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    el.classList.add('dragging');
    const onMove = (ev) => {
      const rect = $('workspace').getBoundingClientRect();
      if (which === 'left') state.layout.left = clampLeft(ev.clientX - rect.left);
      else state.layout.right = clampRight(rect.right - ev.clientX);
      applyLayout();
    };
    const onUp = () => {
      el.classList.remove('dragging');
      el.removeEventListener('pointermove', onMove);
      el.removeEventListener('pointerup', onUp);
      el.removeEventListener('pointercancel', onUp);
      saveState();
    };
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
    el.addEventListener('pointercancel', onUp);
  });
}

// 横グリップ（メイン｜RCI の高さ）。下端からの距離で RCI ペイン高さを決める。
function setupRowDrag() {
  const el = $('resizeRci');
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    el.classList.add('dragging');
    const onMove = (ev) => {
      const rect = $('chartArea').getBoundingClientRect();
      const maxRci = rect.height - MIN_MAIN - GUTTER;
      let rci = rect.bottom - ev.clientY;
      rci = Math.max(MIN_RCI, Math.min(rci, Math.max(MIN_RCI, maxRci)));
      state.layout.rci = rci;
      applyLayout();
    };
    const onUp = () => {
      el.classList.remove('dragging');
      el.removeEventListener('pointermove', onMove);
      el.removeEventListener('pointerup', onUp);
      el.removeEventListener('pointercancel', onUp);
      saveState();
    };
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
    el.addEventListener('pointercancel', onUp);
  });
}

// 右パネル内セクション（エントリー条件・統計）の縦リサイズ。掴んだグリップの位置までを
// 対象セクションの高さにする（横グリップと同じ操作感）。余りはエントリー一覧が吸収する。
function setupSectionRowDrag(gripId, targetId, key, clampFn) {
  const el = $(gripId);
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    el.classList.add('dragging');
    const onMove = (ev) => {
      const top = $(targetId).getBoundingClientRect().top;
      state.layout[key] = clampFn(ev.clientY - top);
      applyLayout();
    };
    const onUp = () => {
      el.classList.remove('dragging');
      el.removeEventListener('pointermove', onMove);
      el.removeEventListener('pointerup', onUp);
      el.removeEventListener('pointercancel', onUp);
      saveState();
    };
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
    el.addEventListener('pointercancel', onUp);
  });
}

function withSecondsAxis() {
  return state.meta && state.meta.resolved_source === 'ticks' && state.meta.tf < 60;
}

// クロスヘア追従の OHLC 浮きボックスは撤去（要望 2026-07-21「この表示はいらない」）。
// メインと RCI ペインのクロスヘア縦位置同期だけ残す。
function onCrosshair(param) {
  if (!param || param.time == null || !param.seriesData || !param.seriesData.get(state.candle)) {
    state.chartRci.clearCrosshairPosition();
    return;
  }
  state.chartRci.setCrosshairPosition(0, param.time, state.rci9);
}

// --- API ---
async function api(path) {
  const res = await fetch(path);
  let body;
  try { body = await res.json(); } catch (e) { throw new Error(`応答の解析に失敗しました (HTTP ${res.status})`); }
  if (!res.ok) throw new Error(body && body.error ? body.error : `HTTP ${res.status}`);
  return body;
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  let data;
  try { data = await res.json(); } catch (e) { throw new Error(`応答の解析に失敗しました (HTTP ${res.status})`); }
  if (!res.ok) throw new Error(data && data.error ? data.error : `HTTP ${res.status}`);
  return data;
}

// --- 銘柄リスト（左サイドバー） ---
function jpName(code) {
  const names = (state.sources && state.sources.bars1m_names) || {};
  return names[code] || null;
}
function symbolMain(code) {
  if (code.startsWith('JP.')) return jpName(code) || code;
  return code.replace(/^US\./, '');   // 米国はティッカー表示
}
function symbolSub(code) {
  if (code.startsWith('JP.')) return code.replace(/^JP\./, '');
  return '';
}

function renderCurrentSymbol() {
  const code = state.code;
  if (!code) { $('currentSymbol').innerHTML = ''; return; }
  const main = symbolMain(code);
  const sub = code;
  $('currentSymbol').innerHTML = `<b>${escapeHtml(main)}</b> <small>${escapeHtml(sub)}</small>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m]));
}

// --- レジームバッジ（今日は順張りの日／逆張りの日） ---
// ヘッダのバッジは「今日（最新営業日）」のレジーム。過去日を表示中は日送りナビ隣の
// 「表示日」バッジがその日のレジームを出すので、ヘッダの今日バッジは隠して二重表示・
// （今日と表示日でレジームが違う場合の）矛盾を防ぐ。最新日・全期間・非 bars1m では今日バッジを出す。
function headerBadgeSuppressed() {
  const m = state.meta || {};
  if (m.resolved_source !== 'bars1m') return false;
  if (state.viewAll) return false;
  const cur = currentViewDate();
  return !!(cur && cur !== latestBars1mDate());
}
function renderRegimeBadge() {
  const el = $('regimeBadge');
  if (!el) return;
  const r = state.regime;
  if (!r || !r.pick || headerBadgeSuppressed()) { el.hidden = true; return; }
  const trend = r.pick === 'trend';
  el.hidden = false;
  el.classList.toggle('regime-trend', trend);
  el.classList.toggle('regime-revert', !trend);
  el.textContent = trend ? '順張りの日' : '逆張りの日';
  const dates = (r.dates && r.dates.length) ? r.dates.join('・') : '-';
  el.title =
    `直前3営業日（${dates}）の◎銘柄 ${r.n_symbols != null ? r.n_symbols : '-'} 銘柄で比較:\n` +
    `順張り(トゲ) 合計 ${bpTxt(r.trend_bp, 1)}bp / 逆張り(ボリンジャー) 合計 ${bpTxt(r.revert_bp, 1)}bp\n` +
    `勝っている方＝${trend ? '順張り（トゲ反転）' : '逆張り（ボリンジャーフェード）'}。クリックで再計算。`;
}

async function loadRegime(force) {
  const el = $('regimeBadge');
  if (el && force) { el.hidden = false; el.textContent = '判定中…'; }
  let data;
  try { data = await api('/api/regime' + (force ? '?force=1' : '')); }
  catch (e) { return; }   // バッジが出なくてもチャート本体は動く
  state.regime = data;
  state.regimeCache['today'] = data;   // auto モード（今日表示）と共有
  renderRegimeBadge();
  renderMode();
}

function renderSymbolList() {
  const wrap = $('symGroups');
  const filter = $('symFilter').value.trim().toLowerCase();
  const jp = (state.sources.bars1m || []);
  const us = (state.sources.us_codes || []);

  const match = (code) => {
    if (!filter) return true;
    const hay = (code + ' ' + (symbolMain(code) || '')).toLowerCase();
    return hay.includes(filter);
  };

  const apt = (state.sources && state.sources.bars1m_aptitude) || {};
  const groupHtml = (title, codes) => {
    const items = codes.filter(match).map((code) => {
      const sel = code === state.code ? ' selected' : '';
      const sub = symbolSub(code);
      const subHtml = sub ? `<span class="sym-sub">${escapeHtml(sub)}</span>` : '';
      // 手法適性の印（◎/○ のみ。最新スクリーン結果由来のヒント）。
      const badge = apt[code];
      const dot = badge ? `<span class="sym-apt ${badge === '◎' ? 'apt-good' : 'apt-ok'}" title="手法適性 ${badge}">${badge}</span>` : '';
      return `<li class="sym-item${sel}" data-code="${escapeHtml(code)}" title="${escapeHtml(code)}">` +
        `<span class="sym-main">${escapeHtml(symbolMain(code))}</span>${subHtml}${dot}</li>`;
    }).join('');
    const count = codes.filter(match).length;
    if (!count) return '';
    return `<div class="sym-group-title">${title} <span class="muted">${count}</span></div><ul class="sym-ul">${items}</ul>`;
  };

  let html = groupHtml('日本株', jp) + groupHtml('米国株', us);
  if (!html) html = `<div class="sym-empty">該当する銘柄がありません。</div>`;
  wrap.innerHTML = html;

  wrap.querySelectorAll('.sym-item').forEach((li) => {
    li.addEventListener('click', () => selectSymbol(li.dataset.code));
  });
}

function selectSymbol(code) {
  if (!code) return;
  state.code = code;
  saveState();
  renderCurrentSymbol();
  // 選択ハイライトを即時更新（リスト全再描画は避ける）
  document.querySelectorAll('#symGroups .sym-item').forEach((li) =>
    li.classList.toggle('selected', li.dataset.code === code));
  loadChart();
}

// --- 条件パネル（入出力） ---
// トゲ/BB の ON/OFF はモード3択が担うので checkbox は無い。深さ/戻り/z の値のみ簡易パネルに出す。
// それ以外（角度/直前差/符号/同調/σ/効率/SAR/寄り抑制/注目）は詳細設定パネルの各トグルが担う。
function writeCond() {
  const c = state.cond;
  $('spikeDepthV').value = c.spike.depth; $('spikeTurnV').value = c.spike.turn;
  $('fadeBollV').value = c.fadeBoll.v;
  $('angleOn').checked = c.angle.on; $('angleV').value = c.angle.v;
  $('prevDiffOn').checked = c.prevDiff.on; $('prevDiffV').value = c.prevDiff.v;
  $('rciSignOn').checked = c.rciSign.on; $('rciSignLevelV').value = c.rciSign.level;
  $('dualOn').checked = c.dual.on; $('dualV').value = c.dual.v;
  $('fadeOn').checked = c.fade.on; $('fadeV').value = c.fade.v;
  $('effOn').checked = c.eff.on; $('effV').value = c.eff.v;
  $('sarOn').checked = c.sar.on;
  $('skipOpenV').value = c.skipOpen;
  $('cooldownV').value = c.cooldown;
  $('costV').value = c.cost;
  reflectCondDisabled();
}
function readCond() {
  const c = state.cond;
  const num = (id, d) => { const v = Number($(id).value); return Number.isFinite(v) ? v : d; };
  // トゲ/BB は値のみ読む（ON/OFF はモードが決める＝on は温存）。
  c.spike = { on: c.spike.on, depth: num('spikeDepthV', DEFAULT_COND.spike.depth), turn: num('spikeTurnV', DEFAULT_COND.spike.turn) };
  c.fadeBoll = { on: c.fadeBoll.on, v: num('fadeBollV', DEFAULT_COND.fadeBoll.v) };
  c.angle = { on: $('angleOn').checked, v: num('angleV', DEFAULT_COND.angle.v) };
  c.prevDiff = { on: $('prevDiffOn').checked, v: num('prevDiffV', DEFAULT_COND.prevDiff.v) };
  c.rciSign = { on: $('rciSignOn').checked, level: num('rciSignLevelV', DEFAULT_COND.rciSign.level) };
  c.dual = { on: $('dualOn').checked, v: num('dualV', DEFAULT_COND.dual.v) };
  c.fade = { on: $('fadeOn').checked, v: num('fadeV', DEFAULT_COND.fade.v) };
  c.eff = { on: $('effOn').checked, v: num('effV', DEFAULT_COND.eff.v) };
  c.sar = { on: $('sarOn').checked };
  c.skipOpen = num('skipOpenV', DEFAULT_COND.skipOpen);
  c.cooldown = num('cooldownV', DEFAULT_COND.cooldown);
  c.cost = num('costV', DEFAULT_COND.cost);
  saveState();
}
// OFF の詳細トグルは数値入力を無効化して「効いていない」ことを見せる。
function reflectCondDisabled() {
  $('angleV').disabled = !$('angleOn').checked;
  $('prevDiffV').disabled = !$('prevDiffOn').checked;
  $('rciSignLevelV').disabled = !$('rciSignOn').checked;
  $('dualV').disabled = !$('dualOn').checked;
  $('fadeV').disabled = !$('fadeOn').checked;
  $('effV').disabled = !$('effOn').checked;
}

// 実効の主トリガー（'spike'＝トゲ順張り / 'boll'＝ボリンジャー逆張り）。
//   trend/revert 固定はそのまま。auto は表示日のレジーム判定（cache）に従う（US・未取得は 'spike'）。
function effectivePrimary() {
  if (state.mode === 'trend') return 'spike';
  if (state.mode === 'revert') return 'boll';
  if (!isBars1mCode(state.code)) return 'spike';
  const r = state.regimeCache[regimeKey(chartRegimeDate())];
  return (r && r.pick === 'revert') ? 'boll' : 'spike';
}

// 条件 → API パラメータ。主トリガー（トゲ/BB）はモードで切替え、それ以外の詳細ゲートは加算的に送る。
function condParams() {
  const c = state.cond;
  const primary = effectivePrimary();
  const useSpike = primary === 'spike';
  const useBoll = primary === 'boll';
  // トゲ/同調のいずれかが「発火根拠」として効いているか。効いていて角度 OFF なら slope=0 でよいが、
  // 効いておらず角度も OFF のまま slope=0 を送ると、サーバの bar_signals が全バーで "angle" を暴発させる
  // （逆張り BB モードで実害）。その場合だけ角度トリガーを 9999 で黙らせる（regime.REVERT_PARAMS と同規約）。
  const spikeActive = useSpike && (c.spike.depth > 0 || c.spike.turn > 0);
  const otherTrendGate = spikeActive || c.dual.on;
  const slopeVal = c.angle.on ? c.angle.v : (otherTrendGate ? 0 : 9999);
  return {
    // 主トリガー: モードで一方だけ有効化。SAR は詳細トグル（モード切替時に自動同期）。
    spike_depth_min: useSpike ? c.spike.depth : 0,
    spike_turn_min: useSpike ? c.spike.turn : 0,
    fade_boll_z: useBoll ? c.fadeBoll.v : 0,
    sar_filter: c.sar.on ? 1 : 0,
    cooldown: c.cooldown,
    cost_bp: c.cost,
    // 詳細ゲート（OFF は 0＝サーバ側で無効。ただし角度は上記の暴発回避で 9999 になり得る）。
    slope_threshold: slopeVal,
    prev_diff_min: c.prevDiff.on ? c.prevDiff.v : 0,
    rci_sign_gate: c.rciSign.on ? 1 : 0,
    rci_sign_level: c.rciSign.on ? c.rciSign.level : 0,
    dual_rci_slope_min: c.dual.on ? c.dual.v : 0,
    fade_sigma_k: c.fade.on ? c.fade.v : 0,
    eff_min: c.eff.on ? c.eff.v : 0,
    skip_open_bars: c.skipOpen,
  };
}

// --- モード＆レジーム連動（2026-07-21） ---
function regimeKey(date) { return date || 'today'; }

// auto モードで実効トリガーを決めるレジームの対象日。bars1m 以外・全期間・最新/未解決日は today(null)。
function chartRegimeDate() {
  if (!isBars1mCode(state.code)) return null;
  if (state.viewAll) return null;
  const cur = currentViewDate();
  if (!cur || cur === latestBars1mDate()) return null;
  return cur;
}

// レジーム判定を取得してキャッシュ（date=null は「今日」＝judge_regime(None)）。サーバ側もキャッシュ済み。
async function ensureRegime(date) {
  const key = regimeKey(date);
  if (state.regimeCache[key]) return state.regimeCache[key];
  let data;
  try { data = await api('/api/regime' + (date ? '?date=' + encodeURIComponent(date) : '')); }
  catch (e) { return null; }
  state.regimeCache[key] = data;
  return data;
}

// モード3択の見た目・説明・簡易パラメータの活性表示を更新。
function renderMode() {
  for (const b of $('modeSeg').children) b.classList.toggle('active', b.dataset.mode === state.mode);
  const primary = effectivePrimary();
  const desc = $('modeDesc');
  if (state.mode === 'auto') {
    const r = state.regimeCache[regimeKey(chartRegimeDate())];
    const day = r ? (r.pick === 'revert' ? '▼逆張りの日' : '▲順張りの日') : '判定中…';
    desc.textContent = `この日は ${day} → ${primary === 'boll' ? 'ボリンジャー逆張り' : 'トゲ反転（順張り）'} を表示`;
    desc.className = 'mode-desc ' + (primary === 'boll' ? 'is-revert' : 'is-trend');
  } else if (state.mode === 'trend') {
    desc.textContent = 'トゲ反転（順張り・SAR方向フィルタ ON）を常に表示';
    desc.className = 'mode-desc is-trend';
  } else {
    desc.textContent = 'ボリンジャーフェード（逆張り・SAR方向フィルタ OFF）を常に表示';
    desc.className = 'mode-desc is-revert';
  }
  // 簡易パラメータの活性: 実効トリガー側だけ通常表示、他方は淡色（値は残す＝バンド描画等に使う）。
  $('rowSpike').classList.toggle('inactive', primary !== 'spike');
  $('rowBoll').classList.toggle('inactive', primary !== 'boll');
}

// 実効トリガーが変わった時だけ SAR を自動同期（順張り=ON / 逆張り=OFF）。以後はユーザーの上書きを尊重。
function syncSarToPrimary() {
  const primary = effectivePrimary();
  if (primary !== state.lastPrimary) {
    state.cond.sar = { on: primary === 'spike' };
    if ($('sarOn')) $('sarOn').checked = state.cond.sar.on;
    state.lastPrimary = primary;
  }
}

function setMode(mode) {
  if (mode !== 'auto' && mode !== 'trend' && mode !== 'revert') return;
  state.mode = mode;
  saveState();
  loadChart();   // モードで params が変わるので再取得（loadChart 内で SAR 同期・renderMode）
}

function setDetailOpen(open) {
  state.detailOpen = open;
  $('detailPanel').classList.toggle('hidden', !open);
  $('detailToggle').textContent = open ? '詳細設定 ▾' : '詳細設定 ▸';
  $('detailToggle').setAttribute('aria-expanded', open ? 'true' : 'false');
  saveState();
}

// JP.*（自前保存の日本株1分足＝bars1m）かどうか。sources のリストで判定する。
function isBars1mCode(code) {
  return !!(state.sources && Array.isArray(state.sources.bars1m) && state.sources.bars1m.includes(code));
}

function buildChartUrl(before) {
  const p = new URLSearchParams();
  p.set('code', state.code);
  const cp = condParams();
  p.set('slope_threshold', cp.slope_threshold);
  p.set('prev_diff_min', cp.prev_diff_min);
  p.set('rci_sign_gate', cp.rci_sign_gate);
  p.set('rci_sign_level', cp.rci_sign_level);
  p.set('dual_rci_slope_min', cp.dual_rci_slope_min);
  p.set('spike_depth_min', cp.spike_depth_min);
  p.set('spike_turn_min', cp.spike_turn_min);
  p.set('fade_sigma_k', cp.fade_sigma_k);
  p.set('fade_boll_z', cp.fade_boll_z);
  p.set('eff_min', cp.eff_min);
  p.set('sar_filter', cp.sar_filter);
  p.set('skip_open_bars', cp.skip_open_bars);
  p.set('cooldown', cp.cooldown);
  p.set('cost_bp', cp.cost_bp);
  p.set('tf', state.tf);
  if (isBars1mCode(state.code)) {
    // bars1m は「1営業日単位」表示（2026-07-21）。viewAll=true のときだけ全期間連結（days=0）。
    // それ以外は from=to=表示日（未解決なら利用可能な最新日）で1日ぶんをロードする。ページング(before)は使わない。
    if (state.viewAll) {
      p.set('days', 0);
    } else {
      const d = state.viewDate || latestBars1mDate() || '';
      p.set('from', d);
      p.set('to', d);
    }
  } else if (before) {
    p.set('before', before);   // hist1m の過去方向ページング（bars1m 以外）
  }
  return '/api/chart?' + p.toString();
}

// --- ローディング / 空状態 / トースト ---
function showLoading(on, text) {
  $('loadingText').textContent = text || '読み込み中…';
  $('loading').classList.toggle('hidden', !on);
}
function showEmpty(msg) { $('emptyMsg').textContent = msg; $('empty').classList.remove('hidden'); }
function hideEmpty() { $('empty').classList.add('hidden'); }
let toastTimer = null;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg; t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 6000);
}

// --- チャート読み込み（新規＝最新へ） ---
async function loadChart() {
  if (!state.code) return;
  const seq = ++state.reqSeq;
  state.loadingMore = false; state.noMore = false; state.pagingArmed = false;
  showLoading(true);
  hideEmpty();
  $('moreLoading').classList.add('hidden');
  // auto モードは実効トリガー判定に表示日レジームが要るので先に取得（固定モードはバッジ用に非ブロッキング先読み）。
  if (isBars1mCode(state.code)) {
    if (state.mode === 'auto') { await ensureRegime(chartRegimeDate()); if (seq !== state.reqSeq) return; }
    else ensureRegime(chartRegimeDate());
  }
  syncSarToPrimary();
  renderMode();
  let payload;
  try {
    payload = await api(buildChartUrl(null));
  } catch (e) {
    if (seq !== state.reqSeq) return;
    showLoading(false);
    toast('読み込みに失敗しました: ' + e.message);
    return;
  }
  if (seq !== state.reqSeq) return;   // 途中で別銘柄に切り替わっていたら破棄
  showLoading(false);
  render(payload);
}

function clearSma() {
  for (const s of state.smaSeries) state.chartMain.removeSeries(s.series);
  state.smaSeries = [];
}

function render(payload) {
  state.meta = payload.meta || {};
  const boll = payload.boll || {};
  state.data = {
    candles: payload.candles || [],
    sma: {},
    rci9: payload.rci9 || [], rci14: payload.rci14 || [], sar: payload.sar || [],
    boll: { basis: boll.basis || [], upper: boll.upper || [], lower: boll.lower || [] },
  };
  const smaPeriods = state.meta.sma || [];
  for (const per of smaPeriods) state.data.sma[String(per)] = payload.sma[String(per)] || [];

  state.chartMain.timeScale().applyOptions({ secondsVisible: withSecondsAxis() });
  updateTfControl();
  updateDayNav();
  renderMode();

  // SMA 系列を作り直す（本数固定だが色対応を確実にするため）。
  clearSma();
  smaPeriods.forEach((period, i) => {
    const color = COLORS.sma[i % COLORS.sma.length];
    const s = state.chartMain.addLineSeries({
      color, lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: true,
    });
    state.smaSeries.push({ series: s, period, color });
  });

  applyData();
  state.attention = payload.attention || [];   // setEntries→refreshMarkers が注目も一緒に張る
  setEntries(payload.entries || []);
  renderStats(payload.stats);

  const n = state.data.candles.length;
  if (n === 0) {
    showEmpty(payload.message || 'この銘柄のデータがありません。');
    $('legend').classList.add('hidden');
  } else {
    hideEmpty();
    if (payload.message) toast(payload.message);
    // 初期表示は最新側 N 本（残りは左スクロールで遡って追加読み込み）。
    if (n > INITIAL_VIEW_BARS) {
      state.chartMain.timeScale().setVisibleLogicalRange({ from: n - INITIAL_VIEW_BARS, to: n + 4 });
    } else {
      state.chartMain.timeScale().fitContent();
    }
    renderLegend();
    $('legend').classList.remove('hidden');
  }

  // bars1m は期間指定モード（全期間 or 指定範囲を一括ロード）なので過去方向ページングは使わない。
  state.noMore = isBars1mCode(state.meta.code) ? true : !(state.meta.has_more);
  collapseDetail();
  // 初期のレンジ設定で走る範囲変更イベントを無視するため、少し遅らせて paging を有効化。
  setTimeout(() => { state.pagingArmed = true; }, 500);

  // ハーネス用: ?autoselect=1 のとき先頭エントリーを自動選択（＝アコーディオン展開）する。
  // ヘッドレス Chrome はクリック注入ができないので、この起動フラグが UI 検証の唯一の手段。
  if (state.autoselect && state.entries.length) {
    state.autoselect = false;   // 初回の描画で一度だけ
    selectEntry(0, true);
  }

  // 履歴パネルから「この日時へジャンプ」した場合、描画完了後に該当エントリーを選択（近傍許容）。
  if (state.pendingJump != null) {
    const t = state.pendingJump;
    state.pendingJump = null;
    selectByTime(t);
  }

  // この銘柄の保存済み注釈を読み込んでマーカー表示（別銘柄へ切替時も追従）。
  state.annotations = [];
  loadAnnotations();
}

// 現在の state.data を全系列へ流し込む（新規描画・prepend 共通）。
// マーカーは data 確定後に setEntries() 側で張るのでここでは触らない（古い entries での誤描画防止）。
function applyData() {
  state.candle.setData(state.data.candles);
  state.sar.setData(state.data.sar);
  state.rci9.setData(state.data.rci9);
  state.rci14.setData(state.data.rci14);
  const b = (state.data && state.data.boll) || { basis: [], upper: [], lower: [] };
  state.bollUpper.setData(b.upper || []);
  state.bollLower.setData(b.lower || []);
  state.bollBasis.setData(b.basis || []);
  for (const s of state.smaSeries) s.series.setData(state.data.sma[String(s.period)] || []);
  applyBollVisibility();
}

// バンドの表示可否: 既定 ON（bollShow）。凡例クリックでトグル・localStorage に保存する（戦略トグルとは独立）。
function bollVisible() {
  return !!state.bollShow;
}
function applyBollVisibility() {
  const vis = bollVisible();
  for (const s of [state.bollUpper, state.bollLower, state.bollBasis]) {
    if (s) s.applyOptions({ visible: vis });
  }
}

// --- 過去方向の追加読み込み（左端に近づいたら1ページ前を取得して先頭に結合） ---
async function loadMoreHistory() {
  if (state.loadingMore || state.noMore) return;
  const before = state.meta && state.meta.page_from;
  if (!before) { state.noMore = true; return; }
  state.loadingMore = true;
  $('moreLoading').classList.remove('hidden');
  const seq = state.reqSeq;
  let older;
  try {
    older = await api(buildChartUrl(before));
  } catch (e) {
    state.loadingMore = false;
    $('moreLoading').classList.add('hidden');
    toast('過去データの取得に失敗しました: ' + e.message);
    return;
  }
  $('moreLoading').classList.add('hidden');
  if (seq !== state.reqSeq) { state.loadingMore = false; return; }  // 銘柄が切り替わっていたら破棄
  prependPayload(older);
  state.loadingMore = false;
}

function prependPayload(older) {
  const oc = older.candles || [];
  if (!oc.length) { state.noMore = true; if (!older.meta || !older.meta.has_more) state.noMore = true; return; }
  const n = oc.length;
  const ts = state.chartMain.timeScale();
  const range = ts.getVisibleLogicalRange();

  state.data.candles = oc.concat(state.data.candles);
  state.data.rci9 = (older.rci9 || []).concat(state.data.rci9);
  state.data.rci14 = (older.rci14 || []).concat(state.data.rci14);
  state.data.sar = (older.sar || []).concat(state.data.sar);
  const ob = older.boll || {};
  state.data.boll.basis = (ob.basis || []).concat(state.data.boll.basis);
  state.data.boll.upper = (ob.upper || []).concat(state.data.boll.upper);
  state.data.boll.lower = (ob.lower || []).concat(state.data.boll.lower);
  for (const per of Object.keys(state.data.sma)) {
    state.data.sma[per] = (older.sma[per] || []).concat(state.data.sma[per]);
  }
  applyData();
  setEntries((older.entries || []).concat(state.entries));

  // 同じローソクを見続けられるよう、可視論理レンジを +n シフトして復元。
  if (range) ts.setVisibleLogicalRange({ from: range.from + n, to: range.to + n });

  // ページングカーソルを 1 つ前へ更新。
  if (older.meta) { state.meta.page_from = older.meta.page_from; state.meta.has_more = older.meta.has_more; }
  if (!older.meta || !older.meta.has_more) state.noMore = true;
  // prepend でインデックスがずれるので選択詳細は閉じる。
  collapseDetail();
}

// --- エントリー（マーカー / 一覧） ---
function setEntries(list) {
  state.entries = (list || []).slice().sort((a, b) => a.time - b.time);
  state.entryByTime = new Map();
  for (let i = 0; i < state.entries.length; i++) {
    if (!state.entryByTime.has(state.entries[i].time)) state.entryByTime.set(state.entries[i].time, i);
  }
  refreshMarkers();
  renderEntriesList();
}

// 逆張り系トリガー（BB・σ）は形状を丸にしてトゲ順張り（矢印）と区別する。
const REVERSAL_TRIGGERS = new Set(['fadeb', 'fade']);

function refreshMarkers() {
  if (!state.candle) return;
  const entryMarkers = state.entries.map((e, i) => {
    const long = e.direction === 'long';
    const reversal = REVERSAL_TRIGGERS.has(e.trigger);
    const selected = i === state.selectedIdx;
    return {
      time: e.time,
      position: long ? 'belowBar' : 'aboveBar',
      color: long ? COLORS.long : COLORS.short,
      // トゲ/順張り=矢印、BB/σ逆張り=丸。選択中は少し大きく＋方向ラベルで強調。
      shape: reversal ? 'circle' : (long ? 'arrowUp' : 'arrowDown'),
      size: selected ? 3 : 2,
      text: selected ? (long ? '▲買' : '▼売') : undefined,
    };
  });
  // 注釈マーカーはエントリーシグナルと明確に区別する: 円形＋金/灰/黄＋「注」ラベル。
  const annMarkers = state.annotations.filter((a) => a.bar_epoch != null).map((a) => {
    const long = a.kind === 'entry_long';
    const noEntry = a.kind === 'no_entry';
    const attn = a.kind === 'attention';
    let color = COLORS.ann;
    if (noEntry) color = COLORS.annNo;
    else if (attn) color = COLORS.attention;
    const text = attn ? '注◇' : (noEntry ? '注✕' : (long ? '注▲' : '注▼'));
    return {
      time: a.bar_epoch,
      position: long ? 'belowBar' : 'aboveBar',
      color,
      shape: 'circle',
      text,
    };
  });
  // 注目（事前アラート）マーカー: 黄・四角・「!」でエントリー矢印・注釈円と明確に区別。表示 OFF なら出さない。
  const attnMarkers = (state.showAttention ? state.attention : []).map((a) => {
    const long = a.direction === 'long';
    return {
      time: a.time,
      position: long ? 'belowBar' : 'aboveBar',
      color: COLORS.attention,
      shape: 'square',
      text: '!',
    };
  });
  // lightweight-charts はマーカーを時刻昇順で要求する。
  const all = entryMarkers.concat(annMarkers, attnMarkers).sort((a, b) => a.time - b.time);
  state.candle.setMarkers(all);
}

// 指定時刻（クリックしたバー）にある注釈を返す（マーカークリックは bar_epoch にちょうど載る）。
function annotationAtTime(time) {
  return state.annotations.find((a) => a.bar_epoch === time) || null;
}

// 現在銘柄の注釈を読み込み、マーカーを張り直す（描画競合を避け、読込中に銘柄が変わったら破棄）。
async function loadAnnotations() {
  const code = state.code;
  if (!code) { state.annotations = []; return; }
  let data;
  try { data = await api('/api/annotations?code=' + encodeURIComponent(code)); }
  catch (e) { return; }   // 注釈が取れなくてもチャート本体は動かす
  if (state.code !== code) return;
  state.annotations = data.annotations || [];
  refreshMarkers();
}

// --- 注釈モード（追加・閲覧・削除ダイアログ） ---
const ANN_KIND_LABEL = {
  entry_long: 'ロングすべき ▲', entry_short: 'ショートすべき ▼',
  no_entry: '入っちゃダメ ✕', attention: '注目すべき ◇',
};

function setAnnotateMode(on) {
  state.annotate = on;
  $('annotateBtn').classList.toggle('active', on);
  document.body.classList.toggle('annotate-mode', on);
  if (on) toast('注釈モード: チャートをクリックして「ここでエントリー／入っちゃダメ」を記録できます');
}

function openAnnotateDialog(time) {
  state.pendingBarTime = time;
  const ws = withSecondsAxis();
  $('annotateBarTime').textContent = `${symbolMain(state.code)}　${fmtDate(time)} ${fmtTime(time, ws)}`;
  const first = document.querySelector('#annotateModal input[name="annKind"][value="entry_long"]');
  if (first) first.checked = true;
  $('annotateReason').value = '';
  $('annotateModal').classList.remove('hidden');
  $('annotateReason').focus();
}

function closeAnnotateDialog() {
  $('annotateModal').classList.add('hidden');
  state.pendingBarTime = null;
}

async function saveAnnotation() {
  if (state.pendingBarTime == null) return;
  const kindEl = document.querySelector('#annotateModal input[name="annKind"]:checked');
  const kind = kindEl ? kindEl.value : 'entry_long';
  const reason = $('annotateReason').value.trim();
  try {
    await apiPost('/api/annotations', { code: state.code, bar_time: state.pendingBarTime, kind, reason });
  } catch (e) {
    toast('注釈の保存に失敗しました: ' + e.message);
    return;
  }
  closeAnnotateDialog();
  toast('注釈を保存しました（AI が読める状態になりました）');
  await loadAnnotations();
}

function openAnnotateView(ann) {
  const ws = withSecondsAxis();
  const kindCls = ann.kind === 'entry_long' ? 'long'
    : (ann.kind === 'entry_short' ? 'short' : (ann.kind === 'attention' ? 'attn' : 'no'));
  const ctx = ann.context || {};
  const ctxRows = [
    ['価格', ctx.price], ['RCI(9)', ctx.rci9], ['RCI(14)', ctx.rci14],
    ['SAR', ctx.sar], ['ATR', ctx.atr], ['SMA5', ctx.sma5], ['SMA25', ctx.sma25],
  ].filter(([, v]) => v != null)
    .map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('');
  $('annotateViewBody').innerHTML =
    `<div class="ann-view-kind ${kindCls}">${ANN_KIND_LABEL[ann.kind] || ann.kind}</div>` +
    `<div class="muted">${escapeHtml(symbolMain(state.code))}　${fmtDate(ann.bar_epoch)} ${fmtTime(ann.bar_epoch, ws)}</div>` +
    (ann.reason
      ? `<div class="ann-view-reason">${escapeHtml(ann.reason)}</div>`
      : '<div class="ann-view-reason muted">（理由なし）</div>') +
    (ctxRows ? `<table class="ann-view-ctx">${ctxRows}</table>` : '');
  $('annotateView').dataset.id = ann.id;
  $('annotateView').classList.remove('hidden');
}

function closeAnnotateView() { $('annotateView').classList.add('hidden'); }

async function deleteAnnotationUI() {
  const id = Number($('annotateView').dataset.id);
  if (!id) return;
  try { await apiPost('/api/annotations/delete', { id }); }
  catch (e) { toast('注釈の削除に失敗しました: ' + e.message); return; }
  closeAnnotateView();
  toast('注釈を削除しました');
  await loadAnnotations();
}

function renderLegend() {
  const parts = [`<span class="item"><span class="swatch" style="background:${COLORS.up}"></span>ローソク</span>`];
  for (const s of state.smaSeries) {
    parts.push(`<span class="item"><span class="swatch" style="background:${s.color}"></span>SMA${s.period}</span>`);
  }
  parts.push(`<span class="item"><span class="dot" style="background:${COLORS.sar}"></span>SAR</span>`);
  // ボリンジャーバンド凡例（クリックで表示/非表示を上書きトグル）。表示中は通常色・非表示中は淡色。
  const mboll = (state.meta && state.meta.boll) || {};
  const bw = mboll.window != null ? mboll.window : 20;
  const bz = mboll.z != null ? mboll.z : 3.0;
  const bvis = bollVisible();
  parts.push(`<span class="item boll-legend${bvis ? '' : ' off'}" id="bollLegend" role="button" tabindex="0" ` +
    `title="クリックでバンドの表示/非表示（フェード OFF でも表示できます）">` +
    `<span class="swatch" style="background:${COLORS.bollBand}"></span>BB(${bw},${bz})</span>`);
  parts.push(`<span class="item"><span class="dot" style="background:${COLORS.long}"></span>ロング</span>`);
  parts.push(`<span class="item"><span class="dot" style="background:${COLORS.short}"></span>ショート</span>`);
  // マーカー形状の凡例: トゲ順張り=矢印 / BB逆張り=丸。
  parts.push(`<span class="item" style="color:${COLORS.muted}">▲▼=トゲ ●=BB(逆張り)</span>`);
  // 注目マーカーが表示 ON かつ実際に注目シグナルがある時だけ凡例に出す。
  if (state.showAttention && state.attention && state.attention.length) {
    parts.push(`<span class="item"><span class="dot" style="background:${COLORS.attention}"></span>注目(事前)</span>`);
  }
  const p = (state.meta && state.meta.params) ? state.meta.params : {};
  const bits = [];
  if (p.slope_threshold > 0) bits.push(`角度≥${p.slope_threshold}`);
  if (p.prev_diff_min > 0) bits.push(`直前差≥${p.prev_diff_min}`);
  if (p.rci_sign_gate) bits.push('RCI符号(＋S/−L)');
  if (p.spike_depth_min > 0 || p.spike_turn_min > 0) bits.push(`トゲ反転(深${p.spike_depth_min || 0}/戻${p.spike_turn_min || 0})`);
  if (p.fade_sigma_k > 0) bits.push(`σフェード(k${p.fade_sigma_k})`);
  if (p.fade_boll_z > 0) bits.push(`ボリンジャーフェード(z${p.fade_boll_z})`);
  if (p.eff_min > 0) bits.push(`効率≥${p.eff_min}`);
  const condTxt = bits.length ? bits.join('・') : '条件フィルタなし（SAR方向のみ）';
  parts.push(`<span class="item" style="color:${COLORS.muted}">RCI 9/14・${condTxt}</span>`);
  $('legend').innerHTML = parts.join('');
  // 凡例の BB をクリック/Enter でバンド表示をトグル（フェード OFF でも表示できる軽い手段）。
  const bl = $('bollLegend');
  if (bl) {
    const toggle = () => { state.bollShow = !state.bollShow; saveState(); applyBollVisibility(); renderLegend(); };
    bl.addEventListener('click', toggle);
    bl.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
  }
}

// --- 統計カード ---
function pct(v) { return v == null ? '-' : (v * 100).toFixed(1) + '%'; }
function bpTxt(v, nd) { return v == null ? '-' : (v >= 0 ? '+' : '') + v.toFixed(nd == null ? 2 : nd); }
function signCls(v) { return v == null ? 'muted' : (v >= 0 ? 'pos' : 'neg'); }
function winCls(v) { return v == null ? 'muted' : (v >= 0.5 ? 'pos' : 'neg'); }
// 手法適性バッジ（◎/○/×）。空文字（判定不能）は '-'。
function aptTxt(v) { return v ? v : '-'; }
function aptCls(v) { return v === '◎' ? 'apt-good' : v === '○' ? 'apt-ok' : v === '×' ? 'apt-bad' : 'muted'; }

function renderStats(stats) {
  const el = $('statsCard');
  el.classList.remove('hidden');
  if (!stats || stats.n_signals === 0) {
    el.innerHTML = `<div class="empty-note">この条件ではエントリーなし</div>`;
    return;
  }
  const s = stats;
  const top = [
    ['シグナル', `${s.n_valid} <small>/ 打切 ${s.n_truncated}</small>`],
    ['ロング / ショート', `${s.n_long} <small>/</small> ${s.n_short}`],
    [`期待値 net ${s.primary_label}`, `<span class="${signCls(s.expectancy_bp)}">${bpTxt(s.expectancy_bp)}bp</span>`],
    ['中央値 net', `<span class="${signCls(s.median_net_bp)}">${bpTxt(s.median_net_bp)}bp</span>`],
    ['平均 gross', `<span class="${signCls(s.mean_gross_bp)}">${bpTxt(s.mean_gross_bp)}bp</span>`],
    ['合計 net', `<span class="${signCls(s.sum_net_bp)}">${bpTxt(s.sum_net_bp, 1)}bp</span>`],
    ['最大連敗', `${s.max_losing_streak != null ? s.max_losing_streak : '-'}`],
  ];
  if (s.kind === 'tick') {
    top.push(['MFE / MAE 中央', `<span class="pos">${bpTxt(s.mfe_median_bp)}</span> <small>/</small> <span class="neg">${bpTxt(s.mae_median_bp)}</span>`]);
  }
  const topHtml = top.map(([k, v]) =>
    `<div class="cell"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');

  const rows = (s.horizons || []).map((h) =>
    `<tr><td>${h.label}</td>` +
    `<td class="${winCls(h.win_rate)}">${pct(h.win_rate)}</td>` +
    `<td class="${signCls(h.mean_net_bp)}">${bpTxt(h.mean_net_bp)}</td>` +
    `<td class="${signCls(h.median_net_bp)}">${bpTxt(h.median_net_bp)}</td></tr>`).join('');
  const tableHtml =
    `<table class="stats-table"><thead><tr><th>窓</th><th>勝率</th><th>期待値</th><th>中央</th></tr></thead>` +
    `<tbody>${rows}</tbody></table>`;

  const sessHtml = (s.session || []).map((x) =>
    `<span>${x.label} <b>${x.n}</b>件 <b class="${winCls(x.win_rate)}">${pct(x.win_rate)}</b></span>`).join('');

  el.innerHTML =
    `<div class="stats-top">${topHtml}</div>` + tableHtml +
    (sessHtml ? `<div class="stats-session">${sessHtml}</div>` : '');
}

// --- エントリー一覧 ---
function fwdCell(e) {
  if (!e.forward || !e.forward.length) return '';
  const f = e.forward.find((x) => x.bp != null) || e.forward[0];
  if (f.bp == null) return `<span class="muted">${f.label}: -</span>`;
  return `<span class="${f.bp >= 0 ? 'pos' : 'neg'}">${f.label}: ${f.bp.toFixed(1)}</span>`;
}

function renderEntriesList() {
  const ul = $('entriesList');
  ul.innerHTML = '';
  const n = state.entries.length;
  $('entriesTitle').textContent = `エントリー ${n}件`;
  const nl = state.entries.filter((e) => e.direction === 'long').length;
  $('entriesSub').textContent = n ? `ロング ${nl} / ショート ${n - nl}` : '';

  if (n === 0) {
    const li = document.createElement('li');
    li.style.cursor = 'default';
    li.innerHTML = `<span class="emeta">この条件ではエントリーがありません。条件のチェックを外す・数値を下げると増えます。</span>`;
    ul.appendChild(li);
    return;
  }
  const ws = withSecondsAxis();
  state.entries.forEach((e, i) => {
    const li = document.createElement('li');
    li.dataset.idx = i;
    const dirCls = e.direction === 'long' ? 'long' : 'short';
    const dirTxt = e.direction === 'long' ? '▲L' : '▼S';
    li.innerHTML =
      `<span class="badge ${dirCls}">${dirTxt}</span>` +
      `<span class="etime">${fmtTime(e.time, ws)} <span class="muted">${e.price != null ? e.price : ''}</span></span>` +
      `<span class="muted">効率 ${e.efficiency != null ? e.efficiency.toFixed(1) : '-'}</span>` +
      `<span class="emeta">ΔRCI ${e.drci != null ? e.drci.toFixed(1) : '-'}　直前差 ${e.prev_diff != null ? e.prev_diff.toFixed(1) : '-'}　${fwdCell(e)}</span>`;
    // 同じ行の再クリックで折りたたみ、別の行なら前のを閉じて直下に展開。
    li.addEventListener('click', () => {
      if (state.selectedIdx === i) collapseDetail();
      else selectEntry(i, true);
    });
    ul.appendChild(li);
  });
}

function selectByTime(time) {
  if (state.entryByTime.has(time)) { selectEntry(state.entryByTime.get(time), true); return; }
  let best = -1, bestd = Infinity;
  for (let i = 0; i < state.entries.length; i++) {
    const d = Math.abs(state.entries[i].time - time);
    if (d < bestd) { bestd = d; best = i; }
  }
  const tol = (state.meta && state.meta.resolved_source === 'ticks') ? (state.meta.tf || 60) : 90;
  if (best >= 0 && bestd <= tol) selectEntry(best, true);
}

// クリックした行の直下に展開する詳細ノードを取り除く（重複挿入防止・折りたたみ）。
function removeDetailNode() {
  const d = document.getElementById('entryDetail');
  if (d) d.remove();
}

// 選択エントリーの価格ライン（水平線）を消す。
function clearSelPriceLine() {
  if (state.selPriceLine && state.candle) { state.candle.removePriceLine(state.selPriceLine); }
  state.selPriceLine = null;
}

// 選択とインライン詳細を全て解除する（別銘柄読込・prepend・再クリック折りたたみで使う）。
function collapseDetail() {
  removeDetailNode();
  state.selectedIdx = -1;
  clearSelPriceLine();
  for (const li of $('entriesList').children) li.classList.remove('selected');
  refreshMarkers();   // 選択強調マーカーを解除
}

function selectEntry(idx, scroll) {
  const e = state.entries[idx];
  if (!e) return;
  const ul = $('entriesList');
  removeDetailNode();                         // 前の展開を閉じてから開き直す（別行クリック対応）
  state.selectedIdx = idx;
  for (const li of ul.children) li.classList.toggle('selected', Number(li.dataset.idx) === idx);

  const li = ul.querySelector(`li[data-idx="${idx}"]`);
  if (li) {
    const det = document.createElement('li');
    det.id = 'entryDetail';
    det.className = 'entry-detail';
    det.innerHTML = detailHtml(e);
    li.after(det);                            // クリックした行のすぐ下（パネル幅いっぱい）に展開
    if (scroll) {
      li.scrollIntoView({ block: 'nearest' });
      det.scrollIntoView({ block: 'nearest' });  // 展開ブロック自体も可視域へ入れる
    }
  }
  // どこで入ったか一目で分かるよう、エントリー価格に水平ライン（破線・方向色・軸ラベル）を引く。
  clearSelPriceLine();
  if (e.price != null && state.candle) {
    state.selPriceLine = state.candle.createPriceLine({
      price: e.price, color: e.direction === 'long' ? COLORS.long : COLORS.short,
      lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true,
      title: (e.direction === 'long' ? 'L' : 'S'),
    });
  }
  refreshMarkers();   // 選択マーカーを強調（大きく＋方向ラベル）
  if (e.price != null) state.chartMain.setCrosshairPosition(e.price, e.time, state.candle);
}

// エントリー詳細テーブルの HTML を組み立てる（時刻/方向/価格/各指標/MFE/MAE/前方リターン/理由）。
function detailHtml(e) {
  const ws = withSecondsAxis();
  const rows = [
    ['時刻', `${fmtDate(e.time)} ${fmtTime(e.time, ws)}`],
    ['方向', e.direction === 'long' ? 'ロング ▲' : 'ショート ▼'],
    ['エントリー価格', e.price],
    ['シグナル値', e.signal],
    ['RCI(9)', e.rci],
    ['ΔRCI(角度)', e.drci],
    ['直前RCI差(尖り)', e.prev_diff],
    ['効率', e.efficiency],
    ['SAR', e.sar],
    ['ATR', e.atr],
  ];
  if (e.mfe != null) rows.push(['MFE(bp)', e.mfe]);
  if (e.mae != null) rows.push(['MAE(bp)', e.mae]);
  for (const f of (e.forward || [])) rows.push([f.label, f.bp]);
  const body = rows.map(([k, v]) => {
    let cls = '';
    if (typeof v === 'number' && (k.includes('bp') || k.includes('MFE') || k.includes('MAE'))) cls = v >= 0 ? 'pos' : 'neg';
    return `<tr><td>${k}</td><td class="${cls}">${v == null ? '-' : v}</td></tr>`;
  }).join('');
  return `<h4>${e.direction === 'long' ? 'ロング' : 'ショート'} エントリー詳細</h4>` +
    `<table>${body}</table>` +
    (e.reason ? `<div class="reason">${e.reason}</div>` : '');
}

// --- 足種セレクタ（ticks の時だけ表示） ---
function updateTfControl() {
  const isTick = !!(state.meta && Array.isArray(state.meta.available_tfs));
  $('tfControl').hidden = !isTick;
  if (isTick && state.meta.tf) { state.tf = state.meta.tf; $('tf').value = state.meta.tf; }
  // クールダウン単位はティックが秒・確定足が本。
  $('cooldownLabel').textContent = isTick ? 'クールダウン(秒)' : 'クールダウン(本)';
}

// --- 日送りナビ（bars1m の時だけ表示。1営業日単位＋全期間切替。2026-07-21） ---
// 全営業日は sources.bars1m_dates（昇順）。◀▶ で前後の営業日へ、「全期間」で連結表示に切替える。
function bars1mDates() {
  return (state.sources && state.sources.bars1m_dates) || [];
}
function latestBars1mDate() {
  const ds = bars1mDates();
  return ds.length ? ds[ds.length - 1] : null;
}
// 現在の表示日（viewDate が null なら最新営業日）。
function currentViewDate() {
  return state.viewDate || (state.meta && state.meta.page_to) || latestBars1mDate();
}
// ◀▶ で表示日を delta 日（営業日基準）ずらす。範囲外は無視。
function moveDay(delta) {
  const ds = bars1mDates();
  if (!ds.length) return;
  const cur = currentViewDate();
  let i = ds.indexOf(cur);
  if (i < 0) i = ds.length - 1;
  const j = i + delta;
  if (j < 0 || j >= ds.length) return;
  state.viewDate = ds[j];
  state.viewAll = false;
  saveState();
  loadChart();
}

function updateDayNav() {
  const m = state.meta || {};
  const isBar = m.resolved_source === 'bars1m';
  $('dayNav').hidden = !isBar;
  renderRegimeBadge();   // 表示日が変わったらヘッダの今日バッジの表示可否も更新（過去日では隠す）
  if (!isBar) { renderDayRegimeBadge(); return; }

  const ds = bars1mDates();
  const latest = latestBars1mDate();
  const cur = currentViewDate();
  const label = $('dayLabel');
  if (state.viewAll) {
    label.textContent = '全期間';
  } else {
    label.textContent = cur || '-';
  }
  // 端では ◀▶ を無効化（全期間モードでは日移動そのものを無効化）。
  const i = ds.indexOf(cur);
  $('dayPrev').disabled = state.viewAll || i <= 0;
  $('dayNext').disabled = state.viewAll || i < 0 || i >= ds.length - 1;
  $('dayAll').classList.toggle('active', state.viewAll);

  // 表示中の日のレジームバッジ（全期間・最新日＝ヘッダの「今日」バッジと統合するため非表示）。
  if (state.viewAll || !cur || cur === latest) {
    state.dayRegime = null;
    renderDayRegimeBadge();
  } else {
    loadDayRegime(cur);
  }
}

// 表示日のレジームを取得して日送りバッジへ反映（judge_regime(その日)＝直前3営業日データでの判定）。
async function loadDayRegime(date) {
  const prevPick = state.dayRegime && state.dayRegime.pick;
  const r = await ensureRegime(date);
  // 取得中に別の日へ動いていたら破棄。
  if (currentViewDate() !== date || state.viewAll) return;
  state.dayRegime = r;
  renderDayRegimeBadge(r && r.pick !== prevPick);   // 判定が変わった時だけ一瞬ハイライト
  renderMode();
}

function renderDayRegimeBadge(flash) {
  const el = $('dayRegimeBadge');
  if (!el) return;
  const r = state.dayRegime;
  if (!r || !r.pick) { el.hidden = true; return; }
  const trend = r.pick === 'trend';
  el.hidden = false;
  el.classList.toggle('regime-trend', trend);
  el.classList.toggle('regime-revert', !trend);
  el.textContent = trend ? '順張りの日' : '逆張りの日';
  if (flash) { el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }
  const dates = (r.dates && r.dates.length) ? r.dates.join('・') : '-';
  el.title =
    `この表示日のレジーム（直前3営業日 ${dates} の◎銘柄 ${r.n_symbols != null ? r.n_symbols : '-'} 銘柄で判定）:\n` +
    `順張り(トゲ) 合計 ${bpTxt(r.trend_bp, 1)}bp / 逆張り(ボリンジャー) 合計 ${bpTxt(r.revert_bp, 1)}bp`;
}

// --- スクリーニングビュー ---
function fmtInt(v) { return v == null ? '-' : String(Math.round(v)); }
function fmtNum1(v) { return v == null ? '-' : Number(v).toFixed(1); }

const SCREEN_COLS = [
  { key: 'code', label: '銘柄', type: 'str' },
  { key: 'n_signals', label: 'シグナル数(中央)', type: 'num', fmt: fmtInt },
  // 銘柄適性（機能B）: 参加者が少ない・ボラが無い銘柄を数字で弾く。
  { key: 'aptitude', label: '適性', type: 'str', fmt: aptTxt, cls: aptCls },
  { key: 'turnover_oku', label: '売買代金(億/日)', type: 'num', fmt: fmtNum1 },
  { key: 'range_bp', label: 'レンジ中央(bp)', type: 'num', fmt: fmtNum1 },
  { key: 'bars_per_day', label: 'bars/日', type: 'num', fmt: fmtInt },
  { key: 'tick_count', label: 'Tick回数', type: 'num', fmt: fmtInt },
  { key: 'median_exp1', label: '中央 net +1本', type: 'num', fmt: (v) => bpTxt(v), cls: signCls },
  { key: 'median_win1', label: '勝率 +1本', type: 'num', fmt: pct, cls: winCls },
  { key: 'median_exp2', label: '中央 net +2本', type: 'num', fmt: (v) => bpTxt(v), cls: signCls },
  { key: 'median_win2', label: '勝率 +2本', type: 'num', fmt: pct, cls: winCls },
  { key: 'best_exp1', label: 'ベスト net +1本', type: 'num', fmt: (v) => bpTxt(v), cls: signCls },
  { key: 'best', label: 'ベスト(rci/slope/eff)', type: 'none' },
];

async function loadScreens() {
  let data;
  try { data = await api('/api/screens'); }
  catch (e) { toast('スクリーニング一覧の取得に失敗: ' + e.message); return; }
  state.screen.runs = data.runs || [];
  fillScreenRuns(state.screen.runs, data.latest);
  if (!state.screen.runs.length) {
    renderScreen({ rows: [], run: null, message: 'スクリーニング結果がありません。scalplab.backtest.screen を実行すると reports/screen-* に出力されます。' });
    return;
  }
  await loadScreen(data.latest);
}
function fillScreenRuns(runs, selected) {
  const sel = $('screenRun'); sel.innerHTML = '';
  for (const v of runs) {
    const o = document.createElement('option');
    o.value = v; o.textContent = v; if (v === selected) o.selected = true;
    sel.appendChild(o);
  }
}
async function loadScreen(run) {
  let data;
  try { data = await api('/api/screen' + (run ? '?run=' + encodeURIComponent(run) : '')); }
  catch (e) { toast('スクリーニング結果の取得に失敗: ' + e.message); return; }
  renderScreen(data);
}

// 現在のチャート条件を全 JP 銘柄へ期間指定で適用して銘柄別成績を出す（機能B）。
async function runScreen() {
  const btn = $('screenRunBtn');
  const cp = condParams();
  const body = {
    slope_threshold: cp.slope_threshold, prev_diff_min: cp.prev_diff_min,
    rci_sign_gate: cp.rci_sign_gate, spike_depth_min: cp.spike_depth_min,
    spike_turn_min: cp.spike_turn_min, fade_sigma_k: cp.fade_sigma_k, fade_boll_z: cp.fade_boll_z,
    eff_min: cp.eff_min, sar_filter: cp.sar_filter, skip_open_bars: cp.skip_open_bars,
    cooldown: cp.cooldown, cost_bp: cp.cost_bp,
    from: $('screenFrom').value || null, to: $('screenTo').value || null,
  };
  btn.disabled = true; btn.textContent = '実行中…';
  let data;
  try {
    data = await apiPost('/api/screen/run', body);
  } catch (e) {
    toast('スクリーニング実行に失敗: ' + e.message);
    btn.disabled = false; btn.textContent = 'この条件で実行';
    return;
  }
  btn.disabled = false; btn.textContent = 'この条件で実行';
  if (data.runs) { state.screen.runs = data.runs; fillScreenRuns(data.runs, data.run); }
  renderScreen(data);
  if (data.rows && data.rows.length) toast(`実行完了: ${data.n_symbols || data.rows.length} 銘柄を評価しました`);
}
function renderScreen(data) {
  state.screen.rows = data.rows || [];
  let meta = '';
  if (data.run) {
    meta = `${data.run}・${state.screen.rows.length} 銘柄`;
    if (data.period) meta += `（期間 ${data.period.from}〜${data.period.to}）`;
    meta += '（列見出しクリックでソート / 行クリックでチャートへ）';
  }
  $('screenMeta').textContent = meta;
  const empty = $('screenEmpty');
  if (!state.screen.rows.length) {
    empty.textContent = data.message || 'スクリーニング結果がありません。';
    empty.classList.remove('hidden');
    $('screenTable').innerHTML = '';
    return;
  }
  empty.classList.add('hidden');
  renderScreenTable();
}
function screenSortedRows() {
  const { key, dir } = state.screen.sort;
  const col = SCREEN_COLS.find((c) => c.key === key);
  // 「適性銘柄のみ表示」ON なら ◎/○ だけに絞る（× と判定不能は隠す）。
  let rows = state.screen.rows.slice();
  if (state.screen.aptOnly) rows = rows.filter((r) => r.aptitude === '◎' || r.aptitude === '○');
  if (!col) return rows;
  rows.sort((a, b) => {
    let x = a[key], y = b[key];
    if (col.type === 'str') {
      x = x || ''; y = y || '';
      return dir === 'asc' ? (x < y ? -1 : x > y ? 1 : 0) : (x > y ? -1 : x < y ? 1 : 0);
    }
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return dir === 'asc' ? x - y : y - x;
  });
  return rows;
}
function renderScreenTable() {
  const t = $('screenTable');
  const { key, dir } = state.screen.sort;
  const head = ['<tr><th class="rank">#</th>'];
  for (const c of SCREEN_COLS) {
    const sortable = c.type === 'num' || c.type === 'str';
    const cls = sortable && c.key === key ? (dir === 'asc' ? 'sorted-asc' : 'sorted-desc') : '';
    head.push(`<th class="${cls}" data-key="${sortable ? c.key : ''}">${c.label}</th>`);
  }
  head.push('</tr>');

  const body = screenSortedRows().map((r, i) => {
    const cells = [`<td class="rank">${i + 1}</td>`];
    for (const c of SCREEN_COLS) {
      if (c.key === 'code') {
        const nm = r.name ? ` <span class="sub">${escapeHtml(r.name)}</span>` : '';
        cells.push(`<td><span class="code">${escapeHtml(r.code)}</span>${nm}</td>`);
      } else if (c.key === 'best') {
        cells.push(`<td class="sub">${fmtInt(r.best_rci_period)}/${fmtInt(r.best_slope_threshold)}/${fmtNum1(r.best_eff_min)}</td>`);
      } else {
        const v = r[c.key];
        const cls = c.cls ? c.cls(v) : '';
        cells.push(`<td class="${cls}">${c.fmt ? c.fmt(v) : (v == null ? '-' : v)}</td>`);
      }
    }
    return `<tr data-code="${escapeHtml(r.code)}">${cells.join('')}</tr>`;
  }).join('');

  t.innerHTML = `<thead>${head.join('')}</thead><tbody>${body}</tbody>`;

  t.querySelectorAll('th[data-key]').forEach((th) => {
    const k = th.dataset.key;
    if (!k) return;
    th.addEventListener('click', () => {
      if (state.screen.sort.key === k) {
        state.screen.sort.dir = state.screen.sort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        state.screen.sort = { key: k, dir: k === 'code' ? 'asc' : 'desc' };
      }
      renderScreenTable();
    });
  });
  t.querySelectorAll('tbody tr').forEach((tr) => {
    tr.addEventListener('click', () => openHistory(tr.dataset.code));
  });
}

// スクリーニングの銘柄行 → その銘柄のチャートへ（サーバが code だけでソース自動解決）。
function jumpToChart(code) {
  setTab('chart');
  selectSymbol(code);
}

// --- エントリー履歴パネル（チャンピオン設定固定） ---
// ランキング行クリックで /api/history を取得し、日別グループ＋日別小計＋累計を表示する。
async function openHistory(code) {
  if (!code) return;
  const panel = $('historyPanel');
  panel.classList.remove('hidden');
  $('historyBody').innerHTML = '<div class="muted" style="padding:12px">読み込み中…</div>';
  $('historyTitle').textContent = code;
  let data;
  try {
    data = await api('/api/history?code=' + encodeURIComponent(code));
  } catch (e) {
    $('historyBody').innerHTML = `<div class="screen-empty">履歴の取得に失敗しました: ${escapeHtml(e.message)}</div>`;
    return;
  }
  state.history = { code, data };
  renderHistory(data);
}

function closeHistory() {
  $('historyPanel').classList.add('hidden');
  state.history = { code: null, data: null };
}

function renderHistory(data) {
  const ks = data.eval_bars || [1, 2];
  const nm = data.name ? ` <span class="sub">${escapeHtml(data.name)}</span>` : '';
  $('historyTitle').innerHTML = `${escapeHtml(data.code)}${nm}`;
  const ch = data.champion || {};
  $('historySub').textContent =
    `チャンピオン設定での履歴（トゲ深${ch.spike_depth_min ?? '-'}/戻${ch.spike_turn_min ?? '-'}・CD${ch.cooldown_bars ?? '-'}・コスト${ch.cost_bp ?? '-'}bp・行クリックでチャートへ）`;

  const body = $('historyBody');
  if (!data.days || !data.days.length || !data.totals || !data.totals.n) {
    body.innerHTML = `<div class="screen-empty">${escapeHtml(data.message || 'この銘柄のエントリーはありません（保存日にチャンピオンが発火せず）。')}</div>`;
    return;
  }

  const retHead = ks.map((k) => `<th class="num">+${k}本net</th>`).join('');
  const cumHead = ks.map((k) => `<th class="num">累計+${k}本</th>`).join('');
  const tot = data.totals;
  const totCells = ks.map((k) => `<span class="${signCls(tot.ret[String(k)])}">${bpTxt(tot.ret[String(k)])}</span>`).join(' / ');
  let html = `<div class="hist-total">累計 ${tot.n}件（L${tot.n_long}/S${tot.n_short}）　net合計 ${totCells}</div>`;

  for (const day of data.days) {
    if (!day.entries.length) continue;
    const s = day.subtotal;
    const subCells = ks.map((k) => `<span class="${signCls(s.ret[String(k)])}">${bpTxt(s.ret[String(k)])}</span>`).join(' / ');
    html += `<div class="hist-day">`;
    html += `<div class="hist-day-head">${escapeHtml(day.date)} <span class="muted">${s.n}件（L${s.n_long}/S${s.n_short}）</span> <span class="hist-day-sum">net ${subCells}</span></div>`;
    html += `<table class="hist-table"><thead><tr><th>時刻</th><th>方向</th><th class="num">価格</th>${retHead}${cumHead}<th>トリガー</th></tr></thead><tbody>`;
    for (const e of day.entries) {
      const dirTxt = e.direction === 'long' ? 'ロング▲' : 'ショート▼';
      const dirCls = e.direction === 'long' ? 'pos' : 'neg';
      const retCells = ks.map((k) => `<td class="num ${signCls(e.ret[String(k)])}">${bpTxt(e.ret[String(k)])}</td>`).join('');
      const cumCells = ks.map((k) => `<td class="num ${signCls(e.cum[String(k)])}">${bpTxt(e.cum[String(k)])}</td>`).join('');
      const trg = TRIGGER_LABEL[e.trigger] || e.trigger || '-';
      html += `<tr data-date="${escapeHtml(day.date)}" data-time="${e.time}">` +
        `<td>${fmtTime(e.time, false)}</td><td class="${dirCls}">${dirTxt}</td>` +
        `<td class="num">${e.price != null ? e.price : '-'}</td>${retCells}${cumCells}<td class="sub">${escapeHtml(trg)}</td></tr>`;
    }
    html += `</tbody></table></div>`;
  }
  body.innerHTML = html;

  body.querySelectorAll('tbody tr').forEach((tr) => {
    tr.addEventListener('click', () => jumpToEntry(state.history.code, tr.dataset.date, Number(tr.dataset.time)));
  });
  // 長いランキング表の下に開くので、パネルを可視域へスクロールして気づけるようにする。
  $('historyPanel').scrollIntoView({ block: 'start' });
}

// 履歴の1行 → チャートタブへ切替、その日にジャンプし、該当エントリーを選択（既存機構を再利用）。
function jumpToEntry(code, date, epoch) {
  if (!code) return;
  state.viewDate = date; state.viewAll = false;   // その日だけ表示（日送りナビと同じ1日モード）
  saveState();
  state.pendingJump = epoch;                  // 描画後に selectByTime で選択
  setTab('chart');
  if (state.code === code) {
    loadChart();                              // 同銘柄なら再読込（期間が変わっている）
  } else {
    selectSymbol(code);                       // 別銘柄なら選択→loadChart（内部で走る）
  }
}

// トリガーの日本語ラベル（サーバの hist_entries_to_records と揃える）。
const TRIGGER_LABEL = { spike: 'トゲ反転', dual: 'RCI同調', angle: '角度', fade: 'σフェード', fadeb: 'ボリンジャーフェード' };

// --- タブ切替 ---
function setTab(tab) {
  state.tab = tab;
  document.body.classList.toggle('tab-chart', tab === 'chart');
  document.body.classList.toggle('tab-screen', tab === 'screen');
  for (const b of $('tabs').children) b.classList.toggle('active', b.dataset.tab === tab);
  saveState();
}

// --- 初期化 ---
async function init() {
  // ハーネス/ディープリンク用の URL パラメータ。?autoselect=1（先頭自動展開）・?code=・?from=・?to=・
  // ?history=CODE（スクリーニングタブで該当銘柄の履歴パネルを自動で開く。ヘッドレス検証用）。
  const qs = new URLSearchParams(location.search);
  state.autoselect = qs.get('autoselect') === '1';
  const urlCode = qs.get('code');
  const urlFrom = qs.get('from');
  const urlTo = qs.get('to');
  const urlHistory = qs.get('history');
  initCharts();
  loadState();
  if (qs.get('boll') != null) state.bollShow = qs.get('boll') === '1';  // ディープリンク/検証: バンド表示の明示指定
  // 復元したペイン幅・高さを現在ウィンドウで再クランプして反映し、ドラッグを有効化。
  reclampLayout();
  setupColDrag('resizeLeft', 'left');
  setupColDrag('resizeRight', 'right');
  setupRowDrag();
  setupSectionRowDrag('resizeCond', 'conditions', 'condH', clampCondHeight);
  setupSectionRowDrag('resizeStats', 'statsCard', 'statsH', clampStatsHeight);
  window.addEventListener('resize', reclampLayout);
  try {
    state.sources = await api('/api/sources');
  } catch (e) {
    toast('初期データの取得に失敗しました: ' + e.message);
    return;
  }

  // 復元した銘柄が現存しなければ既定へフォールバック。
  const jp = state.sources.bars1m || [];
  const us = state.sources.us_codes || [];
  const known = new Set([...jp, ...us]);
  if (!state.code || !known.has(state.code)) state.code = state.sources.defaults.code || jp[0] || us[0] || null;
  // URL の ?code=/?date=（旧 ?from= も表示日として受理）があれば上書き（ハーネスのスクショ・ディープリンク用）。
  if (urlCode && known.has(urlCode)) state.code = urlCode;
  const isDate = (v) => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v);
  const urlDate = qs.get('date') || urlFrom || urlTo;
  if (isDate(urlDate)) { state.viewDate = urlDate; state.viewAll = false; }
  const urlMode = qs.get('mode');
  if (urlMode === 'auto' || urlMode === 'trend' || urlMode === 'revert') state.mode = urlMode;

  writeCond();
  setDetailOpen(state.detailOpen);   // 詳細設定の開閉を初期反映
  renderMode();                      // モードセグメント・説明・簡易パラメータ活性の初期反映
  $('attnShowOn').checked = state.showAttention;   // 注目表示トグルの初期反映
  renderCurrentSymbol();
  renderSymbolList();
  setTab(state.tab);
  loadRegime(false);   // レジームバッジ（銘柄非依存・非同期。取れなくても本体は動く）

  // モード3択セグメント（自動/順張り/逆張り）。押すとレジーム連動でエントリーが切り替わる。
  $('modeSeg').addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-mode]'); if (!b) return;
    setMode(b.dataset.mode);
  });
  // 詳細設定の開閉（既定 閉・状態保存）。
  $('detailToggle').addEventListener('click', () => setDetailOpen(!state.detailOpen));

  // 詳細設定パネル内トグルのイベント（トゲ/BB のトグルはモードが担うのでここには無い）。
  for (const id of ['angleOn', 'prevDiffOn', 'rciSignOn', 'dualOn', 'fadeOn', 'effOn', 'sarOn']) {
    $(id).addEventListener('change', () => { readCond(); reflectCondDisabled(); });
  }
  for (const id of ['angleV', 'prevDiffV', 'rciSignLevelV', 'dualV', 'spikeDepthV', 'spikeTurnV', 'fadeV', 'fadeBollV', 'effV', 'skipOpenV', 'cooldownV', 'costV']) {
    $(id).addEventListener('change', readCond);
    $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') { readCond(); loadChart(); } });
  }
  $('apply').addEventListener('click', () => { readCond(); loadChart(); });

  // 注目シグナルの表示 ON/OFF（表示のみ・再計算不要。マーカーと凡例を張り直す）。
  $('attnShowOn').addEventListener('change', () => {
    state.showAttention = $('attnShowOn').checked;
    saveState();
    refreshMarkers();
    if (state.data && state.data.candles.length) renderLegend();
  });

  // レジームバッジのクリックで force 再計算。
  $('regimeBadge').addEventListener('click', () => loadRegime(true));

  $('symFilter').addEventListener('input', renderSymbolList);
  $('tabs').addEventListener('click', (ev) => {
    const b = ev.target.closest('button'); if (!b) return;
    setTab(b.dataset.tab);
    if (b.dataset.tab === 'screen') loadScreens();
    // チャートは表示（＝コンテナが実サイズ）になってから描画する（0 幅描画を避ける）。
    else if (state.code && !state.data) loadChart();
  });
  $('tf').addEventListener('change', () => { state.tf = Number($('tf').value); saveState(); loadChart(); });
  // 日送りナビ（bars1m）: ◀▶ で前後の営業日、「全期間」で連結表示に切替。
  $('dayPrev').addEventListener('click', () => moveDay(-1));
  $('dayNext').addEventListener('click', () => moveDay(1));
  $('dayAll').addEventListener('click', () => {
    if (state.viewAll) {
      state.viewAll = false;
      if (!state.viewDate) state.viewDate = latestBars1mDate();
    } else {
      state.viewAll = true;
    }
    saveState();
    loadChart();
  });
  $('screenRun').addEventListener('change', () => loadScreen($('screenRun').value));
  $('screenRunBtn').addEventListener('click', runScreen);
  // 「適性銘柄のみ表示」トグル（既定 OFF・表示のみ再描画）。
  $('aptOnly').addEventListener('change', () => {
    state.screen.aptOnly = $('aptOnly').checked;
    renderScreenTable();
  });
  // エントリー履歴パネルを閉じる。
  $('historyClose').addEventListener('click', closeHistory);

  // 注釈モードのトグルと各ダイアログのボタン。
  $('annotateBtn').addEventListener('click', () => setAnnotateMode(!state.annotate));
  $('annotateCancel').addEventListener('click', closeAnnotateDialog);
  $('annotateSave').addEventListener('click', saveAnnotation);
  $('annotateViewClose').addEventListener('click', closeAnnotateView);
  $('annotateViewDelete').addEventListener('click', deleteAnnotationUI);
  // 背景クリック・Escape でダイアログを閉じる（誤操作の逃げ道）。
  for (const id of ['annotateModal', 'annotateView']) {
    $(id).addEventListener('click', (e) => { if (e.target === $(id)) $(id).classList.add('hidden'); });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    $('annotateModal').classList.add('hidden');
    $('annotateView').classList.add('hidden');
  });

  // ハーネス: ?history=CODE ならスクリーニングタブで該当銘柄の履歴パネルを自動で開く。
  if (urlHistory) {
    setTab('screen');
    loadScreens();
    openHistory(urlHistory);
    return;
  }
  // 復元したタブに応じて初期表示。チャートは可視のときだけ描画（スクリーニング復元時は遅延）。
  if (state.tab === 'screen') loadScreens();
  else if (state.code) loadChart();
}

document.addEventListener('DOMContentLoaded', init);
