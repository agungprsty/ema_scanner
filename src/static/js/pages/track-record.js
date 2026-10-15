// Track record of one strategy version: KPIs, equity and underwater curves, R histogram, breakdowns,
// the full ledger with downloads, and a chart for every closed signal
import { $, emptyHtml, getJson, initChrome } from '../core.js';
import { chipsHtml, hasAchievableEntry, netR, outcomeHtml, sideHtml } from '../cards.js';
import {
  chartCandles,
  loadChartLibrary,
  renderDrawdownChart,
  renderEquityChart,
  renderSignalChart,
  showChartMessage,
} from '../chart.js';
import { verifyHtml } from '../events.js';
import {
  DASH,
  escapeHtml,
  formatR,
  formatWIB,
  plural,
  shortHash,
  signClass,
  signedNum,
} from '../format.js';
import { kpiNote, kpiTilesHtml, totalsShown } from '../kpi.js';

initChrome();

const headline = document.body.dataset.headline;
const select = $('version-select');
const tabs = Array.from(document.querySelectorAll('.tabs-bar .tab'));
const charts = [];
let current = null;
let versions = [];
let loaded = {};
let gridOffset = 0;
let gridObserver = null;

function parseRef(ref) {
  const [key, version] = String(ref || '').split('@');
  return { key, version, ref: `${key}@${version}` };
}

function disposeCharts() {
  while (charts.length) charts.pop().remove();
}

function selectTab(name) {
  tabs.forEach((t) => {
    const selected = t.dataset.tab === name;
    t.setAttribute('aria-selected', String(selected));
    // Roving tabindex: only the selected tab is in the tab order, arrows move between tabs
    t.tabIndex = selected ? 0 : -1;
  });
  ['curves', 'breakdown', 'ledger', 'charts'].forEach((v) => { $(`view-${v}`).hidden = v !== name; });
  if (name === 'ledger' && !loaded.ledger) loadLedger();
  if (name === 'charts' && !loaded.charts) startChartGrid();
}

tabs.forEach((t, i) => {
  t.addEventListener('click', () => selectTab(t.dataset.tab));
  t.addEventListener('keydown', (e) => {
    const target = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (target === undefined) return;
    e.preventDefault();
    const next = tabs[(target + tabs.length) % tabs.length];
    selectTab(next.dataset.tab);
    next.focus();
  });
});

// Curves and histogram ------------------------------------------------------------------------------
async function drawCurves(equity, showTotal) {
  const eq = $('equity-chart');
  const dd = $('drawdown-chart');
  if (!equity.length) {
    showChartMessage(eq, 'No closed trades yet.');
    showChartMessage(dd, 'No closed trades yet.');
    return;
  }
  try {
    await loadChartLibrary();
  } catch {
    showChartMessage(eq, 'The chart library could not be loaded.', true);
    return;
  }
  charts.push(renderEquityChart(eq, equity, { showTotal }), renderDrawdownChart(dd, equity));
}

function drawHistogram(bins, width) {
  const box = $('histogram');
  const axis = $('histogram-axis');
  if (!bins.length) {
    box.innerHTML = emptyHtml('No closed trades yet.');
    axis.innerHTML = '';
    $('hist-note').textContent = '';
    return;
  }
  const max = Math.max(...bins.map(b => b.count));
  const labels = [];
  box.innerHTML = bins.map((b) => {
    const mid = (b.lo + b.hi) / 2;
    const cls = b.hi <= 0 ? 'down' : b.lo >= 0 ? 'up' : '';
    const label = `${plural(b.count, 'trade')} from ${signedNum(b.lo)}R to ${signedNum(b.hi)}R`;
    if (b.count) labels.push(label);
    // The count is printed on the bar too, so it does not depend on hovering
    const count = b.count ? `<span class="bar-count num">${escapeHtml(b.count)}</span>` : '';
    return `<div class="bar ${cls}" style="height:${b.count ? Math.max(2, (b.count / max) * 100) : 0}%" title="${escapeHtml(label)}" data-mid="${mid}">${count}</div>`;
  }).join('');
  box.setAttribute('aria-label', `Histogram of net R per closed trade: ${labels.join('; ')}`);
  const lo = bins[0].lo;
  const hi = bins[bins.length - 1].hi;
  const zeroAt = lo < 0 && hi > 0 ? -lo / (hi - lo) : null;
  // The zero label is left out when it would sit on an edge label
  const zero = zeroAt != null && zeroAt > 0.12 && zeroAt < 0.88
    ? `<span class="hist-zero" style="left:${(zeroAt * 100).toFixed(1)}%">0R</span>` : '';
  axis.innerHTML = `<span>${escapeHtml(formatR(lo))}</span>${zero}<span>${escapeHtml(formatR(hi))}</span>`;
  $('hist-note').textContent = `bins of ${width}R; the number on a bar is its count of trades`;
}

