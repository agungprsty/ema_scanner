// /ops: the owner's tools behind the cookie sign-in. A scan is a preview unless "Save as manual" is chosen,
// and saved manual signals never count in the track record. The journal is private.
import {
  $,
  authFetch,
  emptyHtml,
  getJson,
  initChrome,
  login,
  logout,
  onUnauthorized,
  responseError,
  showToast,
} from '../core.js';
import { outcomeHtml, sideHtml } from '../cards.js';
import { loadChartLibrary, renderSignalChart, showChartMessage } from '../chart.js';
import {
  debounce,
  escapeHtml,
  formatPct,
  formatPrice,
  formatR,
  formatWIB,
  hourFmt,
  plural,
  signClass,
} from '../format.js';

initChrome();

const PAGE_SIZE = 20;
const ROUND_TRIP_FEE_PERCENT_DEFAULT = 0.1;

const loginPanel = $('login-panel');
const tools = $('ops-tools');
const btnSignout = $('btn-signout');
const caption = $('caption');
const notice = $('action-notice');
const tabs = Array.from(document.querySelectorAll('.tab[data-tab]'));
let activeTab = 'scan';
let liveSignals = null;
let liveMeta = null;
let shownJournal = [];
let journalPage = 0;
let watchlist = [];
let feePercent = ROUND_TRIP_FEE_PERCENT_DEFAULT;

function showNotice(message) {
  notice.textContent = message || '';
  notice.hidden = !message;
}

// Sign-in ---------------------------------------------------------------------------------------------
function setSignedIn(value) {
  loginPanel.hidden = value;
  tools.hidden = !value;
  btnSignout.hidden = !value;
  if (value) {
    selectTab(activeTab);
    loadWatchlist();
  } else {
    $('login-key').focus();
  }
}

onUnauthorized(() => {
  showToast('Your session ended. Sign in again.');
  setSignedIn(false);
});

$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const key = $('login-key').value.trim();
  const error = $('login-error');
  if (!key) {
    error.textContent = 'Enter the API key';
    return;
  }
  $('login-submit').disabled = true;
  try {
    const problem = await login(key);
    if (problem) {
      error.textContent = problem;
      return;
    }
    $('login-key').value = '';
    error.textContent = '';
    showToast('Signed in');
    setSignedIn(true);
  } catch {
    error.textContent = 'Could not reach the server';
  } finally {
    $('login-submit').disabled = false;
  }
});

btnSignout.addEventListener('click', async () => {
  await logout();
  showToast('Signed out');
  setSignedIn(false);
});

// Tabs ------------------------------------------------------------------------------------------------
function selectTab(tab) {
  activeTab = tab;
  tabs.forEach(t => t.setAttribute('aria-selected', String(t.dataset.tab === tab)));
  $('view-scan').hidden = tab !== 'scan';
  $('view-journal').hidden = tab !== 'journal';
  $('view-watchlist').hidden = tab !== 'watchlist';
  caption.textContent = '';
  if (tab === 'scan') renderScan();
  else if (tab === 'journal') loadJournal();
  else renderWatchlist();
}

tabs.forEach(t => t.addEventListener('click', () => { if (t.dataset.tab !== activeTab) selectTab(t.dataset.tab); }));

// Scan ------------------------------------------------------------------------------------------------
function scanCaption() {
  if (!liveMeta) return 'No scan has run in this session. The scheduled scan runs every 2 hours; Run scan checks now.';
  const count = liveSignals.length;
  const forming = liveSignals.filter(s => s.confirmed === false).length;
  const scanned = liveMeta.total_scanned - liveMeta.failed.length;
  if (scanned === 0) return 'The last scan could not load any candles.';
  return `Scanned ${scanned} of ${liveMeta.total_scanned} symbols in ${liveMeta.execution_time} at ${liveMeta.at} WIB. `
    + `${plural(count, 'crossover')} in the last ${liveMeta.lookback} bars, ${liveMeta.new_signals_count} new.`
    + (liveMeta.saved
      ? ' New ones were saved as manual signals, which are excluded from stats.'
      : ' Preview only: nothing was saved or alerted.')
    + (forming ? ' Crosses on the forming candle are shown but not recorded until it closes.' : '');
}

