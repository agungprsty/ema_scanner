document.addEventListener('DOMContentLoaded', () => {
  // One-time cleanup of legacy API key storage
  try {
    localStorage.removeItem('crossline_api_key');
    localStorage.removeItem('tokyo_api_key');
  } catch {
    /* storage unavailable */
  }
  const PAGE_SIZE = 20;
  const TAB_STATUSES = {
    active: ['PENDING', 'OPEN'],
    history: ['TP_HIT', 'SL_HIT', 'EXPIRED', 'NOT_FILLED'],
  };

  let activeTab = 'active';
  let lastRunMs = null;
  let listRequest = 0;
  let shownSignals = [];
  let shownJournal = [];
  let watchlist = [];
  const pageIndex = { active: 0, history: 0, journal: 0 };

  // Signals
  const tbody = $('signals-tbody');
  const signalCards = $('signals-cards');
  const btnScan = $('btn-scan');
  const scanLabel = $('scan-label');
  const scanIcon = $('scan-icon');
  const caption = $('caption');
  const actionNotice = $('action-notice');
  const watchlistNotice = $('watchlist-notice');

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

  function showNotice(message) {
    actionNotice.textContent = message || '';
    actionNotice.hidden = !message;
  }

  const clockFmt = new Intl.DateTimeFormat('en-GB', {
    timeZone: TZ,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  });

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
    return { page: String(pageIndex[tab] + 1), per_page: String(PAGE_SIZE) };
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
      const { data } = await getJson('/api/health');
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
      await loadStrategyConfig();
      $('brand-meta').textContent = `Supertrend ${config.supertrend_atr_period}/${config.supertrend_factor} · EMA ${config.ema_period}`
        + ` · RSI ${config.rsi_period} · ${config.timeframe} · Binance USDⓈ-M futures`;
    } catch {
      // keep defaults
    }
  }

  // Summary
  async function loadSummary() {
    try {
      const { data } = await getJson('/api/summary');
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
    return `No trade is awaiting fill or open. Signals appear when Supertrend flips on a closed candle `
      + `with the close on the trend side of EMA ${config.ema_period} and RSI(${config.rsi_period}) between `
      + `${config.rsi_midline} and ${config.rsi_long_max} (long) or ${config.rsi_short_min} and ${config.rsi_midline} (short).`;
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

  // Rows open the signal's own page; Ctrl/Cmd-click or a middle click opens it in a new tab
  function bindRowOpen(containers, pick) {
    const handler = (e) => {
      if (e.type === 'keydown' && e.key !== 'Enter') return;
      if (e.type === 'auxclick' && e.button !== 1) return;
      const el = e.target.closest('[data-index]');
      if (!el) return;
      const item = pick(Number(el.dataset.index));
      if (!item) return;
      const url = `/signals/${encodeURIComponent(item.id)}`;
      if (e.ctrlKey || e.metaKey || e.type === 'auxclick') window.open(url, '_blank', 'noopener');
      else window.location.assign(url);
    };
    containers.forEach(el => {
      el.addEventListener('click', handler);
      el.addEventListener('auxclick', handler);
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
      const res = await fetch('/api/cron/trigger', { method: 'POST' });
      if (!res.ok) throw new Error(await responseError(res));
      const result = (await res.json()).data.result || {};
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
    const base = { status: statuses.join(',') };
    try {
      // The caption is a nice-to-have, so a failed summary still lets the list render
      const [page, summary] = await Promise.all([
        getJson(`/api/signals?${filterParams({ ...base, ...pageParams(tab) })}`),
        getJson(`/api/summary?${filterParams(base)}`).then(body => body.data, () => ({})),
      ]);
      if (request !== listRequest || activeTab !== tab) return;
      const signals = page.data || [];
      const total = page.meta.total_data;
      if (stepBackIfEmpty(tab, signals.length, total)) return;
      setCaption(signalsCaption(total, summary));
      renderSignals(signals);
      renderPager(tab, total, signals.length);
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
      const [page, summary] = await Promise.all([
        getJson(`/api/journal?${params}`),
        getJson('/api/journal/summary').then(body => body.data, () => ({})),
      ]);
      if (request !== listRequest || activeTab !== 'journal') return;
      const entries = page.data || [];
      const total = page.meta.total_data;
      if (stepBackIfEmpty('journal', entries.length, total)) return;
      setCaption(journalCaption(summary));
      renderJournal(entries, Boolean(search || filterJournal.value));
      if (entries.length) renderPager('journal', total, entries.length);
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
      watchlist = (await getJson('/api/watchlist')).data || [];
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
        <label class="toggle admin-only">
          <input type="checkbox" class="wl-toggle" data-symbol="${escapeHtml(item.symbol)}" ${item.is_active ? 'checked' : ''}>
          <span class="track"></span>
          <span>Scan</span>
        </label>
        <button class="btn btn-quiet wl-delete admin-only" type="button" data-symbol="${escapeHtml(item.symbol)}">Remove</button>
      </li>
    `).join('');
  }

  watchlistList.addEventListener('change', async (e) => {
    const input = e.target.closest('.wl-toggle');
    if (!input) return;
    const sym = input.dataset.symbol;
    const active = input.checked;
    try {
      const res = await fetch(`/api/watchlist/${encodeURIComponent(sym)}/toggle`, {
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
      const res = await fetch(`/api/watchlist/${encodeURIComponent(sym)}`, { method: 'DELETE' });
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
      const res = await fetch('/api/watchlist', {
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
  // The view lives in the URL, so coming back from a signal page restores the tab, page and filters
  const URL_FILTERS = { symbol: filterSymbol, date: filterDate, side: filterSide, status: filterStatus, journal: filterJournal };

  function saveViewToUrl() {
    const params = new URLSearchParams();
    if (activeTab !== 'active') params.set('tab', activeTab);
    if (pageIndex[activeTab]) params.set('page', String(pageIndex[activeTab] + 1));
    Object.entries(URL_FILTERS).forEach(([key, el]) => {
      if (el.value.trim()) params.set(key, el.value.trim());
    });
    const query = params.toString();
    history.replaceState(null, '', query ? `?${query}` : window.location.pathname);
  }

  function restoreViewFromUrl() {
    const params = new URLSearchParams(window.location.search);
    Object.entries(URL_FILTERS).forEach(([key, el]) => {
      const value = params.get(key);
      if (value == null) return;
      el.value = value;
      if (el.value !== value) el.value = ''; // a select or date input rejects unknown values
    });
    const wanted = params.get('tab');
    const tab = tabs.some(t => t.dataset.tab === wanted) ? wanted : 'active';
    const page = Number(params.get('page'));
    if (tab in pageIndex && Number.isInteger(page) && page > 1) pageIndex[tab] = page - 1;
    return tab;
  }

  function reloadActiveList() {
    saveViewToUrl();
    if (activeTab in TAB_STATUSES) return loadSignals();
    if (activeTab === 'journal') return loadJournal();
    return loadWatchlist();
  }

  function selectTab(tab) {
    activeTab = tab;
    saveViewToUrl();
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

  // Initialization
  initAuth();
  bindLogout();
  btnScan.addEventListener('click', runNow);
  // The "new" tag needs the last run time, which may arrive after the first list
  checkHealth().then(() => { if (shownSignals.length) renderSignals(shownSignals); });
  setInterval(checkHealth, 60000);
  loadConfig();
  loadSummary();
  loadWatchlist();
  selectTab(restoreViewFromUrl());
});
