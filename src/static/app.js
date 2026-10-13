document.addEventListener('DOMContentLoaded', () => {
  let activeTab = 'scan';
  let liveSignals = null;
  let dbSignals = [];
  let pnlHistory = [];
  let watchlist = [];

  // DOM Elements - Signals
  const tbody = document.getElementById('signals-tbody');
  const mobileContainer = document.getElementById('signals-mobile-container');
  const btnScan = document.getElementById('btn-scan');
  const scanLabel = document.getElementById('scan-label');
  const scanIcon = document.getElementById('scan-icon');
  const btnEval = document.getElementById('btn-eval');

  // Navigation Tabs & Views
  const tabScan = document.getElementById('tab-scan');
  const tabHistory = document.getElementById('tab-history');
  const tabProfitLoss = document.getElementById('tab-profitloss');
  const tabWatchlist = document.getElementById('tab-watchlist');

  const viewSignals = document.getElementById('view-signals');
  const viewProfitLoss = document.getElementById('view-profitloss');
  const viewWatchlist = document.getElementById('view-watchlist');

  // Profit/Loss DOM Elements
  const pnlTbody = document.getElementById('pnl-tbody');
  const pnlMobileContainer = document.getElementById('pnl-mobile-container');
  const statTotalPnl = document.getElementById('stat-total-pnl');
  const statPnlSub = document.getElementById('stat-pnl-sub');

  // Filters
  const filterSymbol = document.getElementById('filter-symbol');
  const filterDate = document.getElementById('filter-date');
  const filterSide = document.getElementById('filter-side');
  const filterResult = document.getElementById('filter-result');

  // Watchlist elements
  const inputNewSymbol = document.getElementById('input-new-symbol');
  const btnAddSymbol = document.getElementById('btn-add-symbol');
  const watchlistGrid = document.getElementById('watchlist-grid');

  // Modal elements
  const modal = document.getElementById('signal-modal');
  const modalClose = document.getElementById('modal-close');
  const chartViewport = document.getElementById('chart-viewport');

  // WIB (UTC+7) Live Clock
  setInterval(() => {
    const now = new Date();
    const wibStr = now.toLocaleTimeString('id-ID', {
      timeZone: 'Asia/Jakarta',
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
    document.getElementById('clock-pill').textContent = `${wibStr} WIB (UTC+7)`;
  }, 1000);

  // Time formatting helper for WIB (UTC+7)
  function formatWIB(isoStr) {
    if (!isoStr) return '-';
    try {
      const dt = new Date(isoStr);
      const datePart = dt.toLocaleDateString('id-ID', {
        timeZone: 'Asia/Jakarta',
        day: '2-digit',
        month: '2-digit',
        year: 'numeric',
      });
      const timePart = dt.toLocaleTimeString('id-ID', {
        timeZone: 'Asia/Jakarta',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      });
      return `${datePart} ${timePart} WIB`;
    } catch {
      return isoStr;
    }
  }

  function formatTimeOnlyWIB(isoStr) {
    if (!isoStr) return '--:--';
    try {
      const dt = new Date(isoStr);
      return dt.toLocaleTimeString('id-ID', {
        timeZone: 'Asia/Jakarta',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      });
    } catch {
      return isoStr;
    }
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

  function getApiKey() {
    return localStorage.getItem('tokyo_api_key') || '';
  }

  async function authFetch(url, options = {}) {
    const key = getApiKey();
    options.headers = options.headers || {};
    if (key) {
      if (options.headers instanceof Headers) {
        options.headers.set('X-API-KEY', key);
      } else {
        options.headers['X-API-KEY'] = key;
      }
    }
    const res = await fetch(url, options);
    if (res.status === 401) {
      const entered = prompt('🔐 Protected Endpoint: Masukkan API_SECRET_KEY server:');
      if (entered) {
        localStorage.setItem('tokyo_api_key', entered.trim());
        if (options.headers instanceof Headers) {
          options.headers.set('X-API-KEY', entered.trim());
        } else {
          options.headers['X-API-KEY'] = entered.trim();
        }
        return await fetch(url, options);
      }
    }
    return res;
  }

  // Health & Cron Status Check
  async function checkHealth() {
    try {
      const res = await fetch('/api/health');
      const data = await res.json();
      const dot = document.getElementById('health-dot');
      const text = document.getElementById('health-text');
      const cronDot = document.getElementById('cron-dot');
      const cronText = document.getElementById('cron-text');

      if (data.database && data.redis) {
        dot.className = 'dot-live';
        dot.style.background = 'var(--cyan)';
        text.textContent = 'SYSTEM ACTIVE // 2H';
      } else {
        dot.className = 'dot-live';
        dot.style.background = '#ff0055';
        text.textContent = `DEGRADED (DB:${data.database ? 'OK':'ERR'})`;
      }

      if (data.cron && data.cron.enabled) {
        cronDot.className = 'dot-live';
        cronDot.style.background = 'var(--pink)';
        const nextTime = formatTimeOnlyWIB(data.cron.next_run);
        cronText.textContent = `CRON 2H (${nextTime} WIB)`;
      } else {
        cronText.textContent = 'CRON: DISABLED';
      }
    } catch {
      document.getElementById('health-text').textContent = 'API OFFLINE';
    }
  }

  // Summary & Winrate Speedometer
  async function loadSummary() {
    try {
      const res = await fetch('/api/summary');
      const data = await res.json();

      const winrate = data.winrate || 0.0;
      document.getElementById('winrate-text').textContent = `${winrate}%`;
      document.getElementById('stat-winrate').textContent = `${winrate}%`;

      const circumference = 226;
      const offset = circumference - (circumference * (winrate / 100));
      document.getElementById('winrate-meter').style.strokeDashoffset = offset;

      const wins = data.tp1_hit_count || 0;
      const losses = data.sl_hit_count || 0;
      const pending = data.pending_count || 0;
      const winRatioEl = document.getElementById('stat-win-ratio');
      if (winRatioEl) {
        winRatioEl.innerHTML = `<span class="color-long">${escapeHtml(wins)} Win</span> / <span class="color-short">${escapeHtml(losses)} Loss</span>`;
      }
      const pendingEl = document.getElementById('stat-active-pending');
      if (pendingEl) {
        pendingEl.textContent = `${pending} IN-PROGRESS TRADES`;
      }

      document.getElementById('stat-total-signals').textContent = data.total_signals || 0;
      document.getElementById('stat-long').textContent = `${data.long_signals || 0} L`;
      document.getElementById('stat-short').textContent = `${data.short_signals || 0} S`;

      if (statTotalPnl) {
        const pnlVal = data.total_net_pnl || 0.0;
        statTotalPnl.textContent = `${pnlVal >= 0 ? '+' : ''}${pnlVal.toFixed(2)}%`;
        statTotalPnl.className = `hud-stat ${pnlVal >= 0 ? 'color-long' : 'color-short'}`;
      }
      if (statPnlSub) {
        const closedCount = data.closed_trades ?? (wins + losses);
        statPnlSub.textContent = `${closedCount} Closed Trades`;
      }
    } catch (e) {
      console.warn('Summary load failed', e);
    }
  }

  function formatPrice(val) {
    if (val == null || isNaN(val)) return '-';
    const num = Number(val);
    if (num >= 1000) return num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (num >= 1) return num.toFixed(4);
    return num.toFixed(6);
  }

  // Render Signals Table & Mobile Cards
  function renderSignals(list) {
    const search = filterSymbol.value.toUpperCase().trim();
    const side = filterSide.value;
    const filterRes = filterResult.value;
    const dateVal = filterDate.value;

    const filtered = list.filter(s => {
      if (search && !s.symbol.includes(search)) return false;
      if (side && s.side !== side) return false;
      if (filterRes) {
        if (filterRes === 'PROFIT' && s.win_status !== 'TP1_HIT') return false;
        if (filterRes === 'LOSS' && s.win_status !== 'SL_HIT') return false;
        if (filterRes === 'PENDING' && s.win_status !== 'PENDING') return false;
      }
      if (dateVal) {
        if (!s.cross_time || !s.cross_time.startsWith(dateVal)) return false;
      }
      return true;
    });

    if (filtered.length === 0) {
      const emptyHtml = `
        <div style="text-align:center; padding:36px; color:var(--text-muted); font-family:var(--font-mono); font-size:0.85rem;">
          No 2H crossover signals matching current filter criteria.
        </div>`;
      tbody.innerHTML = `<tr><td colspan="10">${emptyHtml}</td></tr>`;
      mobileContainer.innerHTML = emptyHtml;
      return;
    }

    // 1. Desktop Table Rows
    tbody.innerHTML = filtered.map(s => {
      const isLong = s.side === 'LONG';
      const badgeCls = isLong ? 'badge-drift-long' : 'badge-drift-short';
      const arrow = isLong ? '▲' : '▼';

      let outcomeBadge = '<span class="badge-pending">⏳ PENDING</span>';
      if (s.win_status === 'TP1_HIT' || s.win_status === 'PROFIT') {
        outcomeBadge = '<span class="badge-win">🎯 TP1 HIT</span>';
      } else if (s.win_status === 'SL_HIT' || s.win_status === 'LOSS') {
        outcomeBadge = '<span class="badge-loss">⛔ SL HIT</span>';
      }

      const timeStr = formatWIB(s.cross_time);

      return `
        <tr class="clickable-row" data-symbol="${escapeHtml(s.symbol)}">
          <td class="tag-symbol">${escapeHtml(s.symbol)}</td>
          <td><span class="badge-drift ${badgeCls}">${arrow} ${escapeHtml(s.side)}</span></td>
          <td style="color:var(--pink); font-weight:700;">2H</td>
          <td style="color:var(--cyan); font-weight:700;">$${formatPrice(s.entry_price || s.cross_price)}</td>
          <td style="color:var(--short); font-weight:600;">$${formatPrice(s.stop_loss)}</td>
          <td style="color:var(--long); font-weight:600;">$${formatPrice(s.tp1)}</td>
          <td style="font-weight:700; color:var(--pink);">1:${escapeHtml(s.risk_reward_ratio || 1.5)}</td>
          <td>${outcomeBadge}</td>
          <td style="color:var(--text-muted); font-size:0.75rem;">${escapeHtml(timeStr)}</td>
          <td>
            <button class="btn-outline btn-view-chart" style="padding:4px 10px; font-size:0.7rem;" data-symbol="${escapeHtml(s.symbol)}">
              VIEW CHART >
            </button>
          </td>
        </tr>
      `;
    }).join('');

    // 2. Mobile Cards
    mobileContainer.innerHTML = filtered.map(s => {
      const isLong = s.side === 'LONG';
      const badgeCls = isLong ? 'badge-drift-long' : 'badge-drift-short';
      const arrow = isLong ? '▲' : '▼';

      let outcomeBadge = '<span class="badge-pending">⏳ PENDING</span>';
      if (s.win_status === 'TP1_HIT' || s.win_status === 'PROFIT') {
        outcomeBadge = '<span class="badge-win">🎯 TP1 HIT</span>';
      } else if (s.win_status === 'SL_HIT' || s.win_status === 'LOSS') {
        outcomeBadge = '<span class="badge-loss">⛔ SL HIT</span>';
      }

      const timeStr = formatWIB(s.cross_time);

      return `
        <div class="sig-card clickable-row" data-symbol="${escapeHtml(s.symbol)}">
          <div class="sig-card-header">
            <div class="sig-card-title">
              <span class="tag-symbol">${escapeHtml(s.symbol)}</span>
              <span class="badge-drift ${badgeCls}">${arrow} ${escapeHtml(s.side)}, 2H</span>
            </div>
            <div>${outcomeBadge}</div>
          </div>

          <div class="sig-card-metrics">
            <div class="sig-metric-item">
              <span class="sig-metric-label">Entry</span>
              <span class="sig-metric-val color-cyan">$${formatPrice(s.entry_price || s.cross_price)}</span>
            </div>
            <div class="sig-metric-item">
              <span class="sig-metric-label">SL</span>
              <span class="sig-metric-val color-short">$${formatPrice(s.stop_loss)}</span>
            </div>
            <div class="sig-metric-item">
              <span class="sig-metric-label">TP1</span>
              <span class="sig-metric-val color-long">$${formatPrice(s.tp1)}</span>
            </div>
          </div>

          <div class="sig-card-footer">
            <span style="font-family:var(--font-mono); font-size:0.7rem; color:var(--text-muted);">${escapeHtml(timeStr)}</span>
            <button class="btn-outline" style="padding:5px 12px; font-size:0.72rem; color:var(--cyan); border-color:var(--border-cyan);">
              VIEW CHART >
            </button>
          </div>
        </div>
      `;
    }).join('');

    // Attach row / card click handlers
    document.querySelectorAll('.clickable-row').forEach(el => {
      el.addEventListener('click', () => {
        const sym = el.getAttribute('data-symbol');
        const sig = list.find(item => item.symbol === sym);
        if (sig) openChartModal(sig);
      });
    });
  }

  function renderScanView() {
    if (liveSignals === null) {
      const idleHtml = `
        <div style="text-align:center; padding:36px; color:var(--text-muted); font-family:var(--font-mono); font-size:0.85rem;">
          Click "Launch 2H Scan" to run a MACD scan.
        </div>`;
      tbody.innerHTML = `<tr><td colspan="10">${idleHtml}</td></tr>`;
      mobileContainer.innerHTML = idleHtml;
      return;
    }
    renderSignals(liveSignals);
  }

  // Load Profit/Loss History (profitloss_history table)
  async function loadProfitLossHistory() {
    const search = filterSymbol.value.toUpperCase().trim();
    const side = filterSide.value;
    const filterRes = filterResult.value;
    const dateVal = filterDate.value;

    const params = new URLSearchParams({ limit: '100' });
    if (search) params.append('symbol', search);
    if (side) params.append('direction', side);
    if (filterRes && filterRes !== 'PENDING') params.append('result', filterRes);
    if (dateVal) params.append('date', dateVal);

    try {
      const res = await fetch(`/api/profitloss-history?${params.toString()}`);
      const data = await res.json();
      pnlHistory = data.history || [];
      const sum = data.summary || {};

      if (!search && !side && !filterRes && !dateVal) {
        if (statTotalPnl && sum.total_pnl_percent !== undefined) {
          const pnlVal = sum.total_pnl_percent || 0.0;
          statTotalPnl.textContent = `${pnlVal >= 0 ? '+' : ''}${pnlVal.toFixed(2)}%`;
          statTotalPnl.className = `hud-stat ${pnlVal >= 0 ? 'color-long' : 'color-short'}`;
        }
        if (statPnlSub && sum.total_trades !== undefined) {
          statPnlSub.textContent = `${sum.total_trades} Closed Trades`;
        }
      }

      renderProfitLossHistory(pnlHistory);
    } catch (e) {
      console.error('ProfitLoss history load error:', e);
    }
  }

  function renderProfitLossHistory(list) {
    if (list.length === 0) {
      const emptyHtml = `
        <div style="text-align:center; padding:36px; color:var(--text-muted); font-family:var(--font-mono); font-size:0.85rem;">
          No resolved trade records matching current filter criteria in profitloss_history.
        </div>`;
      pnlTbody.innerHTML = `<tr><td colspan="10">${emptyHtml}</td></tr>`;
      pnlMobileContainer.innerHTML = emptyHtml;
      return;
    }

    // 1. Desktop Table
    pnlTbody.innerHTML = list.map(item => {
      const isProfit = item.result === 'PROFIT';
      const badgeHtml = isProfit
        ? '<span class="badge-win">🎯 PROFIT</span>'
        : '<span class="badge-loss">⛔ LOSS</span>';
      const pnlColor = isProfit ? 'var(--long)' : 'var(--short)';
      const pnlSign = item.pnl_percent >= 0 ? '+' : '';

      return `
        <tr>
          <td class="tag-symbol">${escapeHtml(item.symbol)}</td>
          <td><span class="badge-drift ${item.direction === 'LONG' ? 'badge-drift-long' : 'badge-drift-short'}">${escapeHtml(item.direction)}</span></td>
          <td style="color:var(--pink); font-weight:700;">${escapeHtml(item.timeframe || '2h')}</td>
          <td style="color:var(--cyan); font-weight:700;">$${formatPrice(item.entry_price)}</td>
          <td style="font-weight:700;">$${formatPrice(item.exit_price)}</td>
          <td>${badgeHtml}</td>
          <td style="color:${pnlColor}; font-weight:800;">${pnlSign}${item.pnl_percent.toFixed(2)}%</td>
          <td style="color:var(--pink); font-weight:700;">1:${escapeHtml(item.risk_reward_ratio || 1.5)}</td>
          <td style="color:var(--text-muted); font-size:0.75rem;">${escapeHtml(formatWIB(item.cross_time))}</td>
          <td style="color:var(--text-secondary); font-size:0.75rem;">${escapeHtml(formatWIB(item.exit_time))}</td>
        </tr>
      `;
    }).join('');

    // 2. Mobile Card Deck
    pnlMobileContainer.innerHTML = list.map(item => {
      const isProfit = item.result === 'PROFIT';
      const badgeHtml = isProfit
        ? '<span class="badge-win">🎯 PROFIT</span>'
        : '<span class="badge-loss">⛔ LOSS</span>';
      const pnlColor = isProfit ? 'var(--long)' : 'var(--short)';
      const pnlSign = item.pnl_percent >= 0 ? '+' : '';

      return `
        <div class="sig-card">
          <div class="sig-card-header">
            <div class="sig-card-title">
              <span class="tag-symbol">${escapeHtml(item.symbol)}</span>
              <span class="badge-drift ${item.direction === 'LONG' ? 'badge-drift-long' : 'badge-drift-short'}">${escapeHtml(item.direction)}, ${escapeHtml(item.timeframe)}</span>
            </div>
            <div>${badgeHtml}</div>
          </div>

          <div class="sig-card-metrics">
            <div class="sig-metric-item">
              <span class="sig-metric-label">Entry</span>
              <span class="sig-metric-val color-cyan">$${formatPrice(item.entry_price)}</span>
            </div>
            <div class="sig-metric-item">
              <span class="sig-metric-label">Exit Price</span>
              <span class="sig-metric-val">$${formatPrice(item.exit_price)}</span>
            </div>
            <div class="sig-metric-item">
              <span class="sig-metric-label">Net PnL</span>
              <span class="sig-metric-val" style="color:${pnlColor};">${pnlSign}${item.pnl_percent.toFixed(2)}%</span>
            </div>
          </div>

          <div class="sig-card-footer">
            <span style="font-family:var(--font-mono); font-size:0.68rem; color:var(--text-muted);">
              Signal: ${escapeHtml(formatWIB(item.cross_time))}
            </span>
            <span style="font-family:var(--font-mono); font-size:0.68rem; color:var(--text-secondary);">
              Exit: ${escapeHtml(formatWIB(item.exit_time))}
            </span>
          </div>
        </div>
      `;
    }).join('');
  }

  // Load Watchlist
  async function loadWatchlist() {
    try {
      const res = await fetch('/api/watchlist');
      const data = await res.json();
      watchlist = data.watchlist || [];
      renderWatchlist();
    } catch (e) {
      console.error('Watchlist error:', e);
    }
  }

  function renderWatchlist() {
    watchlistGrid.innerHTML = watchlist.map(item => `
      <div class="wl-chip">
        <div>
          <div style="font-family:var(--font-display); font-weight:800; font-size:0.92rem; color:#ffffff;">
            ${escapeHtml(item.symbol)}
          </div>
          <div style="font-family:var(--font-mono); font-size:0.68rem; color:var(--text-muted);">
            ${escapeHtml(item.notes || 'USDT-M Futures')}
          </div>
        </div>
        <div style="display:flex; align-items:center; gap:12px;">
          <label class="switch">
            <input type="checkbox" class="wl-toggle" data-symbol="${escapeHtml(item.symbol)}" ${item.is_active ? 'checked' : ''}>
            <span class="slider"></span>
          </label>
          <button class="btn-del wl-delete" data-symbol="${escapeHtml(item.symbol)}">&times;</button>
        </div>
      </div>
    `).join('');

    document.querySelectorAll('.wl-toggle').forEach(input => {
      input.addEventListener('change', async (e) => {
        const sym = e.target.getAttribute('data-symbol');
        const active = e.target.checked;
        await authFetch(`/api/watchlist/${encodeURIComponent(sym)}/toggle`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ is_active: active }),
        });
        await loadSummary();
      });
    });

    document.querySelectorAll('.wl-delete').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const sym = e.target.getAttribute('data-symbol');
        if (confirm(`Remove ${sym} from Watchlist?`)) {
          await authFetch(`/api/watchlist/${encodeURIComponent(sym)}`, { method: 'DELETE' });
          await loadWatchlist();
          await loadSummary();
        }
      });
    });
  }

  // Add Watchlist Symbol
  btnAddSymbol.addEventListener('click', async () => {
    const sym = inputNewSymbol.value.trim();
    if (!sym) return;
    try {
      const res = await authFetch('/api/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: sym, notes: 'User Added' }),
      });
      if (res.ok) {
        inputNewSymbol.value = '';
        await loadWatchlist();
        await loadSummary();
      }
    } catch (e) {
      console.error('Failed to add symbol:', e);
    }
  });

  // Launch 2H Scan
  async function runScan() {
    const lookback = document.getElementById('select-lookback').value;
    const includeCurrent = document.getElementById('select-forming').value;

    btnScan.disabled = true;
    scanIcon.innerHTML = `<span class="spinner"></span>`;
    scanLabel.textContent = 'DRIFTING 2H...';

    try {
      const params = new URLSearchParams({
        timeframe: '2h',
        lookback: lookback,
        include_current: includeCurrent,
      });

      const res = await authFetch(`/api/macd-scan?${params.toString()}`);
      if (!res.ok) throw new Error(`Scan failed with status ${res.status}`);
      const data = await res.json();
      liveSignals = data.signals || [];
      activeTab = 'scan';
      switchView('signals');
      updateNavButtons();
      renderSignals(liveSignals);
      await loadSummary();
    } catch (e) {
      console.error('Scan error:', e);
    } finally {
      btnScan.disabled = false;
      scanIcon.innerHTML = '🏎️💨';
      scanLabel.textContent = 'Launch 2H Scan';
    }
  }

  // Load Database Signals History
  async function loadHistory() {
    const search = filterSymbol.value.toUpperCase().trim();
    const side = filterSide.value;
    const filterRes = filterResult.value;
    const dateVal = filterDate.value;

    const params = new URLSearchParams({ timeframe: '2h', limit: '100' });
    if (search) params.append('symbol', search);
    if (side) params.append('direction', side);
    if (filterRes) params.append('result', filterRes);
    if (dateVal) params.append('date', dateVal);

    try {
      const res = await fetch(`/api/signals?${params.toString()}`);
      const data = await res.json();
      dbSignals = data.signals || [];
      renderSignals(dbSignals);
    } catch (e) {
      console.error('History load error:', e);
    }
  }

  // Evaluate Trade Outcomes
  btnEval.addEventListener('click', async () => {
    btnEval.disabled = true;
    btnEval.textContent = 'Evaluating...';
    try {
      await authFetch('/api/evaluate', { method: 'POST' });
      await loadSummary();
      if (activeTab === 'history') {
        await loadHistory();
      } else if (activeTab === 'profitloss') {
        await loadProfitLossHistory();
      } else {
        renderScanView();
      }
    } catch (e) {
      console.error('Eval error:', e);
    } finally {
      btnEval.disabled = false;
      btnEval.textContent = '⚖️ Evaluate Outcomes';
    }
  });

  // View Switching
  function switchView(view) {
    if (view === 'signals') {
      viewSignals.style.display = '';
      mobileContainer.style.display = '';
      viewProfitLoss.style.display = 'none';
      viewWatchlist.style.display = 'none';
    } else if (view === 'profitloss') {
      viewSignals.style.display = 'none';
      mobileContainer.style.display = 'none';
      viewProfitLoss.style.display = 'block';
      viewWatchlist.style.display = 'none';
    } else if (view === 'watchlist') {
      viewSignals.style.display = 'none';
      mobileContainer.style.display = 'none';
      viewProfitLoss.style.display = 'none';
      viewWatchlist.style.display = 'block';
    }
  }

  function updateNavButtons() {
    tabScan.className = activeTab === 'scan' ? 'nav-btn active' : 'nav-btn';
    tabHistory.className = activeTab === 'history' ? 'nav-btn active' : 'nav-btn';
    tabProfitLoss.className = activeTab === 'profitloss' ? 'nav-btn active' : 'nav-btn';
    tabWatchlist.className = activeTab === 'watchlist' ? 'nav-btn active' : 'nav-btn';
  }

  tabScan.addEventListener('click', () => {
    activeTab = 'scan';
    switchView('signals');
    updateNavButtons();
    renderScanView();
  });

  tabHistory.addEventListener('click', () => {
    activeTab = 'history';
    switchView('signals');
    updateNavButtons();
    loadHistory();
  });

  tabProfitLoss.addEventListener('click', () => {
    activeTab = 'profitloss';
    switchView('profitloss');
    updateNavButtons();
    loadProfitLossHistory();
  });

  tabWatchlist.addEventListener('click', () => {
    activeTab = 'watchlist';
    switchView('watchlist');
    updateNavButtons();
    loadWatchlist();
  });

  // Dynamic Filtering on inputs
  function onFilterChanged() {
    if (activeTab === 'scan') {
      renderScanView();
    } else if (activeTab === 'history') {
      loadHistory();
    } else if (activeTab === 'profitloss') {
      loadProfitLossHistory();
    }
  }

  filterSymbol.addEventListener('input', onFilterChanged);
  filterDate.addEventListener('change', onFilterChanged);
  filterSide.addEventListener('change', onFilterChanged);
  filterResult.addEventListener('change', onFilterChanged);

  // Chart & Telemetry Modal
  async function openChartModal(sig) {
    modal.style.display = 'flex';
    document.getElementById('modal-badge').textContent = '2H // UTC+7';
    document.getElementById('modal-title').textContent = `${sig.symbol} // ${sig.side} 2H`;
    document.getElementById('modal-title').style.color = sig.side === 'LONG' ? 'var(--long)' : 'var(--short)';
    document.getElementById('modal-time').textContent = `Cross Time: ${formatWIB(sig.cross_time)} (Confirmed: ${sig.confirmed}) • Timezone: UTC+7 (WIB)`;

    document.getElementById('detail-entry').textContent = `$${formatPrice(sig.entry_price || sig.cross_price)}`;
    document.getElementById('detail-sl').textContent = `$${formatPrice(sig.stop_loss)}`;
    document.getElementById('detail-tp').textContent = `$${formatPrice(sig.tp1)}`;
    document.getElementById('detail-rr').textContent = `1:${sig.risk_reward_ratio || 1.5}`;

    document.getElementById('detail-macd').textContent = sig.macd != null ? sig.macd.toFixed(4) : '-';
    document.getElementById('detail-signal').textContent = sig.signal != null ? sig.signal.toFixed(4) : '-';
    document.getElementById('detail-hist').textContent = sig.histogram != null ? sig.histogram.toFixed(4) : '-';
    document.getElementById('detail-hist').style.color = (sig.histogram >= 0) ? 'var(--long)' : 'var(--short)';

    let statusText = 'IN PROGRESS';
    if (sig.win_status === 'TP1_HIT' || sig.win_status === 'PROFIT') statusText = 'TARGET REACHED (PROFIT)';
    if (sig.win_status === 'SL_HIT' || sig.win_status === 'LOSS') statusText = 'STOPPED OUT (LOSS)';
    document.getElementById('detail-outcome').textContent = statusText;

    await renderChart(sig);
  }

  async function renderChart(sig) {
    chartViewport.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--cyan);font-family:var(--font-display);font-size:0.85rem;"><span class="spinner" style="margin-right:10px;"></span> LOADING 2H CANDLESTICK TELEMETRY (UTC+7)...</div>';

    try {
      const res = await fetch(`/api/klines?symbol=${encodeURIComponent(sig.symbol)}&limit=80`);
      const payload = await res.json();
      chartViewport.innerHTML = '';

      if (window.LightweightCharts) {
        const chart = LightweightCharts.createChart(chartViewport, {
          width: chartViewport.clientWidth,
          height: chartViewport.clientHeight || 360,
          layout: {
            background: { color: '#07080c' },
            textColor: '#94a3b8',
            fontFamily: "'JetBrains Mono', monospace",
          },
          grid: {
            vertLines: { color: 'rgba(255, 255, 255, 0.04)' },
            horzLines: { color: 'rgba(255, 255, 255, 0.04)' },
          },
          crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
          },
          rightPriceScale: {
            borderColor: 'rgba(255, 255, 255, 0.08)',
          },
          timeScale: {
            borderColor: 'rgba(255, 255, 255, 0.08)',
            timeVisible: true,
            secondsVisible: false,
          },
          localization: {
            dateFormat: 'yyyy-MM-dd',
          },
        });

        const candleSeries = chart.addCandlestickSeries({
          upColor: '#10b981',
          downColor: '#fb7185',
          borderVisible: false,
          wickUpColor: '#10b981',
          wickDownColor: '#fb7185',
        });

        // Shift candle timestamps by +7 hours to render natively in UTC+7 (WIB)
        const UTC7_OFFSET_SEC = 7 * 3600;
        const candles = (payload.candles || []).map(c => ({
          ...c,
          time: c.time + UTC7_OFFSET_SEC,
        }));

        candleSeries.setData(candles);

        if (sig.cross_time) {
          const crossDate = new Date(sig.cross_time);
          if (!isNaN(crossDate.getTime())) {
            const crossTsSec = Math.floor(crossDate.getTime() / 1000) + UTC7_OFFSET_SEC;
            const matchingCandle = candles.find(c => Math.abs(c.time - crossTsSec) < 3600);
            if (matchingCandle) {
              candleSeries.setMarkers([
                {
                  time: matchingCandle.time,
                  position: sig.side === 'LONG' ? 'belowBar' : 'aboveBar',
                  color: sig.side === 'LONG' ? '#10b981' : '#fb7185',
                  shape: sig.side === 'LONG' ? 'arrowUp' : 'arrowDown',
                  text: `${sig.side} SIGNAL`,
                },
              ]);
            }
          }
        }

        if (sig.entry_price) {
          candleSeries.createPriceLine({
            price: sig.entry_price,
            color: '#22d3ee',
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Solid,
            axisLabelVisible: true,
            title: 'ENTRY',
          });
        }

        if (sig.tp1) {
          candleSeries.createPriceLine({
            price: sig.tp1,
            color: '#10b981',
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: 'TP1 (1:1.5)',
          });
        }

        if (sig.stop_loss) {
          candleSeries.createPriceLine({
            price: sig.stop_loss,
            color: '#fb7185',
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: 'STOP LOSS',
          });
        }

        chart.timeScale().fitContent();

        window.addEventListener('resize', () => {
          if (chart && chartViewport) {
            chart.applyOptions({ width: chartViewport.clientWidth });
          }
        });
      }
    } catch (e) {
      console.error('Failed to render chart:', e);
      chartViewport.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--short);font-family:var(--font-mono);font-size:0.8rem;">Failed to fetch candle data from Binance.</div>';
    }
  }

  modalClose.addEventListener('click', () => { modal.style.display = 'none'; });
  window.addEventListener('click', (e) => { if (e.target === modal) modal.style.display = 'none'; });

  // Initialization
  btnScan.addEventListener('click', runScan);
  checkHealth();
  loadSummary();
  loadWatchlist();
  renderScanView();
});
