document.addEventListener('DOMContentLoaded', () => {
  const TZ = 'Asia/Jakarta';
  const API_KEY_STORAGE = 'crossline_api_key';
  const LEGACY_API_KEY_STORAGE = 'tokyo_api_key';

  let activeTab = 'scan';
  let liveSignals = null;
  let liveMeta = null;
  let liveGeneration = 0;
  let listRequest = 0;
  let shownSignals = [];
  let watchlist = [];
  let chart = null;
  let chartRequest = 0;

  const $ = (id) => document.getElementById(id);

  // Signals
  const tbody = $('signals-tbody');
  const signalCards = $('signals-cards');
  const btnScan = $('btn-scan');
  const scanLabel = $('scan-label');
  const scanIcon = $('scan-icon');
  const btnEval = $('btn-eval');
  const caption = $('caption');
  const actionNotice = $('action-notice');

  // Tabs & views
  const tabs = Array.from(document.querySelectorAll('.tab'));
  const viewSignals = $('view-signals');
  const viewProfitLoss = $('view-profitloss');
  const viewWatchlist = $('view-watchlist');

  // Closed trades
  const pnlTbody = $('pnl-tbody');
  const pnlCards = $('pnl-cards');
  const statTotalPnl = $('stat-total-pnl');
  const statPnlSub = $('stat-pnl-sub');

  // Filters
  const filters = $('filters');
  const filterSymbol = $('filter-symbol');
  const filterDate = $('filter-date');
  const filterSide = $('filter-side');
  const filterResult = $('filter-result');
  const pendingOption = filterResult.querySelector('option[value="PENDING"]');

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

  function showNotice(message) {
    actionNotice.textContent = message || '';
    actionNotice.hidden = !message;
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

  function formatWIB(isoStr) {
    if (!isoStr) return '–';
    const dt = new Date(isoStr);
    if (isNaN(dt.getTime())) return String(isoStr);
    const p = Object.fromEntries(dateTimeFmt.formatToParts(dt).map(x => [x.type, x.value]));
    return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
  }

  function formatTimeOnlyWIB(isoStr) {
    if (!isoStr) return '--:--';
    const dt = new Date(isoStr);
    return isNaN(dt.getTime()) ? '--:--' : hourFmt.format(dt);
  }

  function formatPrice(val) {
    if (val == null || isNaN(val)) return '–';
    const num = Number(val);
    if (num >= 1000) return num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (num >= 1) return num.toFixed(4);
    return num.toFixed(6);
  }

  function formatPct(val) {
    const num = Number(val) || 0;
    return `${num > 0 ? '+' : ''}${num.toFixed(2)}%`;
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

  function outcomeOf(status) {
    if (status === 'TP1_HIT' || status === 'PROFIT') return { cls: 'win', label: 'Target hit' };
    if (status === 'SL_HIT' || status === 'LOSS') return { cls: 'loss', label: 'Stopped out' };
    return { cls: '', label: 'Open' };
  }

  function outcomeHtml(status) {
    const o = outcomeOf(status);
    return `<span class="outcome ${o.cls}">${o.label}</span>`;
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

  function setSignalsMessage(message, isError = false) {
    const html = emptyHtml(message, isError);
    tbody.innerHTML = `<tr><td colspan="7">${html}</td></tr>`;
    signalCards.innerHTML = html;
    shownSignals = [];
  }

  function setPnlMessage(message, isError = false) {
    const html = emptyHtml(message, isError);
    pnlTbody.innerHTML = `<tr><td colspan="8">${html}</td></tr>`;
    pnlCards.innerHTML = html;
  }

  function setCaption(text) {
    caption.textContent = text || '';
  }

  // Clock
  function tickClock() {
    $('clock').textContent = clockFmt.format(new Date());
  }
  tickClock();
  setInterval(tickClock, 1000);

  // Health & cron
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
    } catch {
      dot.className = 'dot err';
      text.textContent = 'Offline';
      cronText.textContent = '--:--';
    }
  }

  // Summary
  function setNetPnl(value, closedCount) {
    const pnl = Number(value) || 0;
    statTotalPnl.textContent = formatPct(pnl);
    statTotalPnl.className = `stat-value num ${pnl > 0 ? 'is-long' : pnl < 0 ? 'is-short' : ''}`;
    statPnlSub.textContent = `${closedCount} closed ${closedCount === 1 ? 'trade' : 'trades'}`;
  }

  async function loadSummary() {
    try {
      const data = await getJson('/api/summary');
      const wins = data.tp1_hit_count || 0;
      const losses = data.sl_hit_count || 0;
      const decided = wins + losses;

      $('stat-winrate').textContent = decided ? `${Number(data.winrate || 0).toFixed(1)}%` : '–';
      $('bar-wins').style.width = decided ? `${(wins / decided) * 100}%` : '0';
      $('bar-losses').style.width = decided ? `${(losses / decided) * 100}%` : '0';
      $('stat-win-ratio').textContent = `${wins} won, ${losses} lost`;

      $('stat-total-signals').textContent = data.total_signals || 0;
      $('stat-active-pending').textContent = `${data.pending_count || 0} still open`;
      $('stat-long').textContent = data.long_signals || 0;
      $('stat-short').textContent = data.short_signals || 0;
      if (data.avg_rr) $('stat-rr').textContent = `Target at ${Number(data.avg_rr).toFixed(1)}× risk`;

      setNetPnl(data.total_net_pnl, data.closed_trades ?? decided);
    } catch (e) {
      console.warn('Summary load failed', e);
    }
  }

  // Signals (latest scan + history)
  function filterSignals(list) {
    const search = filterSymbol.value.toUpperCase().trim();
    const side = filterSide.value;
    const result = filterResult.value;
    const dateVal = filterDate.value;

    return list.filter(s => {
      if (search && !s.symbol.includes(search)) return false;
      if (side && s.side !== side) return false;
      if (result === 'PROFIT' && s.win_status !== 'TP1_HIT') return false;
      if (result === 'LOSS' && s.win_status !== 'SL_HIT') return false;
      if (result === 'PENDING' && s.win_status !== 'PENDING') return false;
      if (dateVal && !(s.cross_time && s.cross_time.startsWith(dateVal))) return false;
      return true;
    });
  }

  function renderSignals(list) {
    shownSignals = filterSignals(list);

    if (shownSignals.length === 0) {
      setSignalsMessage(list.length ? 'No signals match these filters.' : 'No crossovers found.');
      return;
    }

    const showNew = activeTab === 'scan';

    tbody.innerHTML = shownSignals.map((s, i) => `
      <tr data-index="${i}" tabindex="0">
        <td><span class="sym">${escapeHtml(s.symbol)}</span>${showNew && s.is_new ? '<span class="tag-new">new</span>' : ''}</td>
        <td>${sideHtml(s.side)}</td>
        <td class="r num">${formatPrice(s.entry_price || s.cross_price)}</td>
        <td class="r num">${formatPrice(s.stop_loss)}</td>
        <td class="r num">${formatPrice(s.tp1)}</td>
        <td>${outcomeHtml(s.win_status)}</td>
        <td class="num muted">${escapeHtml(formatWIB(s.cross_time))}</td>
      </tr>
    `).join('');

    signalCards.innerHTML = shownSignals.map((s, i) => `
      <div class="card" data-index="${i}" tabindex="0">
        <div class="card-top">
          <div><span class="sym">${escapeHtml(s.symbol)}</span> &nbsp;${sideHtml(s.side)}${showNew && s.is_new ? '<span class="tag-new">new</span>' : ''}</div>
          ${outcomeHtml(s.win_status)}
        </div>
        <dl class="card-levels">
          <div><dt>Entry</dt><dd class="num">${formatPrice(s.entry_price || s.cross_price)}</dd></div>
          <div><dt>Stop</dt><dd class="num">${formatPrice(s.stop_loss)}</dd></div>
          <div><dt>Target</dt><dd class="num">${formatPrice(s.tp1)}</dd></div>
        </dl>
        <div class="card-time num">${escapeHtml(formatWIB(s.cross_time))} WIB</div>
      </div>
    `).join('');
  }

  function onSignalActivate(e) {
    if (e.type === 'keydown' && e.key !== 'Enter') return;
    const el = e.target.closest('[data-index]');
    if (!el) return;
    const sig = shownSignals[Number(el.dataset.index)];
    if (sig) openChartModal(sig);
  }

  [tbody, signalCards].forEach(el => {
    el.addEventListener('click', onSignalActivate);
    el.addEventListener('keydown', onSignalActivate);
  });

  function scanCaption() {
    if (!liveMeta) return 'No scan has run in this session. The scheduled scan runs every 2 hours; use Run scan to check now.';
    const count = liveSignals.length;
    return `Scanned ${liveMeta.total_scanned} symbols in ${liveMeta.execution_time} at ${liveMeta.at} WIB. `
      + `${count} ${count === 1 ? 'crossover' : 'crossovers'} in the last ${liveMeta.lookback} bars, ${liveMeta.new_signals_count} new.`;
  }

  function renderScanView() {
    if (activeTab !== 'scan') return;
    setCaption(scanCaption());
    if (liveSignals === null) {
      setSignalsMessage('Results from your next scan will appear here.');
      return;
    }
    renderSignals(liveSignals);
  }

  async function refreshLiveStatuses() {
    if (!liveSignals) return false;
    const generation = liveGeneration;
    const tracked = liveSignals.filter(s => s.id);
    if (!tracked.length) return false;
    const updates = await Promise.all(tracked.map(async (s) => {
      try {
        const res = await fetch(`/api/signals/${encodeURIComponent(s.id)}`);
        return res.ok ? await res.json() : null;
      } catch (e) {
        return null;
      }
    }));
    if (generation !== liveGeneration) return false;
    updates.forEach((row, idx) => {
      if (row && row.win_status) tracked[idx].win_status = row.win_status;
    });
    return true;
  }

  async function refreshLiveView() {
    const applied = await refreshLiveStatuses();
    if (applied && activeTab === 'scan') renderScanView();
    return applied;
  }

  function scheduleLiveRefresh() {
    const generation = liveGeneration;
    if (!(liveSignals || []).some(s => s.id && s.win_status === 'PENDING')) return;
    [5000, 15000].forEach((delay) => {
      setTimeout(async () => {
        if (generation !== liveGeneration) return;
        if (await refreshLiveView()) await loadSummary();
      }, delay);
    });
  }

  async function runScan() {
    const lookback = $('select-lookback').value;
    const includeCurrent = $('select-forming').value;

    liveGeneration += 1;
    btnScan.disabled = true;
    scanIcon.classList.add('on');
    scanLabel.textContent = 'Scanning…';
    showNotice('');

    try {
      const params = new URLSearchParams({
        timeframe: '2h',
        lookback: lookback,
        include_current: includeCurrent,
      });

      const res = await authFetch(`/api/macd-scan?${params.toString()}`);
      if (!res.ok) throw new Error(await responseError(res));
      const data = await res.json();
      liveSignals = data.signals || [];
      liveMeta = {
        total_scanned: data.total_scanned,
        execution_time: data.execution_time,
        lookback: data.lookback,
        new_signals_count: data.new_signals_count,
        at: hourFmt.format(new Date()),
      };
      selectTab('scan');
      scheduleLiveRefresh();
      await loadSummary();
    } catch (e) {
      selectTab('scan');
      setSignalsMessage(`Scan failed: ${e.message}`, true);
    } finally {
      btnScan.disabled = false;
      scanIcon.classList.remove('on');
      scanLabel.textContent = 'Run scan';
    }
  }

  function filterParams(base) {
    const params = new URLSearchParams(base);
    const search = filterSymbol.value.toUpperCase().trim();
    if (search) params.append('symbol', search);
    if (filterSide.value) params.append('direction', filterSide.value);
    if (filterResult.value) params.append('result', filterResult.value);
    if (filterDate.value) params.append('date', filterDate.value);
    return params;
  }

  function hasFilters() {
    return Boolean(filterSymbol.value.trim() || filterSide.value || filterResult.value || filterDate.value);
  }

  async function loadHistory() {
    const request = ++listRequest;
    try {
      const data = await getJson(`/api/signals?${filterParams({ timeframe: '2h', limit: '100' })}`);
      if (request !== listRequest || activeTab !== 'history') return;
      const signals = data.signals || [];
      setCaption(data.total > signals.length
        ? `Showing the latest ${signals.length} of ${data.total} signals.`
        : `${data.total} ${data.total === 1 ? 'signal' : 'signals'}.`);
      renderSignals(signals);
    } catch (e) {
      if (request !== listRequest || activeTab !== 'history') return;
      setCaption('');
      setSignalsMessage(`Could not load signal history: ${e.message}`, true);
    }
  }

  // Closed trades
  async function loadProfitLossHistory() {
    const request = ++listRequest;
    try {
      const data = await getJson(`/api/profitloss-history?${filterParams({ limit: '100' })}`);
      if (request !== listRequest || activeTab !== 'profitloss') return;
      const history = data.history || [];
      const sum = data.summary || {};

      if (!hasFilters() && sum.total_pnl_percent !== undefined) {
        setNetPnl(sum.total_pnl_percent, sum.total_trades || 0);
      }

      setCaption(sum.total_trades
        ? `${sum.total_trades} trades, ${sum.profit_count} won and ${sum.loss_count} lost. Net ${formatPct(sum.total_pnl_percent)}.`
        : '');
      renderProfitLossHistory(history);
    } catch (e) {
      if (request !== listRequest || activeTab !== 'profitloss') return;
      setCaption('');
      setPnlMessage(`Could not load closed trades: ${e.message}`, true);
    }
  }

  function renderProfitLossHistory(list) {
    if (list.length === 0) {
      setPnlMessage(hasFilters()
        ? 'No closed trades match these filters.'
        : 'No trades have closed yet. A trade closes when price reaches its target or stop.');
      return;
    }

    const pnlCls = (item) => item.pnl_percent > 0 ? 'is-long' : item.pnl_percent < 0 ? 'is-short' : '';

    pnlTbody.innerHTML = list.map(item => `
      <tr>
        <td><span class="sym">${escapeHtml(item.symbol)}</span></td>
        <td>${sideHtml(item.direction)}</td>
        <td class="r num">${formatPrice(item.entry_price)}</td>
        <td class="r num">${formatPrice(item.exit_price)}</td>
        <td>${outcomeHtml(item.result)}</td>
        <td class="r num ${pnlCls(item)}">${formatPct(item.pnl_percent)}</td>
        <td class="num muted">${escapeHtml(formatWIB(item.cross_time))}</td>
        <td class="num muted">${escapeHtml(formatWIB(item.exit_time))}</td>
      </tr>
    `).join('');

    pnlCards.innerHTML = list.map(item => `
      <div class="card">
        <div class="card-top">
          <div><span class="sym">${escapeHtml(item.symbol)}</span> &nbsp;${sideHtml(item.direction)}</div>
          <span class="num ${pnlCls(item)}">${formatPct(item.pnl_percent)}</span>
        </div>
        <dl class="card-levels">
          <div><dt>Entry</dt><dd class="num">${formatPrice(item.entry_price)}</dd></div>
          <div><dt>Exit</dt><dd class="num">${formatPrice(item.exit_price)}</dd></div>
          <div><dt>Result</dt><dd>${outcomeHtml(item.result)}</dd></div>
        </dl>
        <div class="card-time num">Closed ${escapeHtml(formatWIB(item.exit_time))} WIB</div>
      </div>
    `).join('');
  }

  // Watchlist
  async function loadWatchlist() {
    try {
      const data = await getJson('/api/watchlist');
      watchlist = data.watchlist || [];
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
      await loadWatchlist();
      await loadSummary();
    } catch (err) {
      showNotice(`Could not add ${sym}: ${err.message}`);
    } finally {
      btnAddSymbol.disabled = false;
    }
  });

  // Evaluate trade outcomes
  btnEval.addEventListener('click', async () => {
    btnEval.disabled = true;
    btnEval.textContent = 'Evaluating…';
    try {
      const res = await authFetch('/api/evaluate', { method: 'POST' });
      if (!res.ok) throw new Error(await responseError(res));
      showNotice('');
      await loadSummary();
      if (activeTab === 'history') {
        await loadHistory();
      } else if (activeTab === 'profitloss') {
        await loadProfitLossHistory();
      } else {
        await refreshLiveView();
      }
    } catch (e) {
      showNotice(`Evaluation failed: ${e.message}`);
    } finally {
      btnEval.disabled = false;
      btnEval.textContent = 'Evaluate outcomes';
    }
  });

  // Tabs
  function selectTab(tab) {
    activeTab = tab;
    tabs.forEach(t => t.setAttribute('aria-selected', String(t.dataset.tab === tab)));

    viewSignals.hidden = !(tab === 'scan' || tab === 'history');
    viewProfitLoss.hidden = tab !== 'profitloss';
    viewWatchlist.hidden = tab !== 'watchlist';
    filters.hidden = tab === 'watchlist';

    pendingOption.disabled = tab === 'profitloss';
    if (tab === 'profitloss' && filterResult.value === 'PENDING') filterResult.value = '';

    setCaption('');
    if (tab === 'scan') {
      renderScanView();
    } else if (tab === 'history') {
      setSignalsMessage('Loading…');
      loadHistory();
    } else if (tab === 'profitloss') {
      setPnlMessage('Loading…');
      loadProfitLossHistory();
    } else {
      renderWatchlist();
      loadWatchlist();
    }
  }

  tabs.forEach(t => t.addEventListener('click', async () => {
    if (t.dataset.tab === activeTab) return;
    selectTab(t.dataset.tab);
    if (t.dataset.tab === 'scan') await refreshLiveView();
  }));

  function onFilterChanged() {
    if (activeTab === 'scan') {
      renderScanView();
    } else if (activeTab === 'history') {
      loadHistory();
    } else if (activeTab === 'profitloss') {
      loadProfitLossHistory();
    }
  }

  filterSymbol.addEventListener('input', debounce(onFilterChanged, 250));
  filterDate.addEventListener('change', onFilterChanged);
  filterSide.addEventListener('change', onFilterChanged);
  filterResult.addEventListener('change', onFilterChanged);

  // Signal detail
  function pctFrom(entry, level) {
    if (!entry || !level) return '';
    return `<small>${formatPct(((level - entry) / entry) * 100)}</small>`;
  }

  function openChartModal(sig) {
    const entry = sig.entry_price || sig.cross_price;
    const title = $('modal-title');
    title.innerHTML = `${escapeHtml(sig.symbol)} ${sideHtml(sig.side)}`;
    $('modal-time').textContent = `Crossed ${formatWIB(sig.cross_time)} WIB on the 2H chart, ${sig.confirmed === false ? 'candle still forming' : 'closed candle'}`;

    $('detail-entry').textContent = formatPrice(entry);
    $('detail-sl').innerHTML = formatPrice(sig.stop_loss) + pctFrom(entry, sig.stop_loss);
    $('detail-tp').innerHTML = formatPrice(sig.tp1) + pctFrom(entry, sig.tp1);
    $('detail-rr').textContent = `1:${sig.risk_reward_ratio || 1.5}`;

    const fmt4 = (v) => (v != null ? Number(v).toFixed(4) : '–');
    $('detail-macd').textContent = fmt4(sig.macd);
    $('detail-signal').textContent = fmt4(sig.signal);
    const hist = $('detail-hist');
    hist.textContent = fmt4(sig.histogram);
    hist.className = `num ${sig.histogram == null ? '' : sig.histogram >= 0 ? 'is-long' : 'is-short'}`;
    $('detail-outcome').innerHTML = outcomeHtml(sig.win_status);

    if (!modal.open) modal.showModal();
    renderChart(sig);
  }

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
      const res = await fetch(`/api/klines?symbol=${encodeURIComponent(sig.symbol)}&limit=80`);
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
      if (!window.LightweightCharts) {
        showChartMessage('The charting library failed to load.', true);
        return;
      }
      chartViewport.innerHTML = '';

      const css = getComputedStyle(document.documentElement);
      const color = (name) => css.getPropertyValue(name).trim();
      const longColor = color('--long');
      const shortColor = color('--short');

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

      // Lightweight Charts renders timestamps as UTC; shift them so the axis reads in WIB
      const UTC7_OFFSET_SEC = 7 * 3600;
      const candles = payload.candles.map(c => ({ ...c, time: c.time + UTC7_OFFSET_SEC }));
      candleSeries.setData(candles);

      const crossDate = sig.cross_time ? new Date(sig.cross_time) : null;
      if (crossDate && !isNaN(crossDate.getTime())) {
        const crossTsSec = Math.floor(crossDate.getTime() / 1000) + UTC7_OFFSET_SEC;
        const matchingCandle = candles.find(c => Math.abs(c.time - crossTsSec) < 3600);
        if (matchingCandle) {
          const isLong = sig.side === 'LONG';
          candleSeries.setMarkers([{
            time: matchingCandle.time,
            position: isLong ? 'belowBar' : 'aboveBar',
            color: isLong ? longColor : shortColor,
            shape: isLong ? 'arrowUp' : 'arrowDown',
            text: 'Cross',
          }]);
        }
      }

      const lines = [
        [sig.entry_price, color('--entry'), LightweightCharts.LineStyle.Solid, 'Entry'],
        [sig.tp1, longColor, LightweightCharts.LineStyle.Dashed, 'TP1'],
        [sig.stop_loss, shortColor, LightweightCharts.LineStyle.Dashed, 'Stop'],
      ];
      lines.forEach(([price, lineColor, lineStyle, label]) => {
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

      chart.timeScale().fitContent();
    } catch (e) {
      if (request !== chartRequest) return;
      console.error('Failed to render chart:', e);
      showChartMessage('Could not load candles from Binance.', true);
    }
  }

  modalClose.addEventListener('click', () => modal.close());
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.close(); });
  modal.addEventListener('close', destroyChart);

  // Initialization
  btnScan.addEventListener('click', runScan);
  checkHealth();
  setInterval(checkHealth, 60000);
  loadSummary();
  loadWatchlist();
  if (location.pathname === '/history') selectTab('history');
  renderScanView();
});