// Breakdowns -----------------------------------------------------------------------------------------
const BREAKDOWN_TITLES = { side: 'By side', symbol: 'By symbol', month: 'By exit month (UTC)' };

// `showTotal` false hides summed Net R while the version's totals are held back (too few trades)
function breakdownTableHtml(name, rows, min, showTotal) {
  const body = rows.length ? rows.map(r => `
    <tr class="${r.meaningful ? '' : 'not-meaningful'}">
      <td class="num">${escapeHtml(r.key)}${r.meaningful ? '' : ' <span class="nm">not meaningful</span>'}</td>
      <td class="r num">${escapeHtml(r.n)}</td>
      <td class="r num ${r.meaningful ? signClass(r.mean_net_r) : ''}">${escapeHtml(formatR(r.mean_net_r))}</td>
      <td class="r num">${r.mean_net_r_ci95 ? `[${escapeHtml(signedNum(r.mean_net_r_ci95[0]))}, ${escapeHtml(signedNum(r.mean_net_r_ci95[1]))}]` : DASH}</td>
      <td class="r num">${r.n ? `${escapeHtml(Number(r.win_rate).toFixed(1))}% (${escapeHtml(r.wins)}/${escapeHtml(r.n)})` : DASH}</td>
      <td class="r num">${showTotal ? escapeHtml(formatR(r.total_net_r)) : DASH}</td>
    </tr>`).join('') : `<tr><td colspan="6">${emptyHtml('No closed trades yet.')}</td></tr>`;
  return `
    <div class="panel">
      <div class="panel-title"><h2>${escapeHtml(BREAKDOWN_TITLES[name] || name)}</h2><span class="muted small">greyed: n &lt; ${escapeHtml(min)}</span></div>
      <div class="table-scroll">
        <table class="compact">
          <thead><tr><th>Group</th><th class="r">n</th><th class="r">Mean net R</th><th class="r">95% CI</th><th class="r">Win rate</th><th class="r">Net R</th></tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>
    </div>`;
}

// Ledger -----------------------------------------------------------------------------------------------
function ledgerRowHtml(r) {
  const achievable = r.entry_basis !== 'cross_interp';
  const rCell = (v) => (v == null || !achievable ? DASH : escapeHtml(formatR(v)));
  const counted = r.counted
    ? '<span class="is-up">counted</span>'
    : `<span class="muted">${escapeHtml(r.exclusion_reason ? `excluded: ${r.exclusion_reason}` : (r.win_status === 'PENDING' ? 'open' : 'not counted'))}</span>`;
  return `
    <tr data-href="/s/${encodeURIComponent(r.signal_id)}">
      <td class="num"><a href="/s/${encodeURIComponent(r.signal_id)}">${escapeHtml(r.signal_id)}</a></td>
      <td class="sym">${escapeHtml(r.symbol)}</td>
      <td>${sideHtml(r.side, false)}</td>
      <td class="num">${escapeHtml(r.strategy_version)}</td>
      <td>${escapeHtml(r.source || DASH)}</td>
      <td>${outcomeHtml(r.win_status)}</td>
      <td class="r num">${rCell(r.gross_r)}</td>
      <td class="r num">${r.cost_r == null || !achievable ? DASH : escapeHtml(formatR(-r.cost_r))}</td>
      <td class="r num ${achievable ? signClass(r.net_r) : ''}">${rCell(r.net_r)}</td>
      <td>${counted}</td>
      <td class="num muted">${escapeHtml(formatWIB(r.cross_time))}</td>
      <td class="num muted" title="${escapeHtml(r.last_event_hash || '')}">${escapeHtml(shortHash(r.last_event_hash, 10))}</td>
    </tr>`;
}

