document.addEventListener('DOMContentLoaded', () => {
  const TZ = 'Asia/Jakarta';
  const API_KEY_STORAGE = 'crossline_api_key';
  const LEGACY_API_KEY_STORAGE = 'tokyo_api_key';
  const PAGE_SIZE = 20;
  const TF_UNIT_MS = { m: 60 * 1000, h: 60 * 60 * 1000, d: 24 * 60 * 60 * 1000 };
  const TAB_STATUSES = {
    active: ['PENDING', 'OPEN'],
    history: ['TP_HIT', 'SL_HIT', 'EXPIRED', 'NOT_FILLED'],
  };
  const CHART_LIB_URL = 'https://unpkg.com/lightweight-charts@4.2.1/dist/lightweight-charts.standalone.production.js';

  // Defaults until /api/info answers
  const config = { timeframe: '', rsi_period: 14, oversold: 28, overbought: 72, fee_percent: 0.1 };

  let activeTab = 'active';
  let lastRunMs = null;
  let listRequest = 0;
  let shownSignals = [];
  let shownJournal = [];
  let watchlist = [];
  let chart = null;
  let chartRequest = 0;
  let currentSignal = null;
  const pageIndex = { active: 0, history: 0, journal: 0 };

  const $ = (id) => document.getElementById(id);

  // Signals
  const tbody = $('signals-tbody');
  const signalCards = $('signals-cards');
  const btnScan = $('btn-scan');
  const scanLabel = $('scan-label');
  const scanIcon = $('scan-icon');
  const caption = $('caption');
  const actionNotice = $('action-notice');
  const watchlistNotice = $('watchlist-notice');
  const toast = $('toast');

  // Tabs & views
  const tabs = Array.from(document.querySelectorAll('.tab'));
  const views = {
    signals: $('view-signals'),
    journal: $('view-journal'),
    watchlist: $('view-watchlist'),
  };

  // Journal
  const journalTbody = $('journal-tbody');
  const journalCards = $('journal-cards');

  // Filters
  const filters = $('filters');
  const filterSymbol = $('filter-symbol');
  const filterDate = $('filter-date');
  const filterSide = $('filter-side');
  const filterStatus = $('filter-status');
  const filterJournal = $('filter-journal');
  const pagers = Object.fromEntries(
    Array.from(document.querySelectorAll('[data-pager]')).map(el => [el.dataset.pager, el])
  );

  // Watchlist
  const wlForm = $('wl-form');
  const inputNewSymbol = $('input-new-symbol');
  const btnAddSymbol = $('btn-add-symbol');
  const watchlistList = $('watchlist-list');
  const wlCount = $('wl-count');

  // Detail dialog
  const modal = $('signal-modal');
  const modalClose = $('modal-close');
  const chartViewport = $('chart-viewport');
  const journalForm = $('journal-form');
  const journalEntry = $('journal-entry');
  const journalExit = $('journal-exit');
  const journalNotes = $('journal-notes');
  const journalResult = $('journal-result');
  const journalSave = $('journal-save');

  function showNotice(message) {
    actionNotice.textContent = message || '';
    actionNotice.hidden = !message;
  }

  let toastTimer = null;
  function showToast(message) {
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 2500);
  }

  // Formatting
  const dateTimeFmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  });

  const clockFmt = new Intl.DateTimeFormat('en-GB', {
    timeZone: TZ,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  });

  const hourFmt = new Intl.DateTimeFormat('en-GB', {
    timeZone: TZ,
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  });

  function parseTime(value) {
    if (value == null || value === '') return null;
    const dt = new Date(value);
    return isNaN(dt.getTime()) ? null : dt;
  }

  // Binance interval ("15m", "1h", "1d") in ms
  function intervalMs(tf) {
    const m = /^(\d+)([mhd])$/.exec(tf || '');
    return m ? Number(m[1]) * TF_UNIT_MS[m[2]] : null;
  }

  function formatWIB(value) {
    const dt = value instanceof Date ? value : parseTime(value);
    if (!dt) return value ? String(value) : '–';
    const p = Object.fromEntries(dateTimeFmt.formatToParts(dt).map(x => [x.type, x.value]));
    return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
  }

  function formatTimeOnlyWIB(value) {
    const dt = parseTime(value);
    return dt ? hourFmt.format(dt) : '--:--';
  }

  function formatPrice(val) {
    if (val == null || val === '' || isNaN(val)) return '–';
    const num = Number(val);
    if (num >= 1000) return num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (num >= 1) return num.toFixed(4);
    return num.toFixed(6);
  }

  function formatPct(val) {
    const num = Number(val) || 0;
    return `${num > 0 ? '+' : num < 0 ? '−' : ''}${Math.abs(num).toFixed(2)}%`;
  }

  function formatR(val, digits = 2) {
    if (val == null || isNaN(val)) return '–';
    const num = Number(val);
    return `${num > 0 ? '+' : num < 0 ? '−' : ''}${Math.abs(num).toFixed(digits)}R`;
  }

  function formatRsi(val) {
    return val == null || isNaN(val) ? '–' : Number(val).toFixed(1);
  }

  function signClass(val) {
    if (val == null || isNaN(val)) return '';
    return val > 0 ? 'is-long' : val < 0 ? 'is-short' : '';
  }

  function plural(n, word) {
    return `${n} ${word}${n === 1 ? '' : 's'}`;
  }

  function escapeHtml(value) {
    if (value == null) return '';
    return String(value).replace(/[&<>"']/g, ch => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[ch]));
  }

  function sideHtml(side) {
    const isLong = side === 'LONG';
    return `<span class="${isLong ? 'is-long' : 'is-short'}">${isLong ? 'Long' : 'Short'}</span>`;
  }

  const OUTCOMES = {
    PENDING: { cls: '', label: 'Awaiting fill' },
    OPEN: { cls: 'open', label: 'Open' },
    TP_HIT: { cls: 'win', label: 'Target hit' },
    SL_HIT: { cls: 'loss', label: 'Stopped out' },
    EXPIRED: { cls: 'expired', label: 'Expired' },
    NOT_FILLED: { cls: 'void', label: 'Not filled' },
  };

  function outcomeOf(status) {
    return OUTCOMES[status] || { cls: '', label: status || '–' };
  }

  function outcomeHtml(status) {
    const o = outcomeOf(status);
    return `<span class="outcome ${o.cls}">${o.label}</span>`;
  }

  // R and PnL of a closed signal; a dash while it is open
  function resultHtml(s) {
    if (s.r_multiple == null && s.pnl_percent == null) return '<span class="muted">–</span>';
    const r = s.r_multiple != null ? `<span class="${signClass(s.r_multiple)}">${formatR(s.r_multiple)}</span> ` : '';
    return `${r}<span class="muted">${formatPct(s.pnl_percent)}</span>`;
  }

  function journalTagHtml(status) {
    if (status === 'TAKEN') return '<span class="tag">taken</span>';
    if (status === 'SKIPPED') return '<span class="tag">skipped</span>';
    return '';
  }

  // Same formula as the server: PnL after fees, and R against the signal's own planned risk, not the fill's distance to the stop
  function tradeReturn(sig, entry, exit) {
    if (!(entry > 0) || !(exit > 0)) return null;
    const dir = sig.side === 'LONG' ? 1 : -1;
    const pnl = dir * (exit - entry) / entry * 100 - config.fee_percent;
    const risk = Math.abs(sig.entry_price - sig.stop_loss) / sig.entry_price * 100;
    return { pnl, r: risk > 0 ? pnl / risk : null };
  }

  // API helpers
  function readStorage(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  }

  function writeStorage(key, value) {
    try { localStorage.setItem(key, value); } catch { /* storage unavailable */ }
  }

  function getApiKey() {
    return readStorage(API_KEY_STORAGE) || readStorage(LEGACY_API_KEY_STORAGE) || '';
  }

  function setKeyHeader(options, key) {
    if (options.headers instanceof Headers) {
      options.headers.set('X-API-KEY', key);
    } else {
      options.headers['X-API-KEY'] = key;
    }
  }

  async function authFetch(url, options = {}) {
    const key = getApiKey();
    options.headers = options.headers || {};
    if (key) setKeyHeader(options, key);
    const res = await fetch(url, options);
    if (res.status === 401) {
      const entered = (prompt('This action needs the server API key (API_SECRET_KEY).') || '').trim();
      if (entered) {
        writeStorage(API_KEY_STORAGE, entered);
        setKeyHeader(options, entered);
        return await fetch(url, options);
      }
    }
    return res;
  }

  async function responseError(res) {
    let detail = null;
    try {
      detail = (await res.json()).detail;
    } catch {
      detail = null;
    }
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail) && detail.length) {
      return detail.map(d => (d && d.msg) ? d.msg : String(d)).join('; ');
    }
    if (res.status === 503) return 'API key is not configured on the server';
    return `request failed (HTTP ${res.status})`;
  }

  async function getJson(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(await responseError(res));
    return res.json();
  }

  function debounce(fn, ms) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  // Empty / error states
  function emptyHtml(message, isError = false) {
    return `<div class="empty${isError ? ' err' : ''}">${escapeHtml(message)}</div>`;
  }

  function setListMessage(tbodyEl, cardsEl, columns, message, isError = false) {
    const html = emptyHtml(message, isError);
    tbodyEl.innerHTML = `<tr><td colspan="${columns}">${html}</td></tr>`;
    cardsEl.innerHTML = html;
  }

  function setSignalsMessage(message, isError = false) {
    setListMessage(tbody, signalCards, 7, message, isError);
    shownSignals = [];
    pagers.signals.hidden = true;
  }

  function setJournalMessage(message, isError = false) {
    setListMessage(journalTbody, journalCards, 8, message, isError);
    shownJournal = [];
    pagers.journal.hidden = true;
  }

  // Pagination
  function pageParams(tab) {
    return { limit: String(PAGE_SIZE), offset: String(pageIndex[tab] * PAGE_SIZE) };
  }

  function renderPager(tab, total, count) {
    const el = pagers[tab === 'journal' ? 'journal' : 'signals'];
    const start = pageIndex[tab] * PAGE_SIZE;
    if (!total || (total <= PAGE_SIZE && start === 0)) {
      el.hidden = true;
      return;
    }
    el.querySelector('.pager-info').textContent = `${start + 1}–${start + count} of ${total}`;
    el.querySelector('[data-step="-1"]').disabled = start === 0;
    el.querySelector('[data-step="1"]').disabled = start + count >= total;
    el.hidden = false;
  }

  // A page can end up past the last row, e.g. after a journal entry is removed; go to the last page
  function stepBackIfEmpty(tab, count, total) {
    if (count > 0 || !total || pageIndex[tab] === 0) return false;
    pageIndex[tab] = Math.floor((total - 1) / PAGE_SIZE);
    reloadActiveList();
    return true;
  }

  // A pager is only visible inside the active tab's view, so it always pages that tab
  Object.values(pagers).forEach(el => el.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-step]');
    if (!btn || btn.disabled) return;
    pageIndex[activeTab] = Math.max(0, pageIndex[activeTab] + Number(btn.dataset.step));
    reloadActiveList();
    const panel = document.querySelector('.panel');
    if (panel.getBoundingClientRect().top < 0) panel.scrollIntoView({ block: 'start' });
  }));

  function setCaption(text) {
    caption.textContent = text || '';
  }

  // Clock
  function tickClock() {
    $('clock').textContent = clockFmt.format(new Date());
  }
  tickClock();
  setInterval(tickClock, 1000);

  // Health, cron & config
  async function checkHealth() {
    const dot = $('health-dot');
    const text = $('health-text');
    const cronText = $('cron-text');
    try {
      const data = await getJson('/api/health');
      if (data.database && data.redis) {
        dot.className = 'dot ok';
        text.textContent = 'Online';
      } else {
        const down = [!data.database && 'database', !data.redis && 'redis'].filter(Boolean).join(', ');
        dot.className = 'dot warn';
        text.textContent = `Degraded (${down})`;
      }
      cronText.textContent = data.cron && data.cron.enabled ? formatTimeOnlyWIB(data.cron.next_run) : 'Off';
      const lastRun = data.cron && parseTime(data.cron.last_run);
      if (lastRun) lastRunMs = lastRun.getTime();
    } catch {
      dot.className = 'dot err';
      text.textContent = 'Offline';
      cronText.textContent = '--:--';
    }
  }

  async function loadConfig() {
    try {
      const info = await getJson('/api/info');
      Object.assign(config, info.strategy || {});
      if (info.timeframe) config.timeframe = info.timeframe;
      $('brand-meta').textContent = `RSI ${config.oversold}/${config.overbought} scanner · ${config.timeframe} · Binance USDⓈ-M futures`;
    } catch {
      // keep defaults
    }
  }

  // Summary
  async function loadSummary() {
    try {
      const data = await getJson('/api/summary');
      const wins = data.tp_hit_count || 0;
      const losses = data.sl_hit_count || 0;
      const expired = data.expired_count || 0;
      const closed = wins + losses + expired;

      $('stat-winrate').textContent = closed ? `${Number(data.winrate || 0).toFixed(1)}%` : '–';
      $('bar-wins').style.width = closed ? `${(wins / closed) * 100}%` : '0';
      $('bar-losses').style.width = closed ? `${(losses / closed) * 100}%` : '0';
      $('bar-expired').style.width = closed ? `${(expired / closed) * 100}%` : '0';
      $('stat-win-ratio').textContent = closed
        ? `${wins} hit target, ${losses} stopped, ${expired} expired`
        : 'No closed trades yet';

      const windows = (data.filled_count || 0) + (data.not_filled_count || 0);
      $('stat-fillrate').textContent = windows ? `${Number(data.fill_rate || 0).toFixed(1)}%` : '–';
      $('stat-fill-sub').textContent = windows
        ? `${data.filled_count || 0} filled, ${data.not_filled_count || 0} not filled`
        : 'No fill windows closed yet';

      const netR = $('stat-net-r');
      const trades = data.closed_trades || 0;
      netR.textContent = trades ? formatR(data.total_r, 1) : '–';
      netR.className = `stat-value num ${trades ? signClass(data.total_r) : ''}`;
      $('stat-net-sub').textContent = trades
        ? `${formatPct(data.total_net_pnl)} after fees, ${plural(trades, 'trade')}`
        : 'After fees';

      const dd = $('stat-drawdown');
      dd.textContent = trades ? formatR(-(data.max_drawdown_r || 0), 1) : '–';
      dd.className = `stat-value num ${data.max_drawdown_r ? 'is-short' : ''}`;

      $('stat-open').textContent = (data.pending_count || 0) + (data.open_count || 0);
      $('stat-open-sub').textContent = `${data.pending_count || 0} awaiting fill, ${data.open_count || 0} open, `
        + `${data.total_signals || 0} recorded (${data.long_signals || 0} long / ${data.short_signals || 0} short)`;
    } catch (e) {
      console.warn('Summary load failed', e);
    }
  }

  // Signals (active + history)
  function isNew(s) {
    const created = parseTime(s.created_at);
    return lastRunMs != null && created != null && created.getTime() >= lastRunMs;
  }

  function signalTags(s) {
    if (isNew(s)) return '<span class="tag tag-new">new</span>';
    return journalTagHtml(s.journal_status);
  }

  function emptySignalsText() {
    if (hasFilters()) return 'No signals match these filters.';
    if (activeTab === 'history') return 'No closed signals yet.';
    return `No trade is awaiting fill or open. Signals appear when RSI(${config.rsi_period}) crosses `
      + `${config.oversold} or ${config.overbought} on a closed candle.`;
  }

  function renderSignals(list) {
    if (list.length === 0) {
      setSignalsMessage(emptySignalsText());
      return;
    }
    shownSignals = list;

    tbody.innerHTML = list.map((s, i) => `
      <tr data-index="${i}" tabindex="0">
        <td><span class="sym">${escapeHtml(s.symbol)}</span>${signalTags(s)}</td>
        <td>${sideHtml(s.side)}</td>
        <td class="r num">${formatRsi(s.rsi)}</td>
        <td class="r num">${formatPrice(s.entry_price)}</td>
        <td>${outcomeHtml(s.status)}</td>
        <td class="r num">${resultHtml(s)}</td>
        <td class="num muted">${escapeHtml(formatWIB(s.signal_time))}</td>
      </tr>
    `).join('');

    signalCards.innerHTML = list.map((s, i) => `
      <div class="card" data-index="${i}" tabindex="0">
        <div class="card-top">
          <div><span class="sym">${escapeHtml(s.symbol)}</span> &nbsp;${sideHtml(s.side)}${signalTags(s)}</div>
          ${outcomeHtml(s.status)}
        </div>
        <dl class="card-levels">
          <div><dt>Entry (limit)</dt><dd class="num">${formatPrice(s.entry_price)}</dd></div>
          <div><dt>Stop</dt><dd class="num">${formatPrice(s.stop_loss)}</dd></div>
          <div><dt>Target</dt><dd class="num">${formatPrice(s.take_profit)}</dd></div>
        </dl>
        <div class="card-foot">
          <span class="card-time num">${escapeHtml(formatWIB(s.signal_time))} WIB, RSI ${formatRsi(s.rsi)}</span>
          ${s.r_multiple != null || s.pnl_percent != null ? `<span class="card-result num">${resultHtml(s)}</span>` : ''}
        </div>
      </div>
    `).join('');
  }

  function bindRowOpen(containers, pick) {
    const handler = (e) => {
      if (e.type === 'keydown' && e.key !== 'Enter') return;
      const el = e.target.closest('[data-index]');
      if (!el) return;
      const item = pick(Number(el.dataset.index));
      if (item) openSignal(item);
    };
    containers.forEach(el => {
      el.addEventListener('click', handler);
      el.addEventListener('keydown', handler);
    });
  }

  bindRowOpen([tbody, signalCards], i => shownSignals[i]);
  bindRowOpen([journalTbody, journalCards], i => shownJournal[i]);

  async function runNow() {
    btnScan.disabled = true;
    scanIcon.classList.add('on');
    scanLabel.textContent = 'Running…';
    showNotice('');

    try {
      const res = await authFetch('/api/cron/trigger', { method: 'POST' });
      if (!res.ok) throw new Error(await responseError(res));
      const result = (await res.json()).result || {};
      const failed = result.failed_symbols || [];
      if (failed.length) {
        showNotice(failed.length === result.symbols_scanned
          ? 'Could not load candles for any symbol. Check the server connection to Binance.'
          : `Could not load candles for ${failed.join(', ')}. Results exclude them.`);
      }
      const closed = (result.tp || 0) + (result.sl || 0) + (result.expired || 0) + (result.not_filled || 0);
      showToast(`Scanned ${(result.symbols_scanned || 0) - failed.length} symbols in ${result.execution_time}: `
        + `${plural(result.new_signals || 0, 'new signal')}, ${plural(closed, 'trade')} closed`);
      await checkHealth();
      pageIndex.active = 0;
      selectTab('active');
      await loadSummary();
    } catch (e) {
      showNotice(`Run failed: ${e.message}`);
    } finally {
      btnScan.disabled = false;
      scanIcon.classList.remove('on');
      scanLabel.textContent = 'Run now';
    }
  }

  function filterParams(base) {
    const params = new URLSearchParams(base);
    const search = filterSymbol.value.toUpperCase().trim();
    if (search) params.append('symbol', search);
    if (filterSide.value) params.append('side', filterSide.value);
    if (filterDate.value) params.append('date', filterDate.value);
    return params;
  }

  function hasFilters() {
    return Boolean(filterSymbol.value.trim() || filterSide.value || filterStatus.value || filterDate.value);
  }

  async function loadSignals() {
    const tab = activeTab;
    const request = ++listRequest;
    const statuses = TAB_STATUSES[tab].filter(st => !filterStatus.value || st === filterStatus.value);
    if (!statuses.length) {
      setCaption('');
      setSignalsMessage(`${outcomeOf(filterStatus.value).label} signals are listed under ${tab === 'active' ? 'History' : 'Active'}.`);
      return;
    }
    try {
      const data = await getJson(`/api/signals?${filterParams({ status: statuses.join(','), ...pageParams(tab) })}`);
      if (request !== listRequest || activeTab !== tab) return;
      const signals = data.signals || [];
      if (stepBackIfEmpty(tab, signals.length, data.total)) return;
      setCaption(signalsCaption(data.total, data.summary || {}));
      renderSignals(signals);
      renderPager(tab, data.total, signals.length);
    } catch (e) {
      if (request !== listRequest || activeTab !== tab) return;
      setCaption('');
      setSignalsMessage(`Could not load signals: ${e.message}`, true);
    }
  }

  function signalsCaption(total, sum) {
    if (!total) return '';
    const counts = [
      [sum.pending_count, 'awaiting fill'],
      [sum.open_count, 'open'],
      [sum.tp_hit_count, 'hit target'],
      [sum.sl_hit_count, 'stopped'],
      [sum.expired_count, 'expired'],
      [sum.not_filled_count, 'not filled'],
    ].filter(([n]) => n).map(([n, label]) => `${n} ${label}`);
    let text = `${plural(total, 'signal')}, newest first: ${counts.join(', ')}.`;
    if (sum.closed_trades) {
      text += ` Closed result: ${formatR(sum.total_r)} (${formatPct(sum.total_net_pnl)} after fees).`;
    }
    return text;
  }

  // Journal
  async function loadJournal() {
    const request = ++listRequest;
    const params = new URLSearchParams(pageParams('journal'));
    const search = filterSymbol.value.toUpperCase().trim();
    if (search) params.append('symbol', search);
    if (filterJournal.value) params.append('status', filterJournal.value);
    try {
      const data = await getJson(`/api/journal?${params}`);
      if (request !== listRequest || activeTab !== 'journal') return;
      const entries = data.entries || [];
      if (stepBackIfEmpty('journal', entries.length, data.total)) return;
      setCaption(journalCaption(data.summary || {}));
      renderJournal(entries, Boolean(search || filterJournal.value));
      if (entries.length) renderPager('journal', data.total, entries.length);
    } catch (e) {
      if (request !== listRequest || activeTab !== 'journal') return;
      setCaption('');
      setJournalMessage(`Could not load the journal: ${e.message}`, true);
    }
  }

  function journalCaption(sum) {
    if (!sum.taken && !sum.skipped) return '';
    let text = `${sum.taken} taken, ${sum.skipped} skipped.`;
    if (sum.closed_taken) {
      text += ` Your ${plural(sum.closed_taken, 'closed trade')}: ${formatR(sum.your_total_r)} (${formatPct(sum.your_total_pnl_percent)} after fees).`;
    }
    text += ` Tracked result of the signals you took: ${formatR(sum.signal_r_taken)}; of those you skipped: ${formatR(sum.signal_r_skipped)}.`;
    return text;
  }

  function journalResultHtml(j) {
    if (!j || j.status !== 'TAKEN') return '<span class="muted">–</span>';
    if (j.r_multiple == null) return '<span class="muted">open</span>';
    return `<span class="${signClass(j.r_multiple)}">${formatR(j.r_multiple)}</span> <span class="muted">${formatPct(j.pnl_percent)}</span>`;
  }

  function decisionLabel(j) {
    return j && j.status === 'TAKEN' ? 'Taken' : 'Skipped';
  }

  function renderJournal(list, filtered) {
    if (list.length === 0) {
      setJournalMessage(filtered
        ? 'No journal entries match these filters.'
        : 'Nothing recorded yet. Open a signal and use "Your trade" to note whether you took it.');
      return;
    }
    shownJournal = list;

    journalTbody.innerHTML = list.map((s, i) => `
      <tr data-index="${i}" tabindex="0">
        <td><span class="sym">${escapeHtml(s.symbol)}</span></td>
        <td>${sideHtml(s.side)}</td>
        <td>${decisionLabel(s.journal)}</td>
        <td class="r num">${formatPrice(s.journal && s.journal.entry_fill)}</td>
        <td class="r num">${formatPrice(s.journal && s.journal.exit_fill)}</td>
        <td class="r num">${journalResultHtml(s.journal)}</td>
        <td>${outcomeHtml(s.status)}</td>
        <td class="num muted">${escapeHtml(formatWIB(s.signal_time))}</td>
      </tr>
    `).join('');

    journalCards.innerHTML = list.map((s, i) => `
      <div class="card" data-index="${i}" tabindex="0">
        <div class="card-top">
          <div><span class="sym">${escapeHtml(s.symbol)}</span> &nbsp;${sideHtml(s.side)}<span class="tag">${decisionLabel(s.journal).toLowerCase()}</span></div>
          ${outcomeHtml(s.status)}
        </div>
        <dl class="card-levels">
          <div><dt>Entry fill</dt><dd class="num">${formatPrice(s.journal && s.journal.entry_fill)}</dd></div>
          <div><dt>Exit fill</dt><dd class="num">${formatPrice(s.journal && s.journal.exit_fill)}</dd></div>
          <div><dt>Your result</dt><dd class="num">${journalResultHtml(s.journal)}</dd></div>
        </dl>
        <div class="card-time num">${escapeHtml(formatWIB(s.signal_time))} WIB</div>
      </div>
    `).join('');
  }

  // Watchlist
  async function loadWatchlist() {
    try {
      const data = await getJson('/api/watchlist');
      watchlist = data.watchlist || [];
      watchlistNotice.hidden = watchlist.some(w => w.is_active);
      renderWatchlist();
    } catch (e) {
      watchlistList.innerHTML = `<li>${emptyHtml(`Could not load the watchlist: ${e.message}`, true)}</li>`;
    }
  }

  function renderWatchlist() {
    const active = watchlist.filter(w => w.is_active).length;
    wlCount.textContent = watchlist.length ? ` ${active}` : '';
    wlCount.title = `${active} active`;

    if (activeTab === 'watchlist') {
      setCaption(watchlist.length
        ? `${active} of ${watchlist.length} symbols are scanned. Turn a symbol off to skip it without removing it.`
        : '');
    }

    if (!watchlist.length) {
      watchlistList.innerHTML = `<li>${emptyHtml('The watchlist is empty. Add a futures symbol to start scanning it.')}</li>`;
      return;
    }

    watchlistList.innerHTML = watchlist.map(item => `
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
      </li>
    `).join('');
  }

  watchlistList.addEventListener('change', async (e) => {
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
    watchlistNotice.hidden = watchlist.some(w => w.is_active);
    renderWatchlist();
    await loadSummary();
  });

  watchlistList.addEventListener('click', async (e) => {
    const btn = e.target.closest('.wl-delete');
    if (!btn) return;
    const sym = btn.dataset.symbol;
    if (!confirm(`Remove ${sym} from the watchlist?`)) return;
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
    await loadSummary();
  });

  wlForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const sym = inputNewSymbol.value.trim().toUpperCase();
    if (!sym) return;
    btnAddSymbol.disabled = true;
    try {
      const res = await authFetch('/api/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: sym }),
      });
      if (!res.ok) throw new Error(await responseError(res));
      inputNewSymbol.value = '';
      showNotice('');
      showToast(`${sym} added`);
      await loadWatchlist();
      await loadSummary();
    } catch (err) {
      showNotice(`Could not add ${sym}: ${err.message}`);
    } finally {
      btnAddSymbol.disabled = false;
    }
  });

  // Tabs
  function reloadActiveList() {
    if (activeTab in TAB_STATUSES) return loadSignals();
    if (activeTab === 'journal') return loadJournal();
    return loadWatchlist();
  }

  function selectTab(tab) {
    activeTab = tab;
    tabs.forEach(t => t.setAttribute('aria-selected', String(t.dataset.tab === tab)));

    views.signals.hidden = !(tab in TAB_STATUSES);
    views.journal.hidden = tab !== 'journal';
    views.watchlist.hidden = tab !== 'watchlist';

    filters.hidden = tab === 'watchlist';
    filters.querySelectorAll('[data-tabs]').forEach(el => {
      el.hidden = !el.dataset.tabs.split(' ').includes(tab);
    });

    setCaption('');
    if (tab in TAB_STATUSES) {
      setSignalsMessage('Loading…');
      loadSignals();
    } else if (tab === 'journal') {
      setJournalMessage('Loading…');
      loadJournal();
    } else {
      renderWatchlist();
      loadWatchlist();
    }
  }

  tabs.forEach(t => t.addEventListener('click', () => {
    if (t.dataset.tab === activeTab) return;
    selectTab(t.dataset.tab);
    t.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }));

  function onFilterChanged() {
    // Filters are shared across tabs, so every list starts again from its first page
    Object.keys(pageIndex).forEach(tab => { pageIndex[tab] = 0; });
    reloadActiveList();
  }

  filterSymbol.addEventListener('input', debounce(onFilterChanged, 250));
  [filterDate, filterSide, filterStatus, filterJournal].forEach(el => el.addEventListener('change', onFilterChanged));

  // Signal detail
  function pctFrom(entry, level) {
    if (!entry || !level) return '';
    return `<small>${formatPct(((level - entry) / entry) * 100)}</small>`;
  }

  function detailSubtitle(sig) {
    return `Signal candle ${formatWIB(sig.signal_time)} WIB on the ${sig.timeframe} chart`;
  }

  function fillHtml(sig) {
    if (sig.fill_price != null) {
      return `${formatPrice(sig.fill_price)}<span class="hint">Filled ${escapeHtml(formatWIB(sig.fill_time))} WIB</span>`;
    }
    if (sig.status === 'NOT_FILLED') {
      return `<span class="muted">Not filled</span><span class="hint">Window ended ${escapeHtml(formatWIB(sig.exit_time))} WIB</span>`;
    }
    return `<span class="muted">Waiting</span><span class="hint">Fill by ${escapeHtml(formatWIB(sig.fill_deadline))} WIB, else cancelled</span>`;
  }

  function exitHtml(sig) {
    if (sig.exit_price != null) {
      return `${formatPrice(sig.exit_price)}<span class="hint">${escapeHtml(formatWIB(sig.exit_time))} WIB, `
        + `${formatR(sig.r_multiple)} (${formatPct(sig.pnl_percent)} after fees)</span>`;
    }
    if (sig.status === 'OPEN' && sig.hold_deadline) {
      return `<span class="muted">Open</span><span class="hint">Closes by ${escapeHtml(formatWIB(sig.hold_deadline))} WIB at the latest</span>`;
    }
    return '–';
  }

  function fillDetail(sig) {
    const entry = sig.entry_price;
    const isLong = sig.side === 'LONG';
    $('modal-title').innerHTML = `${escapeHtml(sig.symbol)} ${sideHtml(sig.side)}`;
    $('modal-time').textContent = detailSubtitle(sig);

    $('detail-entry').innerHTML = formatPrice(entry)
      + `<span class="hint">Signal candle ${isLong ? 'low' : 'high'}</span>`;
    $('detail-sl').innerHTML = formatPrice(sig.stop_loss) + pctFrom(entry, sig.stop_loss);
    $('detail-tp').innerHTML = formatPrice(sig.take_profit) + pctFrom(entry, sig.take_profit);
    $('detail-outcome').innerHTML = outcomeHtml(sig.status);

    $('detail-rsi-label').textContent = `RSI(${config.rsi_period})`;
    $('detail-rsi').innerHTML = formatRsi(sig.rsi) + `<span class="hint">Crossed ${isLong
      ? `below ${config.oversold}` : `above ${config.overbought}`} from ${formatRsi(sig.prev_rsi)}</span>`;
    const risk = Math.abs(entry - sig.stop_loss);
    $('detail-rr').textContent = risk > 0 ? `1:${Number((Math.abs(sig.take_profit - entry) / risk).toFixed(2))}` : '–';
    $('detail-fill').innerHTML = fillHtml(sig);
    $('detail-exit').innerHTML = exitHtml(sig);
  }

  async function openSignal(sig) {
    loadChartLibrary().catch(() => {});
    currentSignal = sig;
    destroyChart();
    showChartMessage('<span class="spinner on"></span> Loading candles…');
    fillDetail(sig);
    renderJournalForm(sig);
    if (!modal.open) modal.showModal();
    try {
      const fresh = await getJson(`/api/signals/${encodeURIComponent(sig.id)}`);
      if (currentSignal !== sig) return;
      currentSignal = { ...sig, ...fresh };
      fillDetail(currentSignal);
      renderJournalForm(currentSignal);
      renderChart(currentSignal);
    } catch (e) {
      if (currentSignal !== sig) return;
      showChartMessage(escapeHtml(`Could not load this signal: ${e.message}`), true);
    }
  }

  // Journal form
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

  function updateJournalPreview() {
    const status = selectedJournalStatus();
    journalForm.querySelectorAll('[data-taken]').forEach(el => { el.hidden = status !== 'TAKEN'; });
    if (status !== 'TAKEN' || !currentSignal) {
      journalResult.textContent = '';
      return;
    }
    const entry = parseFill(journalEntry);
    const exit = parseFill(journalExit);
    if (Number.isNaN(entry) || Number.isNaN(exit)) {
      journalResult.innerHTML = '<span class="is-short">Prices must be positive numbers</span>';
      return;
    }
    const res = entry && exit ? tradeReturn(currentSignal, entry, exit) : null;
    if (res) {
      journalResult.innerHTML = `Your result: <span class="${signClass(res.pnl)}">${formatR(res.r)}</span> (${formatPct(res.pnl)} after fees)`;
    } else {
      journalResult.textContent = entry ? 'Add the exit fill once the trade is closed.' : '';
    }
  }

  function renderJournalForm(sig) {
    const j = sig.journal || null;
    const status = j ? j.status : '';
    journalForm.querySelectorAll('input[name="journal-status"]').forEach(r => { r.checked = r.value === status; });
    journalEntry.value = j && j.entry_fill != null ? j.entry_fill : '';
    journalExit.value = j && j.exit_fill != null ? j.exit_fill : '';
    journalNotes.value = j ? j.notes || '' : '';
    journalEntry.placeholder = `Signal ${sig.fill_price != null ? 'fill' : 'limit'} ${formatPrice(sig.fill_price ?? sig.entry_price)}`;
    updateJournalPreview();
  }

  journalForm.addEventListener('input', updateJournalPreview);
  journalForm.addEventListener('change', updateJournalPreview);

  async function saveJournal(sig, status, entry, exit) {
    const url = `/api/signals/${encodeURIComponent(sig.id)}/journal`;
    if (!status) return authFetch(url, { method: 'DELETE' });
    return authFetch(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        status,
        entry_fill: status === 'TAKEN' ? entry : null,
        exit_fill: status === 'TAKEN' ? exit : null,
        notes: journalNotes.value.trim(),
      }),
    });
  }

  journalForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const sig = currentSignal;
    if (!sig) return;
    const status = selectedJournalStatus();
    const entry = parseFill(journalEntry);
    const exit = parseFill(journalExit);
    if (status === 'TAKEN' && (Number.isNaN(entry) || Number.isNaN(exit))) return;
    if (status === 'TAKEN' && exit && !entry) {
      journalResult.innerHTML = '<span class="is-short">Add the entry fill first</span>';
      return;
    }
    if (!status && !sig.journal) {
      showToast('Nothing to save');
      return;
    }

    journalSave.disabled = true;
    try {
      const res = await saveJournal(sig, status, entry, exit);
      if (!res.ok) throw new Error(await responseError(res));
      const saved = status ? await res.json() : null;
      sig.journal = saved ? saved.journal : null;
      sig.journal_status = sig.journal ? sig.journal.status : null;
      shownSignals.forEach(s => {
        if (s.id === sig.id) s.journal_status = sig.journal_status;
      });
      renderJournalForm(sig);
      showToast(status ? 'Saved to your journal' : 'Removed from your journal');
      if (activeTab !== 'watchlist') reloadActiveList();
    } catch (err) {
      journalResult.innerHTML = `<span class="is-short">${escapeHtml(`Could not save: ${err.message}`)}</span>`;
    } finally {
      journalSave.disabled = false;
    }
  });

  // Chart
  let chartLibPromise = null;

  // The chart library is only needed in the signal dialog, so it is fetched the first time it is wanted
  function loadChartLibrary() {
    if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
    if (!chartLibPromise) {
      chartLibPromise = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = CHART_LIB_URL;
        script.async = true;
        script.onload = () => (window.LightweightCharts
          ? resolve(window.LightweightCharts)
          : reject(new Error('chart library did not initialise')));
        script.onerror = () => {
          script.remove();
          reject(new Error('chart library failed to load'));
        };
        document.head.appendChild(script);
      }).catch((err) => {
        chartLibPromise = null; // allow a retry on the next dialog
        throw err;
      });
    }
    return chartLibPromise;
  }

  // Start the download as soon as the pointer or focus reaches a row that opens a chart
  [tbody, signalCards, journalTbody, journalCards].forEach(el => {
    ['pointerover', 'focusin'].forEach(type => el.addEventListener(type, () => {
      loadChartLibrary().catch(() => {});
    }, { once: true }));
  });

  function destroyChart() {
    chartRequest += 1;
    if (chart) {
      chart.remove();
      chart = null;
    }
  }

  function showChartMessage(message, isError = false) {
    chartViewport.innerHTML = `<div class="chart-msg${isError ? ' err' : ''}">${message}</div>`;
  }

  async function renderChart(sig) {
    destroyChart();
    const request = chartRequest;
    showChartMessage('<span class="spinner on"></span> Loading candles…');

    try {
      const res = await fetch(`/api/signals/${encodeURIComponent(sig.id)}/chart`);
      if (request !== chartRequest) return;
      if (!res.ok) {
        showChartMessage(escapeHtml(`Chart unavailable: ${await responseError(res)}`), true);
        return;
      }
      const payload = await res.json();
      if (request !== chartRequest) return;
      if (!Array.isArray(payload.candles) || payload.candles.length === 0) {
        showChartMessage('No candle data for this symbol.');
        return;
      }
      try {
        await loadChartLibrary();
      } catch {
        if (request === chartRequest) showChartMessage('The chart library could not be loaded. Check your connection and reopen the signal.', true);
        return;
      }
      if (request !== chartRequest) return;
      chartViewport.innerHTML = '';

      const css = getComputedStyle(document.documentElement);
      const color = (name) => css.getPropertyValue(name).trim();
      const longColor = color('--long');
      const shortColor = color('--short');
      const entryColor = color('--entry');

      chart = LightweightCharts.createChart(chartViewport, {
        autoSize: true,
        layout: {
          background: { color: color('--surface') },
          textColor: color('--text-3'),
          fontFamily: color('--font-mono'),
          fontSize: 11,
        },
        grid: {
          vertLines: { color: color('--line') },
          horzLines: { color: color('--line') },
        },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        rightPriceScale: { borderColor: color('--line') },
        timeScale: {
          borderColor: color('--line'),
          timeVisible: true,
          secondsVisible: false,
        },
        localization: { dateFormat: 'yyyy-MM-dd' },
      });

      const candleSeries = chart.addCandlestickSeries({
        upColor: longColor,
        downColor: shortColor,
        borderVisible: false,
        wickUpColor: longColor,
        wickDownColor: shortColor,
      });

      // Price on the top 70%, RSI in its own pane below
      candleSeries.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: 0.3 } });
      const rsiSeries = chart.addLineSeries({
        priceScaleId: 'rsi',
        color: color('--text-2'),
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
      });
      chart.priceScale('rsi').applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });

      // Lightweight Charts renders timestamps as UTC; shift them so the axis reads in WIB
      const UTC7_OFFSET_SEC = 7 * 3600;
      const candles = payload.candles.map(c => ({ ...c, time: c.time + UTC7_OFFSET_SEC }));
      candleSeries.setData(candles);
      rsiSeries.setData((payload.rsi || []).map(p => ({ time: p.time + UTC7_OFFSET_SEC, value: p.value })));
      [config.oversold, config.overbought].forEach(price => rsiSeries.createPriceLine({
        price,
        color: color('--text-3'),
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true,
        title: '',
      }));

      // Candle containing a moment; an expiry is stamped at the close, so it belongs to the candle before
      const intervalSec = intervalMs(sig.timeframe) / 1000;
      const candleAt = (ms, closeStamp = false) => {
        if (ms == null || !intervalSec) return null;
        const ts = Math.floor(ms / 1000) + UTC7_OFFSET_SEC - (closeStamp ? 1 : 0);
        return candles.find(c => c.time <= ts && ts < c.time + intervalSec) || null;
      };

      const markers = [];
      const isLong = sig.side === 'LONG';
      const addMarker = (ms, closeStamp, marker) => {
        const c = candleAt(ms, closeStamp);
        if (c) markers.push({ time: c.time, ...marker });
      };
      addMarker(sig.signal_candle_ms, false, {
        position: isLong ? 'belowBar' : 'aboveBar',
        color: isLong ? longColor : shortColor,
        shape: isLong ? 'arrowUp' : 'arrowDown',
        text: 'Signal',
      });
      if (sig.fill_ms != null) {
        addMarker(sig.fill_ms, false, {
          position: isLong ? 'belowBar' : 'aboveBar',
          color: entryColor,
          shape: 'circle',
          text: 'Fill',
        });
      }
      if (sig.exit_price != null) {
        addMarker(sig.closed_ms, sig.status === 'EXPIRED', {
          position: sig.exit_price >= sig.fill_price ? 'aboveBar' : 'belowBar',
          color: (sig.r_multiple ?? sig.pnl_percent) >= 0 ? longColor : shortColor,
          shape: 'circle',
          text: 'Exit',
        });
      }
      if (markers.length) candleSeries.setMarkers(markers.sort((a, b) => a.time - b.time));

      // A limit that never filled had no position, so it gets no levels; a resting limit is dashed
      if (sig.status !== 'NOT_FILLED') {
        const lineStyle = sig.status === 'PENDING' ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid;
        [
          [sig.entry_price, entryColor, 'Entry'],
          [sig.take_profit, longColor, 'TP'],
          [sig.stop_loss, shortColor, 'SL'],
        ].forEach(([price, lineColor, label]) => {
          if (!price) return;
          candleSeries.createPriceLine({
            price,
            color: lineColor,
            lineWidth: 1,
            lineStyle,
            axisLabelVisible: true,
            title: label,
          });
        });
      }

      chart.timeScale().fitContent();
    } catch (e) {
      if (request !== chartRequest) return;
      console.error('Failed to render chart:', e);
      showChartMessage('Could not load candles from Binance.', true);
    }
  }

  modalClose.addEventListener('click', () => modal.close());
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.close(); });
  modal.addEventListener('close', () => {
    destroyChart();
    currentSignal = null;
  });

  // Initialization
  btnScan.addEventListener('click', runNow);
  // The "new" tag needs the last run time, which may arrive after the first list
  checkHealth().then(() => { if (shownSignals.length) renderSignals(shownSignals); });
  setInterval(checkHealth, 60000);
  loadConfig();
  loadSummary();
  loadWatchlist();
  selectTab('active');
});
