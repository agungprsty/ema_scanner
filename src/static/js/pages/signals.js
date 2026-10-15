// Signals: every recorded signal, filtered on the server and paged; the filters live in the URL so a
// filtered list can be shared
import { $, emptyHtml, getJson, initChrome } from '../core.js';
import { STATUS_OPTIONS, bindRowLinks, signalCardHtml, signalRowHtml } from '../cards.js';
import { debounce, escapeHtml, formatR, plural } from '../format.js';

const PAGE_SIZE = 20;
const FILTERS = ['strategy', 'symbol', 'side', 'status', 'source'];

initChrome();

const tbody = $('signals-tbody');
const cards = $('signals-cards');
const caption = $('caption');
const pager = $('pager');
const inputs = {
  strategy: $('filter-strategy'),
  symbol: $('filter-symbol'),
  side: $('filter-side'),
  status: $('filter-status'),
  source: $('filter-source'),
};
let page = 0;
let request = 0;

$('filter-status').insertAdjacentHTML('beforeend', STATUS_OPTIONS
  .map(([value, label]) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`).join(''));

function readUrl() {
  const params = new URLSearchParams(window.location.search);
  FILTERS.forEach((name) => {
    const value = params.get(name) || '';
    const el = inputs[name];
    if (el.tagName === 'SELECT' && value && !Array.from(el.options).some(o => o.value === value)) {
      el.insertAdjacentHTML('beforeend', `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`);
    }
    el.value = value;
  });
  page = Math.max(0, (Number(params.get('page')) || 1) - 1);
}

function writeUrl() {
  const params = new URLSearchParams();
  FILTERS.forEach((name) => {
    const value = inputs[name].value.trim();
    if (value) params.set(name, name === 'symbol' ? value.toUpperCase() : value);
  });
  if (page) params.set('page', String(page + 1));
  const query = params.toString();
  window.history.replaceState(null, '', query ? `?${query}` : window.location.pathname);
}

function apiParams() {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(page * PAGE_SIZE) });
  const [key, version] = inputs.strategy.value.split('@');
  if (key) params.set('strategy_key', key);
  if (version) params.set('strategy_version', version);
  const symbol = inputs.symbol.value.trim().toUpperCase();
  if (symbol) params.set('symbol', symbol);
  if (inputs.side.value) params.set('side', inputs.side.value);
  if (inputs.status.value) params.set('win_status', inputs.status.value);
  if (inputs.source.value) params.set('source', inputs.source.value);
  return params;
}

function hasFilters() {
  return FILTERS.some(name => inputs[name].value.trim());
}

function setMessage(message, isError = false) {
  tbody.innerHTML = `<tr><td colspan="7">${emptyHtml(message, isError)}</td></tr>`;
  cards.innerHTML = emptyHtml(message, isError);
  pager.hidden = true;
}

function captionText(total, sum) {
  if (!total) return '';
  let text = `${plural(total, 'signal')}, newest first: ${sum.open_count || 0} open, ${sum.closed_count || 0} closed.`;
  if (sum.excluded_count) text += ` ${sum.excluded_count} outside the ${sum.strategy_version ? `v${sum.strategy_version} ` : ''}headline stats.`;
  if (sum.eligible_closed_count >= 100) text += ` Counted closed trades net ${formatR(sum.eligible_net_r)}.`;
  return text;
}

function renderPager(total, count) {
  const start = page * PAGE_SIZE;
  if (!total || (total <= PAGE_SIZE && start === 0)) {
    pager.hidden = true;
    return;
  }
  pager.querySelector('.pager-info').textContent = `${start + 1}–${start + count} of ${total}`;
  pager.querySelector('[data-step="-1"]').disabled = start === 0;
  pager.querySelector('[data-step="1"]').disabled = start + count >= total;
  pager.hidden = false;
}

async function load() {
  const current = ++request;
  writeUrl();
  try {
    const data = await getJson(`/api/signals?${apiParams()}`);
    if (current !== request) return;
    const signals = data.signals || [];
    if (!signals.length && data.total && page > 0) {
      page = Math.floor((data.total - 1) / PAGE_SIZE);
      load();
      return;
    }
    caption.textContent = captionText(data.total, data.summary || {});
    if (!signals.length) {
      setMessage(hasFilters() ? 'No signals match these filters.' : 'No signals recorded yet.');
      return;
    }
    tbody.innerHTML = signals.map(signalRowHtml).join('');
    cards.innerHTML = signals.map(signalCardHtml).join('');
    renderPager(data.total, signals.length);
  } catch (e) {
    if (current !== request) return;
    caption.textContent = '';
    setMessage(`Could not load signals: ${e.message}`, true);
  }
}

function onFilterChanged() {
  page = 0;
  load();
}

async function loadVersions() {
  try {
    const data = await getJson('/api/versions');
    const selected = inputs.strategy.value;
    const options = (data.strategies || [])
      .filter(s => s.ref !== selected)
      .map(s => `<option value="${escapeHtml(s.ref)}">${escapeHtml(s.ref)}${s.status ? ` (${escapeHtml(s.status)})` : ''}</option>`);
    inputs.strategy.insertAdjacentHTML('beforeend', options.join(''));
  } catch { /* the list still works without the strategy names */ }
}

pager.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-step]');
  if (!btn || btn.disabled) return;
  page = Math.max(0, page + Number(btn.dataset.step));
  load();
  pager.closest('.panel').scrollIntoView({ block: 'start' });
});
$('filters').addEventListener('submit', e => e.preventDefault());
inputs.symbol.addEventListener('input', debounce(onFilterChanged, 300));
['strategy', 'side', 'status', 'source'].forEach(name => inputs[name].addEventListener('change', onFilterChanged));
bindRowLinks(tbody);

readUrl();
loadVersions();
setMessage('Loading…');
load();
