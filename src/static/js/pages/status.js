// Status: scheduled scan health from cron_runs, and the ledger head with its server-side verification
import { $, emptyHtml, getJson, initChrome } from '../core.js';
import { verifyHtml } from '../events.js';
import { CANDLE_MS, DASH, durationText, escapeHtml, formatWIB, parseTime, rateText } from '../format.js';

initChrome();

const STATUS_LABELS = { ok: 'ok', error: 'error', skipped: 'skipped (another worker ran it)' };

async function loadHealth() {
  try {
    const data = await (await fetch('/api/health')).json();
    $('st-health').textContent = data.status;
    $('st-health').className = `stat-value ${data.status === 'healthy' ? 'is-up' : 'is-down'}`;
    const parts = [`database ${data.db ? 'up' : 'down'}`, `redis ${data.redis ? 'up' : 'down'}`];
    if (data.cron_fresh != null) parts.push(data.cron_fresh ? 'scan on time' : 'scan overdue');
    $('st-health-sub').textContent = parts.join(' · ');
  } catch {
    $('st-health').textContent = 'offline';
  }
}

function runRowHtml(run) {
  const started = parseTime(run.started_at);
  const lag = started ? started.getTime() - run.slot_ms : null;
  const cls = run.status === 'ok' ? '' : run.status === 'error' ? 'is-down' : 'muted';
  return `
    <tr>
      <td class="num">${escapeHtml(formatWIB(run.slot_ms))}</td>
      <td class="${cls}">${escapeHtml(STATUS_LABELS[run.status] || run.status)}${run.error ? `<span class="hint">${escapeHtml(run.error)}</span>` : ''}</td>
      <td class="r num">${lag == null ? DASH : escapeHtml(durationText(lag))}</td>
      <td class="r num">${run.duration_seconds == null ? DASH : `${escapeHtml(run.duration_seconds)}s`}</td>
      <td class="r num">${escapeHtml(run.signals_count)}</td>
      <td class="r num">${escapeHtml(run.evaluated_count)}</td>
    </tr>`;
}

// Most placeholder rows listed for one stretch without runs; a longer one gets a single summary row
const MAX_MISSED_ROWS = 6;
// A slot is only called missed once its run would be late anyway
const MISSED_GRACE_MS = 15 * 60 * 1000;

function missedRowHtml(slotMs, count = 1) {
  const what = count > 1 ? `${count} slots missed: no run recorded` : 'missed: no run recorded';
  return `
    <tr class="missed">
      <td class="num">${escapeHtml(formatWIB(slotMs))}</td>
      <td class="is-down">${escapeHtml(what)}</td>
      <td class="r num">${DASH}</td><td class="r num">${DASH}</td><td class="r num">${DASH}</td><td class="r num">${DASH}</td>
    </tr>`;
}

// Missed 2H slots between consecutive recorded runs (newest first), and since the newest one
function missedRows(laterMs, earlierMs) {
  const slots = [];
  for (let slot = laterMs - CANDLE_MS; slot > earlierMs; slot -= CANDLE_MS) slots.push(slot);
  if (!slots.length) return '';
  if (slots.length > MAX_MISSED_ROWS) return missedRowHtml(slots[slots.length - 1], slots.length);
  return slots.map(slot => missedRowHtml(slot)).join('');
}

// Recorded runs with a placeholder row for every expected slot that has none (a missed slot otherwise
// shows only as a lower on-schedule ratio)
export function runsWithGapsHtml(runs, now = Date.now()) {
  const html = [];
  let later = null;
  const due = now - MISSED_GRACE_MS;
  const newest = runs.length ? Number(runs[0].slot_ms) : null;
  if (newest != null) later = Math.floor(due / CANDLE_MS) * CANDLE_MS + CANDLE_MS;
  runs.forEach((run) => {
    const slot = Number(run.slot_ms);
    if (later != null && slot < later) html.push(missedRows(later, slot));
    html.push(runRowHtml(run));
    later = later == null ? slot : Math.min(later, slot);
  });
  return html.join('');
}

async function loadRuns() {
  const tbody = $('runs-tbody');
  try {
    const data = await getJson('/api/cron/runs?limit=50');
    const s = data.schedule || {};
    $('st-last').textContent = data.last_success ? formatWIB(data.last_success) : 'none yet';
    $('st-last-sub').textContent = data.last_success ? 'WIB, newest scheduled run that succeeded' : 'no scheduled run has succeeded';
    $('st-ratio').textContent = s.expected_slots ? `${((s.on_schedule_ratio || 0) * 100).toFixed(1)}%` : DASH;
    $('st-ratio-sub').textContent = s.expected_slots
      ? `${rateText(s.on_schedule_slots, s.expected_slots)} slots ran ok within ${Math.round(s.on_schedule_seconds / 60)} min`
      : 'no 2H slot due in the window yet';
    $('st-next').textContent = data.enabled && data.next_run ? formatWIB(data.next_run) : 'off';
    $('st-next-sub').textContent = data.schedule_text || '';
    $('runs-total').textContent = `${data.total_runs} runs recorded`;
    const runs = data.runs || [];
    tbody.innerHTML = runs.length ? runsWithGapsHtml(runs) : `<tr><td colspan="6">${emptyHtml('No scheduled run recorded yet.')}</td></tr>`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6">${emptyHtml(`Could not load cron runs: ${e.message}`, true)}</td></tr>`;
  }
}

async function loadLedger() {
  try {
    const data = await getJson('/api/ledger/head');
    $('lg-head').textContent = data.head.hash;
    $('lg-count').textContent = String(data.head.event_count);
    $('lg-last').textContent = data.head.occurred_at ? `${formatWIB(data.head.occurred_at)} WIB (#${data.head.event_id})` : DASH;
    const heads = data.daily || [];
    $('heads-tbody').innerHTML = heads.length
      ? heads.map(h => `<tr><td class="num">${escapeHtml(h.date)}</td><td class="r num">${escapeHtml(h.event_count)}</td><td class="num hash" title="${escapeHtml(h.head_hash)}">${escapeHtml(h.head_hash)}</td></tr>`).join('')
      : `<tr><td colspan="3">${emptyHtml('No daily head published yet.')}</td></tr>`;
  } catch (e) {
    $('lg-head').textContent = e.message;
    $('heads-tbody').innerHTML = `<tr><td colspan="3">${emptyHtml(e.message, true)}</td></tr>`;
  }
  try {
    $('lg-verify').innerHTML = verifyHtml(await getJson('/api/ledger/verify'));
  } catch (e) {
    $('lg-verify').innerHTML = `<span class="muted">${escapeHtml(e.message)}</span>`;
  }
}

loadHealth();
loadRuns();
loadLedger();