function scanTag(s) {
  if (s.confirmed === false) return '<span class="tag tag-forming">forming</span>';
  if (s.is_new) return '<span class="tag tag-new">new</span>';
  return '';
}

function renderScan() {
  const tbody = $('scan-tbody');
  caption.textContent = scanCaption();
  if (liveSignals === null) {
    tbody.innerHTML = `<tr><td colspan="7">${emptyHtml('Results from your next scan will appear here.')}</td></tr>`;
    return;
  }
  if (!liveSignals.length) {
    tbody.innerHTML = `<tr><td colspan="7">${emptyHtml('No crossovers found.')}</td></tr>`;
    return;
  }
  tbody.innerHTML = liveSignals.map((s, i) => `
    <tr data-index="${i}" tabindex="0">
      <td><span class="sym">${escapeHtml(s.symbol)}</span>${scanTag(s)}</td>
      <td>${sideHtml(s.side, false)}</td>
      <td class="r num">${formatPrice(s.entry_price || s.cross_price)}</td>
      <td class="r num">${formatPrice(s.stop_loss)}</td>
      <td class="r num">${formatPrice(s.tp1)}</td>
      <td>${outcomeHtml(s.win_status)}</td>
      <td class="num muted">${escapeHtml(formatWIB(s.cross_time))}</td>
    </tr>`).join('');
}

async function runScan() {
  const btn = $('btn-scan');
  // A preview by default; saving stores manual signals, which never count in the stats
  const saveDb = $('select-save').value === 'true';
  btn.disabled = true;
  $('scan-icon').classList.add('on');
  $('scan-label').textContent = 'Scanning…';
  showNotice('');
  try {
    const params = new URLSearchParams({
      timeframe: '2h',
      lookback: $('select-lookback').value,
      include_current: $('select-forming').value,
      save_db: String(saveDb),
      send_telegram: 'false',
    });
    const res = await authFetch(`/api/scan?${params.toString()}`, { method: 'POST' });
    if (!res.ok) throw new Error(await responseError(res));
    const data = await res.json();
    liveSignals = data.signals || [];
    liveMeta = {
      total_scanned: data.total_scanned,
      execution_time: data.execution_time,
      lookback: data.lookback,
      new_signals_count: data.new_signals_count,
      failed: data.failed_symbols || [],
      at: hourFmt.format(new Date()),
      saved: saveDb,
    };
    if (liveMeta.failed.length) {
      const all = liveMeta.failed.length === liveMeta.total_scanned;
      showNotice(all
        ? 'Could not load candles for any symbol. Check the server connection to Binance.'
        : `Could not load candles for ${liveMeta.failed.join(', ')}. Results exclude them.`);
    }
  } catch (e) {
    showNotice(`Scan failed: ${e.message}`);
  } finally {
    btn.disabled = false;
    $('scan-icon').classList.remove('on');
    $('scan-label').textContent = 'Run scan';
    selectTab('scan');
  }
}

$('btn-scan').addEventListener('click', runScan);

