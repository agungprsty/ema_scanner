// /s/{id}: the chart with its At publish | After close toggle, the ledger timeline and recent signals on
// the same symbol. The banner and facts are rendered on the server.
import { $, emptyHtml, getJson, initChrome, responseError } from '../core.js';
import { signalCardHtml } from '../cards.js';
import { chartCandles, loadChartLibrary, renderSignalChart, showChartMessage } from '../chart.js';
import { eventDetail, eventLabel } from '../events.js';
import { escapeHtml, formatWIB, parseTime, shortHash } from '../format.js';

initChrome();

const signalId = document.body.dataset.signalId;
const viewport = $('chart-viewport');
const candleCache = {};
let signal = null;
let chart = null;
let chartRequest = 0;

function destroyChart() {
  chartRequest += 1;
  if (chart) {
    chart.remove();
    chart = null;
  }
}

async function candlesFor(view) {
  if (!candleCache[view]) {
    candleCache[view] = (async () => {
      const res = await fetch(`/api/signals/${encodeURIComponent(signalId)}/candles${view === 'publish' ? '?view=publish' : ''}`);
      if (!res.ok) throw new Error(await responseError(res));
      return res.json();
    })().catch((err) => {
      delete candleCache[view];
      throw err;
    });
  }
  return candleCache[view];
}

async function drawChart(view) {
  destroyChart();
  const current = chartRequest;
  viewport.innerHTML = '<div class="chart-msg"><span class="spinner on"></span> Loading candles…</div>';
  try {
    const [payload] = await Promise.all([candlesFor(view), loadChartLibrary()]);
    if (current !== chartRequest) return;
    if (!Array.isArray(payload.candles) || !payload.candles.length) {
      showChartMessage(viewport, 'No candle data for this signal.');
      return;
    }
    // At publish: only candles closed when the signal was detected, whatever the server sent
    const detected = parseTime(signal.detected_at);
    const asOfMs = view === 'publish' ? (detected ? detected.getTime() : Number(signal.cross_candle_ms) + 2 * 3600000) : null;
    chart = renderSignalChart(viewport, signal, chartCandles(payload), { asOfMs });
  } catch (e) {
    if (current !== chartRequest) return;
    showChartMessage(viewport, `Chart unavailable: ${e.message}`, true);
  }
}

function timelineItemHtml(ev) {
  const detail = eventDetail(ev);
  const payload = JSON.stringify(ev.payload, null, 2);
  return `
    <li>
      <div><span class="ev-type">${escapeHtml(eventLabel(ev.event_type))}</span> <span class="ev-time num">${escapeHtml(formatWIB(ev.occurred_at))} WIB · event #${escapeHtml(ev.id)}</span></div>
      ${detail ? `<div class="ev-detail">${escapeHtml(detail)}</div>` : ''}
      <div class="ev-hash num">hash ${escapeHtml(ev.hash)}</div>
      <div class="ev-hash num">prev ${escapeHtml(shortHash(ev.prev_hash, 24))}…</div>
      <details><summary>Payload</summary><pre>${escapeHtml(payload)}</pre></details>
    </li>`;
}

async function loadTimeline() {
  const list = $('timeline');
  try {
    const data = await getJson(`/api/signals/${encodeURIComponent(signalId)}/events`);
    const events = data.events || [];
    list.innerHTML = events.length
      ? events.map(timelineItemHtml).join('')
      : `<li>${emptyHtml('No ledger events recorded for this signal.')}</li>`;
  } catch (e) {
    list.innerHTML = `<li>${emptyHtml(`Could not load the ledger events: ${e.message}`, true)}</li>`;
  }
}

async function loadRecent() {
  const box = $('recent-signals');
  try {
    const data = await getJson(`/api/signals?symbol=${encodeURIComponent(signal.symbol)}&limit=6`);
    const others = (data.signals || []).filter(s => String(s.id) !== String(signalId)).slice(0, 5);
    box.innerHTML = others.length ? others.map(signalCardHtml).join('') : emptyHtml('No other signals on this symbol yet.');
  } catch (e) {
    box.innerHTML = emptyHtml(`Could not load recent signals: ${e.message}`, true);
  }
}

document.querySelectorAll('input[name="chart-view"]').forEach((input) => {
  input.addEventListener('change', () => { if (input.checked && signal) drawChart(input.value); });
});

async function init() {
  loadTimeline();
  try {
    signal = await getJson(`/api/signals/${encodeURIComponent(signalId)}`);
  } catch (e) {
    showChartMessage(viewport, `Could not load this signal: ${e.message}`, true);
    return;
  }
  const checked = document.querySelector('input[name="chart-view"]:checked');
  drawChart(checked ? checked.value : 'after');
  loadRecent();
}

init();
