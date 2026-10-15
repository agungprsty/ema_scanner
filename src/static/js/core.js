// Page chrome and requests shared by every page: the header clock and health badge, JSON fetching with
// readable errors, the toast, and the owner session used on /ops.
import { clockFmt, escapeHtml, formatTimeOnlyWIB } from './format.js';

export const $ = (id) => document.getElementById(id);

export async function responseError(res) {
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
  if (res.status === 503) return 'service unavailable';
  return `request failed (HTTP ${res.status})`;
}

export async function getJson(url) {
  const res = await fetch(url, { credentials: 'same-origin' });
  if (!res.ok) throw new Error(await responseError(res));
  return res.json();
}

export function emptyHtml(message, isError = false) {
  return `<div class="empty${isError ? ' err' : ''}">${escapeHtml(message)}</div>`;
}

let toastTimer = null;
export function showToast(message) {
  const toast = $('toast');
  if (!toast) return;
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 2500);
}

// Header: WIB clock, API health and the next scheduled scan
function tickClock() {
  const el = $('clock');
  if (el) el.textContent = clockFmt.format(new Date());
}

export async function checkHealth() {
  const dot = $('health-dot');
  const text = $('health-text');
  const cronText = $('cron-text');
  if (!dot || !text) return;
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
  if (!cronText) return;
  try {
    const cron = await getJson('/api/cron/status');
    cronText.textContent = cron.enabled ? formatTimeOnlyWIB(cron.next_run) : 'Off';
  } catch {
    cronText.textContent = '--:--';
  }
}

// Older versions kept the API key in page storage, readable by any script; it is wiped on load
const STALE_KEY_STORAGE = ['crossline_api_key', 'tokyo_api_key'];

function forgetStoredKeys() {
  STALE_KEY_STORAGE.forEach(key => {
    try { localStorage.removeItem(key); } catch { /* storage unavailable */ }
  });
}

export function initChrome() {
  forgetStoredKeys();
  tickClock();
  setInterval(tickClock, 1000);
  checkHealth();
  setInterval(checkHealth, 60000);
}

// Owner session: the server keeps it in an HttpOnly cookie, so no page ever sees the key after sign-in.
// Writes carry the header the server requires, which a cross-site form cannot add
export const CSRF_HEADERS = { 'X-Requested-With': 'crossline' };

export async function getSession() {
  try {
    return await getJson('/api/auth/session');
  } catch {
    return { authenticated: false, configured: false };
  }
}

export async function login(apiKey) {
  const res = await fetch('/api/auth/login', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS },
    body: JSON.stringify({ api_key: apiKey }),
  });
  if (res.ok) return null;
  return res.status === 401 ? 'Wrong API key' : responseError(res);
}

export async function logout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin', headers: CSRF_HEADERS });
  } catch { /* the cookie expires on its own */ }
}

// Owner-only requests go out with the session cookie plus the CSRF header. A 401 means the session ended:
// `onUnauthorized` runs (the ops page shows its sign-in form again)
let unauthorizedHandler = null;
export function onUnauthorized(handler) {
  unauthorizedHandler = handler;
}

export async function authFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  Object.entries(CSRF_HEADERS).forEach(([k, v]) => headers.set(k, v));
  const res = await fetch(url, { ...options, headers, credentials: 'same-origin' });
  if (res.status === 401 && unauthorizedHandler) unauthorizedHandler();
  return res;
}
