// Formatting shared by every page. Signed figures use the minus sign U+2212, two decimals by default and
// an R suffix; rates always travel with their sample, e.g. "44.4% (4/9)". Times are shown in WIB (UTC+7).

export const TZ = 'Asia/Jakarta';
// WIB is UTC+7 all year; date filters send this so the server reads them as WIB days
export const TZ_OFFSET_MINUTES = 420;
export const CANDLE_MS = 2 * 60 * 60 * 1000;
export const MINUS = '−';
export const DASH = '–';

export function escapeHtml(value) {
  if (value == null) return '';
  return String(value).replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

const dateTimeFmt = new Intl.DateTimeFormat('en-CA', {
  timeZone: TZ,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

export const clockFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: TZ,
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
});

export const hourFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: TZ,
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

// Older rows store UTC times without a zone ("2026-10-13 12:00:00"); read them as UTC. Numbers are epoch ms
export function parseTime(value) {
  if (value == null || value === '') return null;
  if (value instanceof Date) return value;
  if (typeof value === 'number') return new Date(value);
  let str = String(value);
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(str)) str = `${str.replace(' ', 'T')}Z`;
  const dt = new Date(str);
  return isNaN(dt.getTime()) ? null : dt;
}

export function formatWIB(value) {
  const dt = parseTime(value);
  if (!dt) return value ? String(value) : DASH;
  const p = Object.fromEntries(dateTimeFmt.formatToParts(dt).map(x => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
}

export function formatTimeOnlyWIB(value) {
  const dt = parseTime(value);
  return dt ? hourFmt.format(dt) : '--:--';
}

export function formatPrice(val) {
  if (val == null || val === '' || isNaN(val)) return DASH;
  const num = Number(val);
  if (num >= 1000) return num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (num >= 1) return num.toFixed(4);
  return num.toFixed(6);
}

// Fixed decimals with a typographic minus, as every other signed figure on the page
export function fixedText(val, digits) {
  return Number(val).toFixed(digits).replace('-', '−');
}

export function signedNum(val, digits = 2) {
  if (val == null || isNaN(val)) return DASH;
  const num = Number(val);
  const rounded = Number(num.toFixed(digits));
  return `${rounded > 0 ? '+' : rounded < 0 ? '−' : ''}${Math.abs(num).toFixed(digits)}`;
}

export function formatPct(val, digits = 2) {
  if (val == null || isNaN(val)) return DASH;
  return `${signedNum(val, digits)}%`;
}

export function formatR(val, digits = 2) {
  if (val == null || isNaN(val)) return DASH;
  return `${signedNum(val, digits)}R`;
}

export function signClass(val) {
  if (val == null || isNaN(val)) return '';
  return val > 0 ? 'is-up' : val < 0 ? 'is-down' : '';
}

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

// A rate always shows its sample: "44.4% (4/9)"
export function rateText(wins, n, digits = 1) {
  if (!n) return `${DASH} (0/0)`;
  return `${((wins / n) * 100).toFixed(digits)}% (${wins}/${n})`;
}

export function percentText(val, digits = 0) {
  return val == null ? DASH : `${Number(val).toFixed(digits)}%`;
}

// "2.2.0" reads as v2.2.0; a label such as "legacy" is shown as it is
export function versionLabel(version) {
  if (!version) return DASH;
  return /^\d/.test(version) ? `v${version}` : version;
}

// "macd_cross@2.2.0"
export function refOf(s) {
  return `${s.strategy_key || 'macd_cross'}@${s.strategy_version || '?'}`;
}

export function durationText(ms) {
  if (ms == null || ms < 0 || isNaN(ms)) return DASH;
  const hours = Math.floor(ms / 3600000);
  const days = Math.floor(hours / 24);
  const rest = hours % 24;
  if (days) return rest ? `${days}d ${rest}h` : `${days}d`;
  if (hours) return `${hours}h`;
  if (ms < 60000) return `${Math.round(ms / 1000)}s`;
  return `${Math.round(ms / 60000)}m`;
}

export function shortHash(hash, n = 12) {
  if (!hash) return DASH;
  return String(hash).slice(0, n);
}

export function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}
