document.addEventListener('DOMContentLoaded', () => {
  let activeTab = 'live';
  let liveSignals = [];
  let dbSignals = [];

  const tbody = document.getElementById('signals-tbody');
  const btnScan = document.getElementById('btn-scan');
  const scanLabel = document.getElementById('scan-label');
  const scanIcon = document.getElementById('scan-icon');
  const btnRefresh = document.getElementById('btn-refresh');
  const tabLive = document.getElementById('tab-live');
  const tabDb = document.getElementById('tab-db');
  const filterSearch = document.getElementById('filter-search');
  const filterSide = document.getElementById('filter-side');

  // Clock
  setInterval(() => {
    const now = new Date();
    document.getElementById('time-pill').textContent = now.toUTCString().slice(17, 25) + ' UTC';
  }, 1000);

  async function checkHealth() {
    try {
      const res = await fetch('/api/health');
      const data = await res.json();
      const dot = document.getElementById('health-dot');
      const text = document.getElementById('health-text');
      if (data.database && data.redis) {
        dot.className = 'dot';
        text.textContent = 'System Healthy';
      } else {
        dot.className = 'dot offline';
        text.textContent = `Degraded (DB: ${data.database ? 'OK' : 'ERR'}, Redis: ${data.redis ? 'OK' : 'ERR'})`;
      }
    } catch {
      document.getElementById('health-dot').className = 'dot offline';
      document.getElementById('health-text').textContent = 'API Offline';
    }
  }

  async function updateSummary() {
    try {
      const res = await fetch('/api/summary');
      const data = await res.json();
      document.getElementById('metric-total').textContent = data.total_signals ?? 0;
      document.getElementById('metric-long').textContent = data.long_signals ?? 0;
      document.getElementById('metric-short').textContent = data.short_signals ?? 0;
      document.getElementById('metric-symbols').textContent = data.symbols_count ?? 0;
    } catch (e) {
      console.warn('Summary fetch failed', e);
    }
  }

  function renderTable(signals) {
    const search = filterSearch.value.toUpperCase().trim();
    const side = filterSide.value;

    const filtered = signals.filter(s => {
      if (search && !s.symbol.includes(search)) return false;
      if (side && s.side !== side) return false;
      return true;
    });

    if (filtered.length === 0) {
      tbody.innerHTML = `<tr><td colspan="10" class="empty-state">No matching signals found.</td></tr>`;
      return;
    }

    tbody.innerHTML = filtered.map(s => {
      const isLong = s.side === 'LONG';
      const badgeCls = isLong ? 'badge-long' : 'badge-short';
      const histCls = s.histogram >= 0 ? 'color: var(--long);' : 'color: var(--short);';
      const statusCls = s.confirmed ? 'badge-confirmed' : '';
      const statusText = s.confirmed ? 'Confirmed' : 'Forming';
      const timeStr = s.cross_time ? s.cross_time.replace('T', ' ').replace('Z', '') : '-';

      return `
        <tr>
          <td class="symbol-cell">${s.symbol}</td>
          <td><span class="badge-signal ${badgeCls}">${s.side}</span></td>
          <td>${s.cross_price}</td>
          <td>${s.close || s.close_price || '-'}</td>
          <td>${s.macd}</td>
          <td>${s.signal}</td>
          <td style="${histCls}">${s.histogram}</td>
          <td>${s.bars_ago != null ? s.bars_ago : '-'}</td>
          <td><span class="badge-status ${statusCls}">${statusText}</span></td>
          <td style="color: var(--text-muted); font-size: 0.75rem;">${timeStr}</td>
        </tr>
      `;
    }).join('');
  }

  async function runScan() {
    const symbols = document.getElementById('input-symbols').value.trim();
    const timeframe = document.getElementById('select-timeframe').value;
    const lookback = document.getElementById('select-lookback').value;
    const includeCurrent = document.getElementById('select-forming').value;

    btnScan.disabled = true;
    scanIcon.innerHTML = `<span class="spinner"></span>`;
    scanLabel.textContent = 'Scanning Binance...';

    const params = new URLSearchParams();
    if (symbols) params.set('symbols', symbols);
    params.set('timeframe', timeframe);
    params.set('lookback', lookback);
    params.set('include_current', includeCurrent);

    try {
      const res = await fetch(`/api/macd-scan?${params.toString()}`);
      const data = await res.json();
      liveSignals = data.signals || [];
      activeTab = 'live';
      tabLive.className = 'tab-btn active';
      tabDb.className = 'tab-btn';
      renderTable(liveSignals);
      await updateSummary();
    } catch (e) {
      console.error('Scan error:', e);
      tbody.innerHTML = `<tr><td colspan="10" class="empty-state" style="color: var(--short);">Scan failed. Check server logs.</td></tr>`;
    } finally {
      btnScan.disabled = false;
      scanIcon.innerHTML = '⚡';
      scanLabel.textContent = 'Run MACD Scan';
    }
  }

  async function loadDbHistory() {
    try {
      const res = await fetch('/api/signals?limit=100');
      const data = await res.json();
      dbSignals = data.signals || [];
      renderTable(dbSignals);
    } catch (e) {
      console.error('History load error:', e);
    }
  }

  btnScan.addEventListener('click', runScan);

  btnRefresh.addEventListener('click', () => {
    if (activeTab === 'live') {
      renderTable(liveSignals);
    } else {
      loadDbHistory();
    }
    updateSummary();
  });

  tabLive.addEventListener('click', () => {
    activeTab = 'live';
    tabLive.className = 'tab-btn active';
    tabDb.className = 'tab-btn';
    renderTable(liveSignals);
  });

  tabDb.addEventListener('click', () => {
    activeTab = 'db';
    tabDb.className = 'tab-btn active';
    tabLive.className = 'tab-btn';
    loadDbHistory();
  });

  filterSearch.addEventListener('input', () => {
    renderTable(activeTab === 'live' ? liveSignals : dbSignals);
  });

  filterSide.addEventListener('change', () => {
    renderTable(activeTab === 'live' ? liveSignals : dbSignals);
  });

  // Initial loads
  checkHealth();
  updateSummary();
  setInterval(checkHealth, 15000);
});