$('btn-eval').addEventListener('click', async () => {
  const btn = $('btn-eval');
  btn.disabled = true;
  btn.textContent = 'Evaluating…';
  try {
    const res = await authFetch('/api/evaluate', { method: 'POST' });
    if (!res.ok) throw new Error(await responseError(res));
    const data = await res.json();
    showNotice('');
    const n = (data.evaluated && data.evaluated.evaluated_count) || 0;
    showToast(n ? `${plural(n, 'trade')} closed` : 'No open trade reached its target, stop or expiry');
  } catch (e) {
    showNotice(`Evaluation failed: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Evaluate outcomes';
  }
});

// Journal ---------------------------------------------------------------------------------------------
function journalResultHtml(j) {
  if (!j || j.status !== 'TAKEN') return '<span class="muted">–</span>';
  if (j.r_multiple == null) return '<span class="muted">open</span>';
  return `<span class="${signClass(j.r_multiple)}">${formatR(j.r_multiple)}</span> <span class="muted">${formatPct(j.pnl_percent)}</span>`;
}

function journalCaption(sum) {
  if (!sum.taken && !sum.skipped) return '';
  let text = `${sum.taken} taken, ${sum.skipped} skipped.`;
  if (sum.closed_taken) {
    text += ` Your ${plural(sum.closed_taken, 'closed trade')}: ${formatR(sum.your_total_r)} (${formatPct(sum.your_total_pnl_percent)} after fees).`;
  }
  const tracked = (closed, r) => (closed ? `${formatR(r)} net over ${closed} closed` : 'none closed yet');
  text += ` Tracked result of the signals you took: ${tracked(sum.signal_closed_taken, sum.signal_r_taken)}; `
    + `of those you skipped: ${tracked(sum.signal_closed_skipped, sum.signal_r_skipped)}.`;
  return text;
}

async function loadJournal() {
  const tbody = $('journal-tbody');
  const pager = $('journal-pager');
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(journalPage * PAGE_SIZE) });
  const symbol = $('journal-symbol').value.trim().toUpperCase();
  if (symbol) params.append('symbol', symbol);
  if ($('journal-status-filter').value) params.append('status', $('journal-status-filter').value);
  try {
    const res = await authFetch(`/api/journal?${params}`);
    if (!res.ok) throw new Error(await responseError(res));
    const data = await res.json();
    if (activeTab !== 'journal') return;
    shownJournal = data.entries || [];
    caption.textContent = journalCaption(data.summary || {});
    tbody.innerHTML = shownJournal.length ? shownJournal.map((s, i) => `
      <tr data-index="${i}" tabindex="0">
        <td><span class="sym">${escapeHtml(s.symbol)}</span></td>
        <td>${sideHtml(s.side, false)}</td>
        <td>${s.journal && s.journal.status === 'TAKEN' ? 'Taken' : 'Skipped'}</td>
        <td class="r num">${formatPrice(s.journal && s.journal.entry_fill)}</td>
        <td class="r num">${formatPrice(s.journal && s.journal.exit_fill)}</td>
        <td class="r num">${journalResultHtml(s.journal)}</td>
        <td>${outcomeHtml(s.win_status)}</td>
        <td class="num muted">${escapeHtml(formatWIB(s.cross_time))}</td>
      </tr>`).join('') : `<tr><td colspan="8">${emptyHtml('Nothing recorded yet. Open a scan result and use "Your trade".')}</td></tr>`;
    const total = data.total || 0;
    const start = journalPage * PAGE_SIZE;
    pager.hidden = total <= PAGE_SIZE && start === 0;
    pager.querySelector('.pager-info').textContent = shownJournal.length ? `${start + 1}–${start + shownJournal.length} of ${total}` : '';
    pager.querySelector('[data-step="-1"]').disabled = start === 0;
    pager.querySelector('[data-step="1"]').disabled = start + shownJournal.length >= total;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8">${emptyHtml(`Could not load the journal: ${e.message}`, true)}</td></tr>`;
  }
}

$('journal-pager').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-step]');
  if (!btn || btn.disabled) return;
  journalPage = Math.max(0, journalPage + Number(btn.dataset.step));
  loadJournal();
});
$('journal-symbol').addEventListener('input', debounce(() => { journalPage = 0; loadJournal(); }, 250));
$('journal-status-filter').addEventListener('change', () => { journalPage = 0; loadJournal(); });

