(function() {
    const state = {
        symbol: '',
        status: '',
        date_from: '',
        date_to: '',
        limit: 20,
        cursor: null,
        cursors: [],
        sort_by: 'created_at',
        sort_order: 'desc',
        has_more: false,
        next_cursor: null,
    };

    async function fetchJSON(url, options = {}) {
        try {
            const res = await fetch(url, options);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return await res.json();
        } catch (e) {
            console.error('Fetch error:', e);
            return null;
        }
    }

    function formatPrice(val) {
        if (val == null) return '—';
        return Number(val).toLocaleString(undefined, { maximumFractionDigits: 4 });
    }

    function formatPnl(pct) {
        if (pct == null) return '—';
        const cls = pct > 0 ? 'green' : 'red';
        const sign = pct > 0 ? '+' : '';
        return `<span class="${cls}">${sign}${pct.toFixed(2)}%</span>`;
    }

    function formatDuration(hours) {
        if (hours == null || hours === 0) return '—';
        if (hours < 1) return `${Math.round(hours * 60)}m`;
        if (hours < 24) return `${hours.toFixed(1)}h`;
        return `${(hours / 24).toFixed(1)}d`;
    }

    function formatRR(rr, actual = false) {
        if (rr == null || rr === 0) return '<span class="rr-neutral">—</span>';
        const cls = rr >= 1 ? 'rr-positive' : rr > 0 ? 'rr-neutral' : 'rr-negative';
        const label = actual ? 'Actual' : 'Planned';
        return `<span class="${cls}">${rr.toFixed(2)}${actual ? ' (A)' : ''}</span>`;
    }

    function formatMaxDrawdown(dd) {
        if (dd == null) return '—';
        const cls = dd > 5 ? 'red' : dd > 2 ? 'yellow' : 'green';
        return `<span class="${cls}">${dd.toFixed(2)}%</span>`;
    }

    function computeDuration(trade) {
        const closed = trade.closed_at || trade.timestamps?.closed_at;
        const created = trade.created_at || trade.timestamps?.created_at;
        if (!closed || !created) return null;
        try {
            const c_dt = new Date(closed.replace('Z', '+00:00'));
            const cr_dt = new Date(created.replace('Z', '+00:00'));
            return (c_dt - cr_dt) / 3600000;
        } catch {
            return null;
        }
    }

    function formatDate(iso) {
        if (!iso) return '—';
        try {
            const d = new Date(iso.replace('Z', '+00:00'));
            return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        } catch {
            return iso;
        }
    }

    function statusClass(status) {
        if (!status) return '';
        const s = status.toLowerCase().replace(/[\s_-]+/g, '_');
        return `status-${s}`;
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    let currentTradeId = null;

    function canClose(status) {
        return ['PENDING', 'LIMIT_PLACED', 'FILLED', 'TP1_HIT'].includes(status);
    }

    function canExpire(status) {
        return status === 'LIMIT_PLACED';
    }

    function isTerminal(status) {
        return ['CLOSED_TP', 'CLOSED_SL', 'CLOSED_BEP', 'EXPIRED'].includes(status);
    }

    function openCloseModal(tradeId, symbol) {
        currentTradeId = tradeId;
        document.getElementById('modal-symbol').textContent = symbol;
        document.getElementById('close-modal').style.display = '';
    }

    function closeModal() {
        currentTradeId = null;
        document.getElementById('close-modal').style.display = 'none';
    }

    async function confirmClose(status) {
        if (!currentTradeId) return;
        const res = await fetchJSON(`/api/trades/${currentTradeId}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status }),
        });
        closeModal();
        if (res && res.success) {
            loadSummary();
            loadTrades();
        } else {
            alert('Failed to update trade: ' + (res?.error || 'unknown error'));
        }
    }

    async function confirmExpired(tradeId) {
        const res = await fetchJSON(`/api/trades/${tradeId}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'EXPIRED' }),
        });
        if (res && res.success) {
            loadSummary();
            loadTrades();
        } else {
            alert('Failed to expire trade: ' + (res?.error || 'unknown error'));
        }
    }

    async function loadSummary() {
        const summaryParams = new URLSearchParams();
        if (state.symbol) summaryParams.set('symbol', state.symbol);
        const data = await fetchJSON(`/api/summary?${summaryParams}`);
        if (!data || data.error) {
            const container = document.getElementById('summary');
            container.innerHTML = data ? `<div class="error">${escapeHtml(data.error)}</div>` : '';
            return;
        }

        const container = document.getElementById('summary');
        const cards = [
            { label: 'Total Trades', value: data.total_trades },
            { label: 'Win Rate', value: `${data.win_rate_pct}%`, cls: data.win_rate_pct >= 50 ? 'green' : 'red' },
            { label: 'Avg PnL', value: formatPnl(data.avg_pnl_pct) },
            { label: 'Best', value: `${formatPnl(data.best_trade_pct)}` },
            { label: 'Max Drawdown', value: formatMaxDrawdown(data.max_drawdown_pct), cls: 'red' },
        ];

        container.innerHTML = cards.map(c => `
            <div class="card">
                <div class="card-label">${c.label}</div>
                <div class="card-value ${c.cls || ''}">${c.value}</div>
            </div>
        `).join('');
    }

    async function loadTrades() {
        const container = document.getElementById('trades-container');
        container.innerHTML = '<div class="loading">Loading trades...</div>';

        const params = new URLSearchParams({
            symbol: state.symbol || '',
            status: state.status || '',
            limit: state.limit,
            sort_by: state.sort_by,
            sort_order: state.sort_order,
            date_from: state.date_from || '',
            date_to: state.date_to || '',
        });
        if (state.cursor) {
            params.set('cursor', state.cursor);
        }

        const data = await fetchJSON(`/api/trades?${params}`);
        if (!data) {
            container.innerHTML = '<div class="error">Failed to load trades.</div>';
            return;
        }

        if (data.error) {
            container.innerHTML = `<div class="error">${escapeHtml(data.error)}</div>`;
            return;
        }

        const { trades, has_more, next_cursor } = data;
        state.has_more = has_more;
        state.next_cursor = next_cursor;

        if (trades.length === 0) {
            container.innerHTML = '<div class="loading">No trades found.</div>';
            return;
        }

        const rows = trades.map(t => `
            <tr>
                <td>${formatDate(t.created_at)}</td>
                <td><strong>${t.symbol}</strong></td>
                <td><span class="status-badge status-${t.side}">${t.side}</span></td>
                <td>${formatPrice(t.entry_price)}</td>
                <td>${formatPrice(t.sl_price)}</td>
                <td>${formatPrice(t.tp1_price)}</td>
                <td>${t.quantity ? Number(t.quantity).toFixed(4) : '—'}</td>
                <td><span class="status-badge ${statusClass(t.status)}">${t.status}</span></td>
                <td>${formatDuration(computeDuration(t))}</td>
                <td>${formatPnl(t.pnl_pct)}</td>
                <td>${formatPrice(t.exit_price)}</td>
                <td>${formatRR(t.rr_planned)}</td>
                <td>${formatRR(t.rr_actual, true)}</td>
                <td class="actions-cell">
                    ${canClose(t.status) ? `<button class="btn-sm btn-danger" onclick="window.__history.openCloseModal('${t.trade_id}','${t.symbol}')">Close</button>` : ''}
                    ${canExpire(t.status) ? `<button class="btn-sm btn-secondary" onclick="window.__history.confirmExpired('${t.trade_id}')">Expired</button>` : ''}
                    ${isTerminal(t.status) ? '—' : ''}
                </td>
            </tr>
        `).join('');

        const pageNum = state.cursors.length;
        const showingFrom = pageNum * state.limit + 1;
        const showingTo = showingFrom + trades.length - 1;

        container.innerHTML = `
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Symbol</th>
                            <th>Side</th>
                            <th>Entry</th>
                            <th>SL</th>
                            <th>TP1</th>
                            <th>Qty</th>
                            <th>Status</th>
                            <th>Duration</th>
                            <th>PnL%</th>
                            <th>Exit Price</th>
                            <th>R:R Planned</th>
                            <th>R:R Actual</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
            <div class="pagination">
                <span>Showing ${showingFrom}–${showingTo}</span>
                <div style="display:flex;gap:8px;">
                    <button class="btn-secondary" onclick="window.__history.prev()" ${state.cursors.length === 0 ? 'disabled' : ''}>Previous</button>
                    <button class="btn-secondary" onclick="window.__history.next()" ${!state.has_more ? 'disabled' : ''}>Next</button>
                </div>
            </div>
        `;
    }

    window.__history = {
        prev: () => {
            if (state.cursors.length > 0) {
                state.cursor = state.cursors.pop();
                loadTrades();
            }
        },
        next: () => {
            if (state.has_more && state.next_cursor) {
                state.cursors.push(state.cursor);
                state.cursor = state.next_cursor;
                loadTrades();
            }
        },
        applyFilters,
        resetFilters,
        openCloseModal,
        closeModal,
        confirmClose,
        confirmExpired,
    };

   function applyFilters() {
        state.symbol = document.getElementById('filter-symbol').value.trim();
        state.status = document.getElementById('filter-status').value;
        state.date_from = document.getElementById('filter-date-from').value;
        state.date_to = document.getElementById('filter-date-to').value;
        state.cursor = null;
        state.cursors = [];
        state.has_more = false;
        state.next_cursor = null;
        loadSummary();
        loadTrades();
    }

    function resetFilters() {
        document.getElementById('filter-symbol').value = '';
        document.getElementById('filter-status').value = '';
        document.getElementById('filter-date-from').value = '';
        document.getElementById('filter-date-to').value = '';
        applyFilters();
    }

    // Mode badge from query param
    const params = new URLSearchParams(window.location.search);
    const modeBadge = document.getElementById('mode-badge');
    if (params.get('dry_run') === 'true' || params.get('dry_run') === '1') {
        modeBadge.textContent = 'DRY RUN';
        modeBadge.classList.add('dry-run');
    } else {
        modeBadge.textContent = 'PRODUCTION';
    }

    loadSummary();
    loadTrades();
})();
