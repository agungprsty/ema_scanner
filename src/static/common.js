// Helpers shared by the dashboard (app.js) and the signal page (signal.js).
// Loaded first as a classic script, so its top-level declarations are visible to the page script after it.

const TZ = 'Asia/Jakarta';

const TF_UNIT_MS = { m: 60 * 1000, h: 60 * 60 * 1000, d: 24 * 60 * 60 * 1000 };

// Defaults until /api/info answers
const config = { timeframe: '', rsi_period: 14, oversold: 28, overbought: 72, fee_percent: 0.1 };

const $ = (id) => document.getElementById(id);

let toastTimer = null;
function showToast(message) {
  const toast = $('toast');
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

// Same formula as the server: PnL after fees, and R against the signal's own planned risk (its stop loss after fees), not the fill's distance to the stop
function tradeReturn(sig, entry, exit) {
  if (!(entry > 0) || !(exit > 0)) return null;
  const dir = sig.side === 'LONG' ? 1 : -1;
  const pnl = dir * (exit - entry) / entry * 100 - config.fee_percent;
  // The server's 1R, which stays the risk at the fill after an admin moves the stop
  const risk = sig.risk_percent ?? Math.abs(sig.entry_price - sig.stop_loss) / sig.entry_price * 100 + config.fee_percent;
  return { pnl, r: risk > 0 ? pnl / risk : null };
}

// API helpers
// Error envelope: { message, errors: [{ field, message }] }; a proxy may answer with HTML instead
async function responseError(res) {
  if (res.status === 401) {
    window.location.assign('/login');
    return 'Session expired, redirecting to login...';
  }
  if (res.status === 403) {
    return 'Administrator only';
  }
  if (res.status === 429) {
    const wait = Number(res.headers.get('Retry-After'));
    return wait > 0 ? `too many requests, try again in ${wait}s` : 'too many requests, try again shortly';
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (body && typeof body.message === 'string' && body.message) {
    const details = (Array.isArray(body.errors) ? body.errors : [])
      .filter(e => e && e.message)
      .map(e => (e.field ? `${e.field}: ${e.message}` : e.message));
    return details.length ? `${body.message} (${details.join('; ')})` : body.message;
  }
  return `request failed (HTTP ${res.status})`;
}

// Returns the whole envelope; the payload is in .data, paging in .meta
async function getJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(await responseError(res));
  return res.json();
}

// Authentication & Session
async function initAuth() {
  try {
    const envelope = await getJson('/api/auth/me');
    const user = envelope.data;
    if (user) {
      document.body.dataset.role = user.role;
      const userNameEl = document.getElementById('user-name');
      const userRoleEl = document.getElementById('user-role');
      if (userNameEl) userNameEl.textContent = user.username;
      if (userRoleEl) {
        userRoleEl.textContent = user.role === 'administrator' ? 'Admin' : 'User';
        if (user.role === 'administrator') userRoleEl.classList.add('admin');
      }
    }
  } catch {
    // getJson redirects to /login on 401
  }
}

function bindLogout() {
  const btnLogout = document.getElementById('btn-logout');
  if (!btnLogout) return;
  btnLogout.addEventListener('click', async () => {
    try {
      await fetch('/api/auth/logout', { method: 'POST' });
    } finally {
      window.location.assign('/');
    }
  });
}

// Strategy settings from /api/info into config; returns the whole info object
async function loadStrategyConfig() {
  const info = (await getJson('/api/info')).data;
  Object.assign(config, info.strategy || {});
  if (info.timeframe) config.timeframe = info.timeframe;
  return info;
}