// Watchlist -------------------------------------------------------------------------------------------
async function loadWatchlist() {
  try {
    const data = await getJson('/api/watchlist');
    watchlist = data.watchlist || [];
    renderWatchlist();
  } catch (e) {
    $('watchlist-list').innerHTML = `<li>${emptyHtml(`Could not load the watchlist: ${e.message}`, true)}</li>`;
  }
}

function renderWatchlist() {
  const list = $('watchlist-list');
  const active = watchlist.filter(w => w.is_active).length;
  $('wl-count').textContent = watchlist.length ? ` ${active}` : '';
  if (activeTab === 'watchlist') {
    caption.textContent = watchlist.length
      ? `${active} of ${watchlist.length} symbols are scanned. Turn a symbol off to skip it without removing it.`
      : '';
  }
  if (!watchlist.length) {
    list.innerHTML = `<li>${emptyHtml('The watchlist is empty. Add a futures symbol to start scanning it.')}</li>`;
    return;
  }
  list.innerHTML = watchlist.map(item => `
    <li class="wl-row${item.is_active ? '' : ' off'}">
      <div class="wl-info">
        <span class="sym">${escapeHtml(item.symbol)}</span>
        <span class="wl-notes">${escapeHtml(item.notes || '')}</span>
      </div>
      <label class="toggle">
        <input type="checkbox" class="wl-toggle" data-symbol="${escapeHtml(item.symbol)}" ${item.is_active ? 'checked' : ''}>
        <span class="track"></span>
        <span>Scan</span>
      </label>
      <button class="btn btn-quiet wl-delete" type="button" data-symbol="${escapeHtml(item.symbol)}">Remove</button>
    </li>`).join('');
}

$('watchlist-list').addEventListener('change', async (e) => {
  const input = e.target.closest('.wl-toggle');
  if (!input) return;
  const sym = input.dataset.symbol;
  const active = input.checked;
  try {
    const res = await authFetch(`/api/watchlist/${encodeURIComponent(sym)}/toggle`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_active: active }),
    });
    if (!res.ok) throw new Error(await responseError(res));
    showNotice('');
  } catch (err) {
    input.checked = !active;
    showNotice(`Could not update ${sym}: ${err.message}`);
    return;
  }
  const item = watchlist.find(w => w.symbol === sym);
  if (item) item.is_active = active;
  renderWatchlist();
});

