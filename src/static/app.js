document.addEventListener('DOMContentLoaded', () => {
  const TZ = 'Asia/Jakarta';
  // Older versions kept the API key here, readable by any script on the page; it is wiped on load
  const STALE_KEY_STORAGE = ['crossline_api_key', 'tokyo_api_key'];
  const CANDLE_MS = 2 * 60 * 60 * 1000;
  const PAGE_SIZE = 20;
  // Self-hosted copy of lightweight-charts 4.2.1 (identical to the npm tarball); the hash pins those exact bytes
  const CHART_LIB_URL = '/static/vendor/lightweight-charts-4.2.1.standalone.production.js';
  const CHART_LIB_INTEGRITY = 'sha384-6pZys+XyCH0tACkmBDDBmI3cqbXjHWKKPtaZLx45PUShrKwmpQljmRsBkU58Ln1F';

  // WIB is UTC+7 all year; date filters send this so the server reads them as WIB days
  const TZ_OFFSET_MINUTES = 420;

  // Defaults until /api/info and /api/performance answer. strategy_version is the running version, the
  // only one whose signals count; summed totals are shown from min_sample_for_totals closed trades
  const config = {
    signal_expiry_candles: 48,
    round_trip_fee_percent: 0.1,
    strategy_version: null,
    min_sample_for_totals: 100,
  };

  let activeTab = 'scan';
  let liveSignals = null;
  let liveMeta = null;
  let liveGeneration = 0;
  let listRequest = 0;
  let shownSignals = [];
  let shownJournal = [];
  let watchlist = [];
  let chart = null;
  let chartRequest = 0;
  let currentSignal = null;
  const pageIndex = { history: 0, journal: 0 };

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
  const filterResult = $('filter-result');
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
  const journalFields = $('journal-fields');
  const journalHelp = $('journal-help');
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

  // Older rows store UTC times without a zone ("2026-10-13 12:00:00"); read them as UTC
  function parseTime(value) {
    if (!value) return null;
    let str = String(value);
    if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$/.test(str)) str = `${str.replace(' ', 'T')}Z`;
    const dt = new Date(str);
    return isNaN(dt.getTime()) ? null : dt;
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

  // Fixed decimals with a typographic minus, as every other signed figure on the page
  function fixedText(val, digits) {
    return Number(val).toFixed(digits).replace('-', '−');
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

  function outcomeOf(status) {
    if (status === 'TP1_HIT' || status === 'PROFIT') return { cls: 'win', label: 'Target hit' };
    if (status === 'SL_HIT' || status === 'LOSS') return { cls: 'loss', label: 'Stopped out' };
    if (status === 'EXPIRED') return { cls: 'expired', label: 'Expired' };
    return { cls: '', label: 'Open' };
  }

  function outcomeHtml(status) {
    const o = outcomeOf(status);
    return `<span class="outcome ${o.cls}">${o.label}</span>`;
  }

  // Net R of a closed signal under the cost model; rows booked before it fall back to their booked R
  function netR(s) {
    if (s.net_r != null) return s.net_r;
    return s.r_multiple != null ? s.r_multiple : null;
  }

  // Booked at the interpolated cross price, a fill nobody could get, so it has no achievable result
  const NO_FILL_NOTE = 'Entry was the interpolated cross price, not a price anyone could fill, so there is no achievable net result';

  function hasAchievableEntry(s) {
    return s.entry_basis !== 'cross_interp';
  }

  function resultHtml(s) {
    const r = netR(s);
    if (r == null) return '<span class="muted">–</span>';
    if (!hasAchievableEntry(s)) return `<span class="muted" title="${escapeHtml(NO_FILL_NOTE)}">no fill</span>`;
    return `<span class="${signClass(r)}">${formatR(r)}</span>`;
  }

  // Where a signal came from; anything but an on-time scheduled signal is listed but not counted
  const SOURCE_CHIPS = {
    legacy: { label: 'LEGACY', cls: 'chip-legacy', why: 'Booked under pre-v2.2 rules: interpolated cross-price entry, or a result recorded without fees' },
    catchup: { label: 'LATE', cls: 'chip-late', why: 'Found after its entry candle had already passed' },
    manual: { label: 'MANUAL', cls: '', why: 'Saved from a manual scan' },
  };

  const SOURCE_LABELS = {
    cron: 'Scheduled scan',
    catchup: 'Scheduled scan, late catch-up',
    manual: 'Manual scan',
    legacy: 'Legacy (pre-v2.2 rules)',
  };

  const ENTRY_BASIS_LABELS = {
    next_open: 'Next candle open',
    forming_close: 'Close of the forming candle',
    cross_interp: 'Interpolated cross price',
  };

  // "2.2.0" reads as v2.2.0; a label such as "legacy" is shown as it is
  function versionLabel(version) {
    if (!version) return '–';
    return /^\d/.test(version) ? `v${version}` : version;
  }

  // Same rule as the server: only eligible signals of the running strategy version count
  function isOtherVersion(s) {
    return Boolean(config.strategy_version && s.strategy_version && s.source !== 'legacy'
      && s.strategy_version !== config.strategy_version);
  }

  function isExcluded(s) {
    return Boolean(s.source) && (s.is_eligible === false || isOtherVersion(s));
  }

  function chipHtml(s) {
    const chip = SOURCE_CHIPS[s.source]
      || (isOtherVersion(s)
        ? { label: versionLabel(s.strategy_version), cls: '', why: `Produced by ${versionLabel(s.strategy_version)}, not the running ${versionLabel(config.strategy_version)}` }
        : null);
    if (!chip) return '';
    return `<span class="chip ${chip.cls}" title="${escapeHtml(`${chip.why}. Excluded from stats.`)}">${escapeHtml(chip.label)}</span>`;
  }

  function journalTagHtml(status) {
    if (status === 'TAKEN') return '<span class="tag">taken</span>';
    if (status === 'SKIPPED') return '<span class="tag">skipped</span>';
    return '';
  }

  // Same formula as the server: PnL after fees, and R against the signal's stop
  function tradeReturn(side, entry, stop, exit) {
    if (!(entry > 0) || !(exit > 0)) return null;
    const dir = side === 'LONG' ? 1 : -1;
    const pnl = dir * (exit - entry) / entry * 100 - config.round_trip_fee_percent;
    const risk = Math.abs(entry - stop) / entry * 100;
    return { pnl, r: risk > 0 ? pnl / risk : null };
  }

  // API helpers
  function forgetStoredKeys() {
    STALE_KEY_STORAGE.forEach(key => {
      try { localStorage.removeItem(key); } catch { /* storage unavailable */ }
    });
  }
  forgetStoredKeys();

  // Owner session: the server keeps it in an HttpOnly cookie, so this page never sees the key after sign-in
  const loginModal = $('login-modal');
  const loginForm = $('login-form');
  const loginKey = $('login-key');
  const loginError = $('login-error');
  const loginSubmit = $('login-submit');
  const btnAuth = $('btn-auth');
  const CSRF_HEADERS = { 'X-Requested-With': 'crossline' };
  let signedIn = false;
  let loginWaiters = [];

  function setSignedIn(value) {
    signedIn = value;
    btnAuth.textContent = value ? 'Sign out' : 'Sign in';
  }

  // Resolves true once signed in, false if the dialog is dismissed; concurrent callers share one dialog
  function requestLogin(message = '') {
    return new Promise((resolve) => {
      loginWaiters.push(resolve);
      if (loginModal.open) return;
      loginError.textContent = message;
      loginKey.value = '';
      loginModal.showModal();
      loginKey.focus();
    });
  }

  function settleLogin(ok) {
    const waiters = loginWaiters;
    loginWaiters = [];
    waiters.forEach(resolve => resolve(ok));
  }

  loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const key = loginKey.value.trim();
    if (!key) {
      loginError.textContent = 'Enter the API key';
      return;
    }
    loginSubmit.disabled = true;
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS },
        body: JSON.stringify({ api_key: key }),
      });
      if (!res.ok) {
        loginError.textContent = res.status === 401 ? 'Wrong API key' : await responseError(res);
        return;
      }
      loginKey.value = '';
      setSignedIn(true);
      settleLogin(true);
      loginModal.close();
    } catch {
      loginError.textContent = 'Could not reach the server';
    } finally {
      loginSubmit.disabled = false;
    }
  });
  $('login-cancel').addEventListener('click', () => loginModal.close());
  $('login-close').addEventListener('click', () => loginModal.close());
  loginModal.addEventListener('close', () => {
    loginKey.value = '';
    settleLogin(false);
  });

  async function checkSession() {
    try {
      const data = await getJson('/api/auth/session');
      setSignedIn(Boolean(data.authenticated));
      if (signedIn && activeTab === 'journal') loadJournal();
    } catch {
      setSignedIn(false);
    }
  }

  btnAuth.addEventListener('click', async () => {
    if (signedIn) {
      try {
        await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin', headers: CSRF_HEADERS });
      } catch { /* the cookie expires on its own */ }
      setSignedIn(false);
      showToast('Signed out');
    } else {
      if (!(await requestLogin())) return;
      showToast('Signed in');
    }
    reloadActiveList();
  });

  // Owner-only requests go out with the session cookie plus the header the server requires on writes;
  // a 401 opens the sign-in dialog and the request is retried once after signing in
  async function authFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    Object.entries(CSRF_HEADERS).forEach(([k, v]) => headers.set(k, v));
    const request = { ...options, headers, credentials: 'same-origin' };
    let res = await fetch(url, request);
    if (res.status === 401) {
      setSignedIn(false);
      if (await requestLogin('Sign in to continue.')) res = await fetch(url, request);
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
    setListMessage(tbody, signalCards, 8, message, isError);
    shownSignals = [];
    pagers.history.hidden = true;
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
    const el = pagers[tab];
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

  Object.entries(pagers).forEach(([tab, el]) => el.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-step]');
    if (!btn || btn.disabled || activeTab !== tab) return;
    pageIndex[tab] = Math.max(0, pageIndex[tab] + Number(btn.dataset.step));
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
      // 503 still carries the status body (database down)
      const data = await (await fetch('/api/health')).json();
      if (data.status === 'healthy') {
        dot.className = 'dot ok';
        text.textContent = 'Online';
      } else {
        const issues = [
          !data.db && 'database',
          !data.redis && 'redis',
          data.cron_fresh === false && 'scan overdue',
        ].filter(Boolean).join(', ');
        dot.className = data.db ? 'dot warn' : 'dot err';
        text.textContent = `Degraded (${issues})`;
      }
    } catch {
      dot.className = 'dot err';
      text.textContent = 'Offline';
    }
    try {
      const cron = await getJson('/api/cron/status');
      cronText.textContent = cron.enabled ? formatTimeOnlyWIB(cron.next_run) : 'Off';
    } catch {
      cronText.textContent = '--:--';
    }
  }

  async function loadConfig() {
    try {
      const info = await getJson('/api/info');
      if (info.signal_expiry_candles != null) config.signal_expiry_candles = info.signal_expiry_candles;
      if (info.round_trip_fee_percent != null) config.round_trip_fee_percent = info.round_trip_fee_percent;
      if (info.strategy_version) config.strategy_version = info.strategy_version;
    } catch {
      // keep defaults
    }
  }

  // Track record: eligible closed trades of the running strategy version, from /api/performance
  function signedNum(val, digits = 2) {
    const num = Number(val);
    return `${num > 0 ? '+' : num < 0 ? '−' : ''}${Math.abs(num).toFixed(digits)}`;
  }

  function percentText(val) {
    return val == null ? '–' : `${Number(val).toFixed(0)}%`;
  }

  // Every reason a signal is left out; the last two are only listed when they apply
  const EXCLUSION_LABELS = [
    ['legacy', 'legacy', true],
    ['catchup', 'late', true],
    ['manual', 'manual', true],
    ['other_version', 'other versions', false],
    ['other', 'other', false],
  ];

  function statsNote(p) {
    const ex = p.excluded || {};
    const parts = [
      `Strategy ${versionLabel(p.strategy_version)}, cost model ${p.cost_model_version}`,
      `n=${p.n_closed} closed, ${p.open_count} open`,
    ];
    if (p.sample_size_warning) parts.push(`${p.sample_size_warning} (n < ${p.min_sample}): too few trades to tell an edge from luck`);
    const reasons = EXCLUSION_LABELS
      .filter(([key, , always]) => always || ex[key])
      .map(([key, label]) => `${ex[key] || 0} ${label}`);
    parts.push(`excluded from stats: ${reasons.join(', ')}`);
    return `${parts.join(' · ')}.`;
  }

  // Summed totals wait for enough trades, so a handful of results does not anchor anyone
  function totalsShown(n) {
    return n >= config.min_sample_for_totals;
  }

  async function loadSummary() {
    try {
      const p = await getJson('/api/performance');
      const n = Number(p.n_closed) || 0;
      if (p.min_sample_for_totals != null) config.min_sample_for_totals = p.min_sample_for_totals;
      if (p.strategy_version) config.strategy_version = p.strategy_version;
      const insufficient = p.sample_size_warning
        ? `<span class="stat-warn">${escapeHtml(p.sample_size_warning)}</span>`
        : '';

      const mean = $('stat-mean-r');
      mean.textContent = n ? formatR(p.mean_net_r) : '–';
      mean.className = `stat-value num ${n ? signClass(p.mean_net_r) : ''}`;
      const ci = p.mean_net_r_ci95;
      $('stat-mean-sub').innerHTML = (ci
        ? `95% CI [${signedNum(ci[0])}, ${signedNum(ci[1])}], n=${n}`
        : `Per closed trade, n=${n}`) + insufficient;

      $('stat-winrate').innerHTML = n
        ? `${percentText(p.win_rate)}<span class="stat-n">(${Number(p.wins)}/${n})</span>`
        : '–';
      $('bar-wins').style.width = n ? `${(p.wins / n) * 100}%` : '0';
      $('bar-losses').style.width = n ? `${(p.losses / n) * 100}%` : '0';
      $('bar-expired').style.width = n ? `${(p.expired / n) * 100}%` : '0';
      $('stat-win-sub').textContent = n
        ? `break-even ≈${percentText(p.break_even_win_rate)}, ${p.expired} expired counted as closed`
        : 'No closed trades yet';

      const total = $('stat-net-r');
      const showTotal = n > 0 && totalsShown(n);
      total.textContent = showTotal ? formatR(p.total_net_r) : '–';
      total.className = `stat-value num ${showTotal ? signClass(p.total_net_r) : ''}`;
      $('stat-net-sub').textContent = (showTotal
        ? `${versionLabel(p.strategy_version)}, after fees and slippage`
        : `Total shown from n=${config.min_sample_for_totals} (now ${n})`)
        + (p.profit_factor != null ? `, profit factor ${Number(p.profit_factor).toFixed(2)}` : '');

      const dd = $('stat-drawdown');
      dd.textContent = n ? formatR(-(p.max_drawdown_r || 0)) : '–';
      dd.className = `stat-value num ${p.max_drawdown_r ? 'is-short' : ''}`;

      $('stats-note').textContent = statsNote(p);
    } catch (e) {
      console.warn('Performance load failed', e);
    }
  }

  // Signals (latest scan + history)
  function filterSignals(list) {
    const search = filterSymbol.value.toUpperCase().trim();
    const side = filterSide.value;
    const result = filterResult.value;
    const dateVal = filterDate.value;
    const statusFor = { PROFIT: 'TP1_HIT', LOSS: 'SL_HIT', EXPIRED: 'EXPIRED', PENDING: 'PENDING' };

    return list.filter(s => {
      if (search && !s.symbol.includes(search)) return false;
      if (side && s.side !== side) return false;
      if (result && s.win_status !== statusFor[result]) return false;
      // The picked date is a WIB day, the same day the table shows
      if (dateVal && !formatWIB(s.cross_time).startsWith(dateVal)) return false;
      return true;
    });
  }

  function signalTags(s) {
    if (s.confirmed === false) return '<span class="tag tag-forming">forming</span>';
    if (activeTab === 'scan' && s.is_new) return '<span class="tag tag-new">new</span>';
    return journalTagHtml(s.journal_status);
  }

  function renderSignals(list) {
    // History rows arrive filtered and paged by the server; only the scan result is filtered here
    const filtered = activeTab === 'scan' ? filterSignals(list) : list;

    if (filtered.length === 0) {
      const filteredOut = activeTab === 'scan' ? list.length > 0 : hasFilters();
      setSignalsMessage(filteredOut ? 'No signals match these filters.' : 'No crossovers found.');
      return;
    }
    shownSignals = filtered;

    const excludedNote = '<div class="excl-note">excluded from stats</div>';

    tbody.innerHTML = filtered.map((s, i) => `
      <tr data-index="${i}" tabindex="0"${isExcluded(s) ? ' class="is-excluded"' : ''}>
        <td><span class="sym">${escapeHtml(s.symbol)}</span>${chipHtml(s)}${signalTags(s)}</td>
        <td>${sideHtml(s.side)}</td>
        <td class="r num">${formatPrice(s.entry_price || s.cross_price)}</td>
        <td class="r num">${formatPrice(s.stop_loss)}</td>
        <td class="r num">${formatPrice(s.tp1)}</td>
        <td>${outcomeHtml(s.win_status)}</td>
        <td class="r num">${resultHtml(s)}${isExcluded(s) ? excludedNote : ''}</td>
        <td class="num muted">${escapeHtml(formatWIB(s.cross_time))}</td>
      </tr>
    `).join('');

    signalCards.innerHTML = filtered.map((s, i) => `
      <div class="card${isExcluded(s) ? ' is-excluded' : ''}" data-index="${i}" tabindex="0">
        <div class="card-top">
          <div><span class="sym">${escapeHtml(s.symbol)}</span> &nbsp;${sideHtml(s.side)}${chipHtml(s)}${signalTags(s)}</div>
          ${outcomeHtml(s.win_status)}
        </div>
        <dl class="card-levels">
          <div><dt>Entry</dt><dd class="num">${formatPrice(s.entry_price || s.cross_price)}</dd></div>
          <div><dt>Stop</dt><dd class="num">${formatPrice(s.stop_loss)}</dd></div>
          <div><dt>Target</dt><dd class="num">${formatPrice(s.tp1)}</dd></div>
        </dl>
        <div class="card-foot">
          <span class="card-time num">${escapeHtml(formatWIB(s.cross_time))} WIB${isExcluded(s) ? ', excluded from stats' : ''}</span>
          ${netR(s) != null ? `<span class="card-result num">${resultHtml(s)}</span>` : ''}
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

  function scanCaption() {
    if (!liveMeta) return 'No scan has run in this session. The scheduled scan runs every 2 hours; use Run scan to check now.';
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

  function renderScanView() {
    if (activeTab !== 'scan') return;
    pagers.history.hidden = true;
    setCaption(scanCaption());
    if (liveSignals === null) {
      setSignalsMessage('Results from your next scan will appear here.');
      return;
    }
    if (liveMeta && liveMeta.failed.length === liveMeta.total_scanned) {
      setSignalsMessage('No results: candles could not be loaded from Binance.', true);
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
      if (!row) return;
      if (row.win_status) tracked[idx].win_status = row.win_status;
      ['r_multiple', 'pnl_percent', 'gross_r', 'cost_r', 'net_r', 'exit_price', 'exit_time', 'source', 'is_eligible',
        'strategy_version', 'entry_basis', 'detected_at'].forEach(k => { tracked[idx][k] = row[k]; });
      tracked[idx].journal_status = row.journal ? row.journal.status : null;
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
    // A preview by default; saving stores manual signals, which never count in the stats
    const saveDb = $('select-save').value === 'true';

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
    if (filterDate.value) {
      params.append('date', filterDate.value);
      params.append('tz_offset_minutes', String(TZ_OFFSET_MINUTES));
    }
    return params;
  }

  function hasFilters() {
    return Boolean(filterSymbol.value.trim() || filterSide.value || filterResult.value || filterDate.value);
  }

  async function loadHistory() {
    const request = ++listRequest;
    try {
      const data = await getJson(`/api/signals?${filterParams({ timeframe: '2h', ...pageParams('history') })}`);
      if (request !== listRequest || activeTab !== 'history') return;
      const signals = data.signals || [];
      if (stepBackIfEmpty('history', signals.length, data.total)) return;
      setCaption(signalsCaption(data.total, data.summary || {}));
      renderSignals(signals);
      renderPager('history', data.total, signals.length);
    } catch (e) {
      if (request !== listRequest || activeTab !== 'history') return;
      setCaption('');
      setSignalsMessage(`Could not load signal history: ${e.message}`, true);
    }
  }

  function signalsCaption(total, sum) {
    if (!total) return '';
    let text = `${plural(total, 'signal')}, newest first: ${sum.open_count || 0} open, `
      + `${sum.profit_count || 0} hit target, ${sum.loss_count || 0} stopped, ${sum.expired_count || 0} expired.`;
    if (sum.eligible_closed_count) {
      text += ` Counted in stats (${versionLabel(sum.strategy_version)}): ${plural(sum.eligible_closed_count, 'closed trade')}`
        + (totalsShown(sum.eligible_closed_count) ? `, ${formatR(sum.eligible_net_r)} net.` : '.');
    }
    if (sum.excluded_count) text += ` ${sum.excluded_count} excluded from stats.`;
    return text;
  }

  // Journal
  async function loadJournal() {
    const request = ++listRequest;
    // Owner-only: a visitor gets the explanation, not a sign-in prompt or a failed request
    if (!signedIn) {
      setCaption('');
      setJournalMessage('Your journal is private. Use Sign in at the top of the page to see it.');
      return;
    }
    const params = new URLSearchParams(pageParams('journal'));
    const search = filterSymbol.value.toUpperCase().trim();
    if (search) params.append('symbol', search);
    if (filterJournal.value) params.append('status', filterJournal.value);
    try {
      const res = await authFetch(`/api/journal?${params}`);
      if (request !== listRequest || activeTab !== 'journal') return;
      if (res.status === 401) {
        setCaption('');
        setJournalMessage('Your journal is private. Sign in to see it.');
        return;
      }
      if (!res.ok) throw new Error(await responseError(res));
      const data = await res.json();
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
    const tracked = (closed, r) => (closed ? `${formatR(r)} net over ${closed} closed` : 'none closed yet');
    text += ` Tracked result of the signals you took: ${tracked(sum.signal_closed_taken, sum.signal_r_taken)}; `
      + `of those you skipped: ${tracked(sum.signal_closed_skipped, sum.signal_r_skipped)}.`;
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
        <td>${outcomeHtml(s.win_status)}</td>
        <td class="num muted">${escapeHtml(formatWIB(s.cross_time))}</td>
      </tr>
    `).join('');

    journalCards.innerHTML = list.map((s, i) => `
      <div class="card" data-index="${i}" tabindex="0">
        <div class="card-top">
          <div><span class="sym">${escapeHtml(s.symbol)}</span> &nbsp;${sideHtml(s.side)}<span class="tag">${decisionLabel(s.journal).toLowerCase()}</span></div>
          ${outcomeHtml(s.win_status)}
        </div>
        <dl class="card-levels">
          <div><dt>Entry fill</dt><dd class="num">${formatPrice(s.journal && s.journal.entry_fill)}</dd></div>
          <div><dt>Exit fill</dt><dd class="num">${formatPrice(s.journal && s.journal.exit_fill)}</dd></div>
          <div><dt>Your result</dt><dd class="num">${journalResultHtml(s.journal)}</dd></div>
        </dl>
        <div class="card-time num">${escapeHtml(formatWIB(s.cross_time))} WIB</div>
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

  // Evaluate trade outcomes
  btnEval.addEventListener('click', async () => {
    btnEval.disabled = true;
    btnEval.textContent = 'Evaluating…';
    try {
      const res = await authFetch('/api/evaluate', { method: 'POST' });
      if (!res.ok) throw new Error(await responseError(res));
      const data = await res.json();
      showNotice('');
      const n = (data.evaluated && data.evaluated.evaluated_count) || 0;
      showToast(n ? `${plural(n, 'trade')} closed` : 'No open trade reached its target, stop or expiry');
      await loadSummary();
      await reloadActiveList();
    } catch (e) {
      showNotice(`Evaluation failed: ${e.message}`);
    } finally {
      btnEval.disabled = false;
      btnEval.textContent = 'Evaluate outcomes';
    }
  });

  // Tabs
  function reloadActiveList() {
    if (activeTab === 'history') return loadHistory();
    if (activeTab === 'journal') return loadJournal();
    if (activeTab === 'scan') return refreshLiveView();
    return loadWatchlist();
  }

  function selectTab(tab) {
    activeTab = tab;
    tabs.forEach(t => t.setAttribute('aria-selected', String(t.dataset.tab === tab)));

    views.signals.hidden = !(tab === 'scan' || tab === 'history');
    views.journal.hidden = tab !== 'journal';
    views.watchlist.hidden = tab !== 'watchlist';

    filters.hidden = tab === 'watchlist';
    filters.querySelectorAll('[data-tabs]').forEach(el => {
      el.hidden = !el.dataset.tabs.split(' ').includes(tab);
    });

    setCaption('');
    if (tab === 'scan') {
      renderScanView();
    } else if (tab === 'history') {
      setSignalsMessage('Loading…');
      loadHistory();
    } else if (tab === 'journal') {
      setJournalMessage('Loading…');
      loadJournal();
    } else {
      renderWatchlist();
      loadWatchlist();
    }
  }

  tabs.forEach(t => t.addEventListener('click', async () => {
    if (t.dataset.tab === activeTab) return;
    selectTab(t.dataset.tab);
    t.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    if (t.dataset.tab === 'scan') await refreshLiveView();
  }));

  function onFilterChanged() {
    // Filters are shared across tabs, so every list starts again from its first page
    Object.keys(pageIndex).forEach(tab => { pageIndex[tab] = 0; });
    if (activeTab === 'scan') {
      renderScanView();
    } else {
      reloadActiveList();
    }
  }

  filterSymbol.addEventListener('input', debounce(onFilterChanged, 250));
  [filterDate, filterSide, filterResult, filterJournal].forEach(el => el.addEventListener('change', onFilterChanged));

  // Signal detail
  function pctFrom(entry, level) {
    if (!entry || !level) return '';
    return `<small>${formatPct(((level - entry) / entry) * 100)}</small>`;
  }

  function detailSubtitle(sig) {
    const crossed = parseTime(sig.cross_time);
    if (!crossed) return '';
    let text = `Crossed ${formatWIB(crossed)} WIB on the 2H chart`;
    if (sig.confirmed === false) return `${text}. The candle is still forming, so this cross is not recorded yet.`;
    if (sig.win_status === 'PENDING') {
      const expires = new Date(crossed.getTime() + (config.signal_expiry_candles + 1) * CANDLE_MS);
      text += `. Expires ${formatWIB(expires)} WIB if neither level is hit.`;
    }
    return text;
  }

  function fillDetail(sig) {
    const entry = sig.entry_price || sig.cross_price;
    $('modal-title').innerHTML = `${escapeHtml(sig.symbol)} ${sideHtml(sig.side)}`;
    $('modal-time').textContent = detailSubtitle(sig);

    const showCross = sig.cross_price && entry && Math.abs(sig.cross_price - entry) / entry > 1e-6;
    $('detail-entry').innerHTML = formatPrice(entry)
      + (showCross ? `<span class="hint">cross at ${formatPrice(sig.cross_price)}</span>` : '');
    $('detail-sl').innerHTML = formatPrice(sig.stop_loss) + pctFrom(entry, sig.stop_loss);
    $('detail-tp').innerHTML = formatPrice(sig.tp1) + pctFrom(entry, sig.tp1);
    $('detail-rr').textContent = sig.risk_reward_ratio ? `1:${sig.risk_reward_ratio}` : '–';
    let outcome = sig.win_status ? outcomeHtml(sig.win_status) : '–';
    if (sig.exit_price != null) {
      outcome += `<span class="hint">Exit ${formatPrice(sig.exit_price)} on ${escapeHtml(formatWIB(sig.exit_time))}</span>`;
    }
    $('detail-outcome').innerHTML = outcome;
    fillProvenance(sig);

    const fmt4 = (v) => (v != null ? fixedText(v, 4) : '–');
    $('detail-macd').textContent = fmt4(sig.macd);
    $('detail-signal').textContent = fmt4(sig.signal);
    const hist = $('detail-hist');
    hist.textContent = fmt4(sig.histogram);
    hist.className = `num ${sig.histogram == null ? '' : sig.histogram >= 0 ? 'is-long' : 'is-short'}`;
  }

  // Result split into gross, cost and net R, and where the signal came from
  function fillProvenance(sig) {
    const rCell = (id, value) => {
      const el = $(id);
      el.textContent = value == null ? '–' : formatR(value);
      el.className = `num ${signClass(value)}`;
    };
    const achievable = hasAchievableEntry(sig);
    rCell('detail-gross', achievable ? sig.gross_r : null);
    rCell('detail-cost', !achievable || sig.cost_r == null ? null : -sig.cost_r);
    rCell('detail-net', achievable ? sig.net_r : null);
    if (!achievable && netR(sig) != null) {
      $('detail-net').innerHTML = `–<span class="hint">${escapeHtml(NO_FILL_NOTE)}.</span>`;
    }

    let counted = '–';
    if (sig.source) counted = isExcluded(sig) ? `Excluded${chipHtml(sig)}` : 'Counted';
    else if (!sig.id) counted = 'Not saved';
    $('detail-counted').innerHTML = counted;

    $('detail-version').textContent = versionLabel(sig.strategy_version);
    $('detail-source').textContent = sig.source
      ? SOURCE_LABELS[sig.source] || sig.source
      : (sig.id ? '–' : 'Preview, not saved');
    $('detail-basis').textContent = sig.entry_basis ? ENTRY_BASIS_LABELS[sig.entry_basis] || sig.entry_basis : '–';
    $('detail-detected').textContent = sig.detected_at ? formatWIB(sig.detected_at) : '–';
  }

  async function openSignal(sig) {
    loadChartLibrary().catch(() => {});
    currentSignal = sig;
    destroyChart();
    showChartMessage('<span class="spinner on"></span> Loading candles…');
    fillDetail(sig);
    renderJournalForm(sig);
    if (!modal.open) modal.showModal();
    if (!sig.id) {
      renderChart(sig);
      return;
    }
    try {
      const fresh = await getJson(`/api/signals/${encodeURIComponent(sig.id)}`);
      if (currentSignal !== sig) return;
      currentSignal = { ...sig, ...fresh };
      fillDetail(currentSignal);
      renderJournalForm(currentSignal);
      renderChart(currentSignal);
    } catch (e) {
      if (currentSignal !== sig) return;
      if (sig.entry_price) renderChart(sig);
      else showChartMessage(escapeHtml(`Could not load this signal: ${e.message}`), true);
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
    const res = entry && exit ? tradeReturn(currentSignal.side, entry, currentSignal.stop_loss, exit) : null;
    if (res) {
      journalResult.innerHTML = `Your result: <span class="${signClass(res.pnl)}">${formatR(res.r)}</span> (${formatPct(res.pnl)} after fees)`;
    } else {
      journalResult.textContent = entry ? 'Add the exit fill once the trade is closed.' : '';
    }
  }

  function renderJournalForm(sig) {
    const canRecord = Boolean(sig.id);
    journalFields.hidden = !canRecord;
    journalHelp.textContent = canRecord
      ? "Record whether you took this signal and at what prices, to compare your results with the signal's."
      : 'Only recorded signals can be journaled. Crosses on a forming candle are recorded once the candle closes.';
    if (!canRecord) return;

    const j = sig.journal || null;
    const status = j ? j.status : '';
    journalForm.querySelectorAll('input[name="journal-status"]').forEach(r => { r.checked = r.value === status; });
    journalEntry.value = j && j.entry_fill != null ? j.entry_fill : '';
    journalExit.value = j && j.exit_fill != null ? j.exit_fill : '';
    journalNotes.value = j ? j.notes || '' : '';
    journalEntry.placeholder = sig.entry_price ? `Signal entry ${formatPrice(sig.entry_price)}` : '';
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
    if (!sig || !sig.id) return;
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
      [liveSignals || [], shownSignals].forEach(list => list.forEach(s => {
        if (s.id === sig.id) s.journal_status = sig.journal_status;
      }));
      renderJournalForm(sig);
      showToast(status ? 'Saved to your journal' : 'Removed from your journal');
      if (activeTab === 'scan') renderScanView();
      else if (activeTab !== 'watchlist') reloadActiveList();
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
        script.integrity = CHART_LIB_INTEGRITY;
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
      // Recorded signals chart their own trade window; unsaved scan results show the latest candles
      const url = sig.id
        ? `/api/signals/${encodeURIComponent(sig.id)}/candles`
        : `/api/klines?symbol=${encodeURIComponent(sig.symbol)}&limit=80`;
      const res = await fetch(url);
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

      // Candle containing a moment; an expiry is stamped at the close, so it belongs to the candle before
      const candleAt = (date, closeStamp = false) => {
        if (!date) return null;
        const ts = Math.floor(date.getTime() / 1000) + UTC7_OFFSET_SEC - (closeStamp ? 1 : 0);
        return candles.find(c => c.time <= ts && ts < c.time + CANDLE_MS / 1000) || null;
      };

      const markers = [];
      const isLong = sig.side === 'LONG';
      const crossCandle = candleAt(parseTime(sig.cross_time));
      if (crossCandle) {
        markers.push({
          time: crossCandle.time,
          position: isLong ? 'belowBar' : 'aboveBar',
          color: isLong ? longColor : shortColor,
          shape: isLong ? 'arrowUp' : 'arrowDown',
          text: 'Cross',
        });
      }
      const exitCandle = sig.exit_price != null ? candleAt(parseTime(sig.exit_time), sig.win_status === 'EXPIRED') : null;
      if (exitCandle) {
        const entry = sig.entry_price || sig.cross_price;
        markers.push({
          time: exitCandle.time,
          position: sig.exit_price >= entry ? 'aboveBar' : 'belowBar',
          color: (sig.r_multiple ?? sig.pnl_percent) >= 0 ? longColor : shortColor,
          shape: 'circle',
          text: 'Exit',
        });
      }
      if (markers.length) candleSeries.setMarkers(markers.sort((a, b) => a.time - b.time));

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
  modal.addEventListener('close', () => {
    destroyChart();
    currentSignal = null;
  });

  // Initialization
  btnScan.addEventListener('click', runScan);
  checkSession();
  checkHealth();
  setInterval(checkHealth, 60000);
  loadConfig();
  loadSummary();
  loadWatchlist();
  // Visitors land on the recorded signals: a scan result only exists after the owner runs one
  selectTab('history');
});
