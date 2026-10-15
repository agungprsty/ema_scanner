// Overview: KPI strip per live and shadow strategy version, open signals, latest ledger events, scan status
import { $, emptyHtml, getJson, initChrome } from '../core.js';
import { signalCardHtml } from '../cards.js';
import { feedItemHtml } from '../events.js';
import { escapeHtml, formatWIB, rateText } from '../format.js';
import { kpiBlockHtml } from '../kpi.js';

initChrome();

async function loadKpis() {
  const box = $('kpis');
  try {
    const versions = await getJson('/api/versions');
    const shown = (versions.strategies || []).filter(s => s.status === 'live' || s.status === 'shadow');
    if (!shown.length) {
      box.innerHTML = emptyHtml('No live or shadow strategy is registered.');
      return;
    }
    const perfs = await Promise.all(shown.map(s => getJson(
      `/api/performance?strategy_key=${encodeURIComponent(s.key)}&strategy_version=${encodeURIComponent(s.version)}`,
    ).catch(() => null)));
    const failed = (s) => emptyHtml(`Could not load ${s.ref}.`, true);
    box.innerHTML = shown.map((s, i) => (perfs[i] ? kpiBlockHtml(s, perfs[i]) : failed(s))).join('');
  } catch (e) {
    box.innerHTML = emptyHtml(`Could not load the track record: ${e.message}`, true);
  }
}

async function loadOpenSignals() {
  const box = $('open-signals');
  try {
    const data = await getJson('/api/signals?win_status=PENDING&limit=50');
    const signals = data.signals || [];
    $('open-count').textContent = String(data.total ?? signals.length);
    box.innerHTML = signals.length
      ? signals.map(signalCardHtml).join('')
      : emptyHtml('No open signals. New ones appear after the scheduled scan finds a cross on a closed 2H candle.');
  } catch (e) {
    box.innerHTML = emptyHtml(`Could not load open signals: ${e.message}`, true);
  }
}

async function loadEvents() {
  const list = $('events');
  try {
    const data = await getJson('/api/ledger/events?limit=10');
    const events = data.events || [];
    list.innerHTML = events.length ? events.map(feedItemHtml).join('') : `<li>${emptyHtml('No ledger events yet.')}</li>`;
  } catch (e) {
    list.innerHTML = `<li>${emptyHtml(`Could not load the ledger: ${e.message}`, true)}</li>`;
  }
}

async function loadCron() {
  try {
    const data = await getJson('/api/cron/runs?limit=1');
    $('cron-last').textContent = data.last_success ? `${formatWIB(data.last_success)} WIB` : 'none yet';
    $('cron-next').textContent = data.enabled ? `${formatWIB(data.next_run)} WIB` : 'scheduler off';
    const s = data.schedule || {};
    $('cron-ratio').textContent = s.expected_slots ? rateText(s.on_schedule_slots, s.expected_slots) : 'no slots due yet';
  } catch (e) {
    $('cron-last').innerHTML = `<span class="muted">${escapeHtml(e.message)}</span>`;
  }
}

loadKpis();
loadOpenSignals();
loadEvents();
loadCron();