async function loadLedger() {
  const tbody = $('ledger-tbody');
  const { key } = current;
  loaded.ledger = true;
  tbody.innerHTML = `<tr><td colspan="12">${emptyHtml('Loading…')}</td></tr>`;
  try {
    const data = await getJson(`/api/ledger?strategy_key=${encodeURIComponent(key)}`);
    if (current.key !== key) return;
    // Newest cross first (ids follow insertion, and a manual or late row can be inserted long after)
    const rows = (data.rows || []).slice().sort((a, b) => (Number(b.cross_candle_ms) - Number(a.cross_candle_ms))
      || (b.signal_id - a.signal_id));
    const counted = rows.filter(r => r.counted).length;
    $('ledger-caption').textContent = `${plural(rows.length, 'signal')} of ${key}, every version, newest first: `
      + `${counted} counted in their version's track record, ${rows.length - counted} open or excluded (late, manual, legacy).`;
    tbody.innerHTML = rows.length ? rows.map(ledgerRowHtml).join('') : `<tr><td colspan="12">${emptyHtml('No signals recorded yet.')}</td></tr>`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="12">${emptyHtml(`Could not load the ledger: ${e.message}`, true)}</td></tr>`;
  }
}

$('ledger-tbody').addEventListener('click', (e) => {
  if (e.target.closest('a')) return;
  const row = e.target.closest('tr[data-href]');
  if (row) window.location.assign(row.dataset.href);
});

// All charts -------------------------------------------------------------------------------------------
const GRID_PAGE = 100;

function miniHtml(s) {
  const r = netR(s);
  const result = hasAchievableEntry(s) && r != null ? `<span class="num ${signClass(r)}">${escapeHtml(formatR(r))}</span>` : '<span class="muted">no fill</span>';
  // A card, not a link around the chart: the header carries the link, and excluded rows their chip
  return `
    <article class="mini${s.is_eligible === false ? ' is-excluded' : ''}">
      <div class="mini-head"><a href="/s/${encodeURIComponent(s.id)}"><span class="sym">${escapeHtml(s.symbol)}</span> ${sideHtml(s.side, false)}</a>${chipsHtml(s)}${result}</div>
      <div class="mini-chart" data-signal="${escapeHtml(s.id)}"><div class="chart-msg">…</div></div>
      <div class="mini-foot num">${outcomeHtml(s.win_status)} · exit ${escapeHtml(formatWIB(s.exit_time))} WIB${s.is_eligible === false ? ' · excluded from stats' : ''}</div>
    </article>`;
}

// Chart requests of the grid go out through a small queue (a few at a time, a few per second), so
// scrolling through it stays well inside the site's per-visitor rate limit
const CANDLE_QUEUE = { maxInFlight: 3, minGapMs: 170 };
let candleInFlight = 0;
let candleLastStart = 0;
const candleWaiting = [];

function pumpCandleQueue() {
  if (!candleWaiting.length || candleInFlight >= CANDLE_QUEUE.maxInFlight) return;
  const wait = candleLastStart + CANDLE_QUEUE.minGapMs - Date.now();
  if (wait > 0) {
    setTimeout(pumpCandleQueue, wait);
    return;
  }
  const { url, resolve, reject } = candleWaiting.shift();
  candleInFlight += 1;
  candleLastStart = Date.now();
  getJson(url).then(resolve, reject).finally(() => {
    candleInFlight -= 1;
    pumpCandleQueue();
  });
  pumpCandleQueue();
}

function queuedCandles(url) {
  return new Promise((resolve, reject) => {
    candleWaiting.push({ url, resolve, reject });
    pumpCandleQueue();
  });
}

async function drawMini(el, sig) {
  try {
    const [payload] = await Promise.all([
      queuedCandles(`/api/signals/${encodeURIComponent(sig.id)}/candles`),
      loadChartLibrary(),
    ]);
    if (!el.isConnected) return;
    const chart = renderSignalChart(el, sig, chartCandles(payload), { compact: true });
    if (chart) charts.push(chart);
  } catch (e) {
    if (el.isConnected) showChartMessage(el, e.message, true);
  }
}

async function loadGridPage() {
  const { key, version } = current;
  const grid = $('chart-grid');
  const more = $('charts-more');
  more.hidden = true;
  try {
    const params = new URLSearchParams({
      strategy_key: key, strategy_version: version, limit: String(GRID_PAGE), offset: String(gridOffset),
    });
    const data = await getJson(`/api/signals?${params}`);
    if (current.key !== key || current.version !== version) return;
    const closed = (data.signals || []).filter(s => s.win_status !== 'PENDING');
    gridOffset += (data.signals || []).length;
    const bySignal = Object.fromEntries(closed.map(s => [String(s.id), s]));
    grid.insertAdjacentHTML('beforeend', closed.map(miniHtml).join(''));
    if (!grid.children.length) grid.innerHTML = emptyHtml('No closed signals for this version yet.');
    grid.querySelectorAll('.mini-chart[data-signal]:not([data-observed])').forEach((el) => {
      el.dataset.observed = '1';
      el.sigData = bySignal[el.dataset.signal];
      gridObserver.observe(el);
    });
    more.hidden = gridOffset >= (data.total || 0);
  } catch (e) {
    grid.insertAdjacentHTML('beforeend', emptyHtml(`Could not load signals: ${e.message}`, true));
  }
}

function startChartGrid() {
  loaded.charts = true;
  gridOffset = 0;
  candleWaiting.length = 0;
  $('chart-grid').innerHTML = '';
  if (gridObserver) gridObserver.disconnect();
  // Each chart loads its candles only when it scrolls into view
  gridObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      gridObserver.unobserve(entry.target);
      if (entry.target.sigData) drawMini(entry.target, entry.target.sigData);
    });
  }, { rootMargin: '200px' });
  loadGridPage();
}

$('charts-more').addEventListener('click', loadGridPage);

// Integrity ---------------------------------------------------------------------------------------------
async function loadIntegrity() {
  try {
    const head = await getJson('/api/ledger/head');
    $('ledger-head').textContent = head.head.hash;
    $('ledger-events').textContent = String(head.head.event_count);
  } catch (e) {
    $('ledger-head').textContent = e.message;
  }
  try {
    $('ledger-verify').innerHTML = verifyHtml(await getJson('/api/ledger/verify'));
  } catch (e) {
    $('ledger-verify').innerHTML = `<span class="muted">${escapeHtml(e.message)}</span>`;
  }
}

// Version ------------------------------------------------------------------------------------------------
function versionNote(info, perf) {
  const parts = [];
  if (info) {
    parts.push(`${info.ref}: ${info.status}${info.frozen ? ', frozen' : ''}`);
    if (info.holdout) parts.push(`holdout ${info.holdout.verdict || 'run'}`);
  }
  if (perf.first_exit_time) parts.push(`exits ${formatWIB(perf.first_exit_time)} to ${formatWIB(perf.last_exit_time)} WIB`);
  return parts.join(' · ');
}

// The backtest card of the selected version (an empty note for a version without one)
function showBaseline(ref) {
  const cards = Array.from(document.querySelectorAll('.baseline-card[data-ref]'));
  const match = cards.some(c => c.dataset.ref === ref);
  cards.forEach((c) => { c.hidden = match ? c.dataset.ref !== ref : c.dataset.ref !== ''; });
}

async function load(ref) {
  current = parseRef(ref);
  loaded = {};
  disposeCharts();
  showBaseline(current.ref);
  const url = new URL(window.location.href);
  url.searchParams.set('strategy', current.ref);
  window.history.replaceState(null, '', url);
  $('dl-csv').href = `/api/ledger.csv?strategy_key=${encodeURIComponent(current.key)}`;
  $('kpi-tiles').innerHTML = '<div class="stat"><div class="stat-label">Loading…</div></div>';
  try {
    const data = await getJson(`/api/track-record?strategy_key=${encodeURIComponent(current.key)}&strategy_version=${encodeURIComponent(current.version)}`);
    if (current.ref !== ref) return;
    const p = data.performance;
    $('kpi-tiles').innerHTML = kpiTilesHtml(p);
    $('kpi-note').textContent = kpiNote(p);
    $('version-note').textContent = versionNote(versions.find(v => v.ref === current.ref), p);
    drawHistogram(data.histogram || [], data.histogram_bin_r);
    const showTotal = totalsShown(p);
    $('breakdowns').innerHTML = ['side', 'symbol', 'month']
      .map(name => breakdownTableHtml(name, (data.breakdowns || {})[name] || [], data.min_breakdown_sample, showTotal)).join('');
    await drawCurves(data.equity || [], showTotal);
  } catch (e) {
    $('kpi-tiles').innerHTML = emptyHtml(`Could not load the track record: ${e.message}`, true);
  }
  const active = tabs.find(t => t.getAttribute('aria-selected') === 'true');
  if (active) selectTab(active.dataset.tab);
}

async function init() {
  const wanted = new URLSearchParams(window.location.search).get('strategy') || headline;
  try {
    versions = (await getJson('/api/versions')).strategies || [];
    select.innerHTML = versions.map(v => `<option value="${escapeHtml(v.ref)}">${escapeHtml(v.ref)} (${escapeHtml(v.status)})</option>`).join('');
  } catch { /* keep the headline option */ }
  if (!Array.from(select.options).some(o => o.value === wanted)) {
    select.insertAdjacentHTML('beforeend', `<option value="${escapeHtml(wanted)}">${escapeHtml(wanted)}</option>`);
  }
  select.value = wanted;
  select.addEventListener('change', () => load(select.value));
  load(wanted);
  loadIntegrity();
}

init();