$('watchlist-list').addEventListener('click', async (e) => {
  const btn = e.target.closest('.wl-delete');
  if (!btn) return;
  const sym = btn.dataset.symbol;
  if (!window.confirm(`Remove ${sym} from the watchlist?`)) return;
  try {
    const res = await authFetch(`/api/watchlist/${encodeURIComponent(sym)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(await responseError(res));
    showNotice('');
  } catch (err) {
    showNotice(`Could not remove ${sym}: ${err.message}`);
    return;
  }
  showToast(`${sym} removed`);
  await loadWatchlist();
});

$('wl-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('input-new-symbol');
  const sym = input.value.trim().toUpperCase();
  if (!sym) return;
  $('btn-add-symbol').disabled = true;
  try {
    const res = await authFetch('/api/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: sym }),
    });
    if (!res.ok) throw new Error(await responseError(res));
    input.value = '';
    showNotice('');
    showToast(`${sym} added`);
    await loadWatchlist();
  } catch (err) {
    showNotice(`Could not add ${sym}: ${err.message}`);
  } finally {
    $('btn-add-symbol').disabled = false;
  }
});

// Signal dialog with the journal form ------------------------------------------------------------------
const modal = $('signal-modal');
const journalForm = $('journal-form');
let currentSignal = null;
let chart = null;
let chartRequest = 0;

function destroyChart() {
  chartRequest += 1;
  if (chart) {
    chart.remove();
    chart = null;
  }
}

async function drawDialogChart(sig) {
  destroyChart();
  const current = chartRequest;
  const viewport = $('chart-viewport');
  viewport.innerHTML = '<div class="chart-msg"><span class="spinner on"></span> Loading candles…</div>';
  try {
    // Recorded signals chart their own trade window; unsaved scan results show the latest candles
    const url = sig.id
      ? `/api/signals/${encodeURIComponent(sig.id)}/candles`
      : `/api/klines?symbol=${encodeURIComponent(sig.symbol)}&limit=80`;
    const [payload] = await Promise.all([getJson(url), loadChartLibrary()]);
    if (current !== chartRequest) return;
    chart = renderSignalChart(viewport, sig, payload.candles || []);
  } catch (e) {
    if (current === chartRequest) showChartMessage(viewport, `Chart unavailable: ${e.message}`, true);
  }
}

function fillDialog(sig) {
  const entry = sig.entry_price || sig.cross_price;
  $('modal-title').innerHTML = `${escapeHtml(sig.symbol)} ${sideHtml(sig.side)}`;
  $('modal-sub').textContent = `Crossed ${formatWIB(sig.cross_time)} WIB on the 2H chart`
    + (sig.confirmed === false ? '. The candle is still forming, so this cross is not recorded yet.' : '');
  $('detail-entry').textContent = formatPrice(entry);
  $('detail-sl').textContent = formatPrice(sig.stop_loss);
  $('detail-tp').textContent = formatPrice(sig.tp1);
  $('detail-outcome').innerHTML = outcomeHtml(sig.win_status);
  $('detail-link').hidden = !sig.id;
  if (sig.id) $('detail-permalink').href = `/s/${encodeURIComponent(sig.id)}`;
}

function selectedJournalStatus() {
  const checked = journalForm.querySelector('input[name="journal-status"]:checked');
  return checked ? checked.value : '';
}

function parseFill(input) {
  const raw = input.value.trim().replace(',', '.');
  if (!raw) return null;
  const num = Number(raw);
  return Number.isFinite(num) && num > 0 ? num : NaN;
}

// Same formula as the server: PnL after fees, and R against the signal's stop
function tradeReturn(side, entry, stop, exit) {
  if (!(entry > 0) || !(exit > 0)) return null;
  const dir = side === 'LONG' ? 1 : -1;
  const pnl = dir * (exit - entry) / entry * 100 - feePercent;
  const risk = Math.abs(entry - stop) / entry * 100;
  return { pnl, r: risk > 0 ? pnl / risk : null };
}

function updateJournalPreview() {
  const result = $('journal-result');
  const status = selectedJournalStatus();
  journalForm.querySelectorAll('[data-taken]').forEach((el) => { el.hidden = status !== 'TAKEN'; });
  if (status !== 'TAKEN' || !currentSignal) {
    result.textContent = '';
    return;
  }
  const entry = parseFill($('journal-entry'));
  const exit = parseFill($('journal-exit'));
  if (Number.isNaN(entry) || Number.isNaN(exit)) {
    result.innerHTML = '<span class="is-down">Prices must be positive numbers</span>';
    return;
  }
  const res = entry && exit ? tradeReturn(currentSignal.side, entry, currentSignal.stop_loss, exit) : null;
  if (res) {
    result.innerHTML = `Your result: <span class="${signClass(res.pnl)}">${formatR(res.r)}</span> (${formatPct(res.pnl)} after fees)`;
  } else {
    result.textContent = entry ? 'Add the exit fill once the trade is closed.' : '';
  }
}

function renderJournalForm(sig) {
  const canRecord = Boolean(sig.id);
  $('journal-fields').hidden = !canRecord;
  $('journal-help').textContent = canRecord
    ? "Record whether you took this signal and at what prices, to compare your results with the signal's."
    : 'Only recorded signals can be journaled. Crosses on a forming candle are recorded once the candle closes.';
  if (!canRecord) return;
  const j = sig.journal || null;
  const status = j ? j.status : '';
  journalForm.querySelectorAll('input[name="journal-status"]').forEach((r) => { r.checked = r.value === status; });
  $('journal-entry').value = j && j.entry_fill != null ? j.entry_fill : '';
  $('journal-exit').value = j && j.exit_fill != null ? j.exit_fill : '';
  $('journal-notes').value = j ? j.notes || '' : '';
  $('journal-entry').placeholder = sig.entry_price ? `Signal entry ${formatPrice(sig.entry_price)}` : '';
  updateJournalPreview();
}

async function openSignal(sig) {
  currentSignal = sig;
  fillDialog(sig);
  renderJournalForm(sig);
  if (!modal.open) modal.showModal();
  if (sig.id) {
    try {
      // Signed in, the signal comes back with the owner's journal entry
      const fresh = await getJson(`/api/signals/${encodeURIComponent(sig.id)}`);
      if (currentSignal !== sig) return;
      currentSignal = { ...sig, ...fresh };
      fillDialog(currentSignal);
      renderJournalForm(currentSignal);
    } catch { /* keep what the list had */ }
  }
  drawDialogChart(currentSignal);
}

journalForm.addEventListener('input', updateJournalPreview);
journalForm.addEventListener('change', updateJournalPreview);

journalForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const sig = currentSignal;
  if (!sig || !sig.id) return;
  const status = selectedJournalStatus();
  const entry = parseFill($('journal-entry'));
  const exit = parseFill($('journal-exit'));
  const result = $('journal-result');
  if (status === 'TAKEN' && (Number.isNaN(entry) || Number.isNaN(exit))) return;
  if (status === 'TAKEN' && exit && !entry) {
    result.innerHTML = '<span class="is-down">Add the entry fill first</span>';
    return;
  }
  if (!status && !sig.journal) {
    showToast('Nothing to save');
    return;
  }
  const url = `/api/signals/${encodeURIComponent(sig.id)}/journal`;
  $('journal-save').disabled = true;
  try {
    const res = status
      ? await authFetch(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          status,
          entry_fill: status === 'TAKEN' ? entry : null,
          exit_fill: status === 'TAKEN' ? exit : null,
          notes: $('journal-notes').value.trim(),
        }),
      })
      : await authFetch(url, { method: 'DELETE' });
    if (!res.ok) throw new Error(await responseError(res));
    const saved = status ? await res.json() : null;
    sig.journal = saved ? saved.journal : null;
    renderJournalForm(sig);
    showToast(status ? 'Saved to your journal' : 'Removed from your journal');
    if (activeTab === 'journal') loadJournal();
  } catch (err) {
    result.innerHTML = `<span class="is-down">${escapeHtml(`Could not save: ${err.message}`)}</span>`;
  } finally {
    $('journal-save').disabled = false;
  }
});

function bindRows(tbody, pick) {
  const handler = (e) => {
    if (e.type === 'keydown' && e.key !== 'Enter') return;
    const row = e.target.closest('tr[data-index]');
    if (!row) return;
    const item = pick(Number(row.dataset.index));
    if (item) openSignal(item);
  };
  tbody.addEventListener('click', handler);
  tbody.addEventListener('keydown', handler);
}

bindRows($('scan-tbody'), i => (liveSignals || [])[i]);
bindRows($('journal-tbody'), i => shownJournal[i]);

$('modal-close').addEventListener('click', () => modal.close());
modal.addEventListener('click', (e) => { if (e.target === modal) modal.close(); });
modal.addEventListener('close', () => {
  destroyChart();
  currentSignal = null;
});

async function loadConfig() {
  try {
    const info = await getJson('/api/info');
    if (info.round_trip_fee_percent != null) feePercent = info.round_trip_fee_percent;
  } catch { /* keep the default */ }
}

loadConfig();
if (document.body.dataset.signedIn === 'true') setSignedIn(true);
