// How a signal is shown everywhere: its status, provenance chips, the signal card and the table row.
// Every value from the API goes through escapeHtml before it reaches markup.
import {
  DASH,
  durationText,
  escapeHtml,
  formatPct,
  formatPrice,
  formatR,
  formatWIB,
  parseTime,
  CANDLE_MS,
  signClass,
} from './format.js';

// Status labels; wins and losses get the same treatment, only the sign of R is coloured
const OUTCOMES = {
  PENDING: { cls: 'open', label: 'Open' },
  TP1_HIT: { cls: 'closed', label: 'Target hit' },
  SL_HIT: { cls: 'closed', label: 'Stopped out' },
  STOPPED: { cls: 'closed', label: 'Stopped out' },
  EXPIRED: { cls: 'closed', label: 'Expired' },
  // Exits of strategies without a fixed target (shadow macd_cross_ema50d_oppexit)
  OPPOSITE_CROSS: { cls: 'closed', label: 'Opposite-cross exit' },
  TIME_EXIT: { cls: 'closed', label: 'Time exit' },
};

export const STATUS_OPTIONS = [
  ['PENDING', 'Open'],
  ['TP1_HIT', 'Target hit'],
  ['SL_HIT', 'Stopped out (target strategies)'],
  ['EXPIRED', 'Expired'],
  ['STOPPED', 'Stopped out (exit-rule strategies)'],
  ['OPPOSITE_CROSS', 'Opposite-cross exit'],
  ['TIME_EXIT', 'Time exit'],
];

export function outcomeOf(status) {
  return OUTCOMES[status || 'PENDING'] || { cls: 'closed', label: String(status) };
}

export function outcomeHtml(status) {
  const o = outcomeOf(status);
  return `<span class="outcome ${o.cls}">${escapeHtml(o.label)}</span>`;
}

export function isOpen(s) {
  return (s.win_status || 'PENDING') === 'PENDING';
}

// Net R of a closed signal under the cost model; rows booked before it fall back to their booked R
export function netR(s) {
  if (s.net_r != null) return s.net_r;
  return s.r_multiple != null ? s.r_multiple : null;
}

// Booked at the interpolated cross price, a fill nobody could get, so it has no achievable result
export const NO_FILL_NOTE = 'Entry was the interpolated cross price, not a price anyone could fill, so there is no achievable net result';

export function hasAchievableEntry(s) {
  return s.entry_basis !== 'cross_interp';
}

export function resultHtml(s) {
  if (isOpen(s)) return '<span class="muted">open</span>';
  const r = netR(s);
  if (r == null) return `<span class="muted">${DASH}</span>`;
  if (!hasAchievableEntry(s)) return `<span class="muted" title="${escapeHtml(NO_FILL_NOTE)}">no fill</span>`;
  return `<span class="${signClass(r)}">${formatR(r)}</span>`;
}

export const SOURCE_LABELS = {
  cron: 'Scheduled scan',
  catchup: 'Scheduled scan, late catch-up',
  manual: 'Manual scan',
  legacy: 'Legacy (pre-v2.2 rules)',
};

export const ENTRY_BASIS_LABELS = {
  next_open: 'next open',
  forming_close: 'forming close',
  cross_interp: 'interpolated cross',
};

// Anything but an on-time scheduled signal entered at the next open is listed but not counted
const SOURCE_CHIPS = {
  legacy: { label: 'LEGACY', cls: 'chip-legacy', why: 'Booked under pre-v2.2 rules: interpolated cross-price entry, or a result recorded without fees' },
  catchup: { label: 'LATE', cls: 'chip-late', why: 'Found after its entry candle had already passed' },
  manual: { label: 'MANUAL', cls: '', why: 'Saved from a manual scan' },
};

export function isExcluded(s) {
  return Boolean(s.source) && s.is_eligible === false;
}

export function chipsHtml(s) {
  const chips = [];
  const chip = SOURCE_CHIPS[s.source];
  if (chip) {
    chips.push(`<span class="chip ${chip.cls}" title="${escapeHtml(`${chip.why}. Excluded from stats.`)}">${escapeHtml(chip.label)}</span>`);
  } else if (isExcluded(s)) {
    chips.push('<span class="chip" title="Not entered at the next open. Excluded from stats.">EXCLUDED</span>');
  }
  if (s.strategy_status === 'shadow') {
    chips.push('<span class="chip chip-shadow" title="Shadow strategy: stored and evaluated, never alerted, with its own track record">SHADOW</span>');
  }
  return chips.join('');
}

export function sideHtml(side, withArrow = true) {
  const isLong = side === 'LONG';
  const arrow = withArrow ? (isLong ? '▲ ' : '▼ ') : '';
  return `<span class="${isLong ? 'is-long' : 'is-short'}">${arrow}${isLong ? 'LONG' : 'SHORT'}</span>`;
}

export function signalUrl(s) {
  return `/s/${encodeURIComponent(s.id)}`;
}

export function refHtml(s) {
  const key = s.strategy_key || 'macd_cross';
  const basis = ENTRY_BASIS_LABELS[s.entry_basis] || s.entry_basis || '';
  return `<span class="ref num">${escapeHtml(`${key}@${s.strategy_version || '?'}`)}${basis ? ` · ${escapeHtml(basis)}` : ''}</span>`;
}

function pctFrom(entry, level) {
  if (!entry || !level) return '';
  return formatPct(((level - entry) / entry) * 100);
}

// Planned reward-to-risk of the target, or null for a strategy without one
function targetR(s) {
  const entry = s.entry_price;
  if (!s.tp1 || !entry || !s.stop_loss || entry === s.stop_loss) return null;
  return Math.abs(s.tp1 - entry) / Math.abs(entry - s.stop_loss);
}

// Bar from the stop (−1R) to the target; a closed trade's marker sits at its net R
export function rBarHtml(s) {
  const target = targetR(s);
  const r = isOpen(s) || !hasAchievableEntry(s) ? null : netR(s);
  const hi = Math.max(target ?? 2, r ?? 0) + 0.25;
  const lo = Math.min(-1.25, (r ?? 0) - 0.1);
  const pos = (v) => `${(((v - lo) / (hi - lo)) * 100).toFixed(1)}%`;
  const ticks = [
    `<span class="rbar-tick rbar-stop" style="left:${pos(-1)}" title="Stop, −1R"></span>`,
    `<span class="rbar-tick rbar-zero" style="left:${pos(0)}" title="Entry, 0R"></span>`,
  ];
  if (target != null) ticks.push(`<span class="rbar-tick rbar-target" style="left:${pos(target)}" title="Target, +${target.toFixed(2)}R"></span>`);
  const marker = r == null ? '' : `<span class="rbar-mark ${r >= 0 ? 'up' : 'down'}" style="left:${pos(r)}" title="Exit, ${escapeHtml(formatR(r))} net"></span>`;
  const label = r == null
    ? (isOpen(s) ? 'Open: no result until it closes' : 'No achievable result')
    : `Closed at ${formatR(r)} net, between the stop at −1R and ${target != null ? `the target at +${target.toFixed(2)}R` : 'the exit rules'}`;
  return `<div class="rbar" role="img" aria-label="${escapeHtml(label)}">${ticks.join('')}${marker}</div>`;
}

export function expiresText(s, now = Date.now()) {
  const at = parseTime(s.expires_at);
  if (!at) return '';
  const left = at.getTime() - now;
  const word = s.tp1 ? 'expires' : 'time exit';
  return left > 0 ? `${word} in ${durationText(left)}` : `${word} due, awaiting evaluation`;
}

// Exits stamped at the open of the candle that touched a level: the level was hit somewhere inside it
const LEVEL_HITS = ['TP1_HIT', 'SL_HIT', 'STOPPED'];

// How long a closed trade was held: exact for exits at a candle open or close, an upper bound ("≤")
// for a target or stop touched inside a candle
export function heldText(s) {
  const exit = parseTime(s.exit_time);
  if (!exit || s.cross_candle_ms == null) return '';
  const held = exit.getTime() - (Number(s.cross_candle_ms) + CANDLE_MS);
  if (LEVEL_HITS.includes(s.win_status)) {
    return held <= 0 ? 'within the entry candle' : `≤ ${durationText(held + CANDLE_MS)}`;
  }
  return durationText(held);
}

export function timeLine(s) {
  const parts = [`Detected ${formatWIB(s.detected_at || s.cross_time)} WIB`];
  if (isOpen(s)) {
    const exp = expiresText(s);
    if (exp) parts.push(exp);
  } else {
    const held = heldText(s);
    if (held) parts.push(`held ${held}`);
    if (s.exit_time) parts.push(`exit ${formatWIB(s.exit_time)}`);
  }
  return parts.join(' · ');
}

export function costLine(s) {
  if (isOpen(s) || !hasAchievableEntry(s)) return '';
  const parts = [];
  if (s.gross_r != null) parts.push(`gross ${formatR(s.gross_r)}`);
  if (s.cost_r != null) parts.push(`cost ${formatR(-s.cost_r)}`);
  return parts.join(' · ');
}

// The signal card (benchmark §5.2): one per signal, linking to its page
export function signalCardHtml(s) {
  const o = outcomeOf(s.win_status);
  const r = netR(s);
  const entry = s.entry_price || s.cross_price;
  const rText = isOpen(s) ? '' : (hasAchievableEntry(s) && r != null
    ? `<span class="sc-r num ${signClass(r)}">${formatR(r)} net</span>`
    : '<span class="sc-r muted">no achievable fill</span>');
  const excluded = isExcluded(s);
  return `
    <a class="sig-card${excluded ? ' is-excluded' : ''}" href="${signalUrl(s)}">
      <div class="sc-top">
        <span class="sym">${escapeHtml(s.symbol)}</span>
        ${sideHtml(s.side)}
        <span class="tf">2H</span>
        ${refHtml(s)}
        ${chipsHtml(s)}
      </div>
      <div class="sc-status">
        <span class="status-label ${o.cls}"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(o.label.toUpperCase())}</span>
        ${rText}
      </div>
      <dl class="sc-levels">
        <div><dt>Entry</dt><dd class="num">${formatPrice(entry)}</dd></div>
        <div><dt>Stop</dt><dd class="num">${formatPrice(s.stop_loss)} <small>${escapeHtml(pctFrom(entry, s.stop_loss))}</small></dd></div>
        <div><dt>Target</dt><dd class="num">${s.tp1 ? `${formatPrice(s.tp1)} <small>${escapeHtml(pctFrom(entry, s.tp1))}</small>` : '<span class="muted">exit rules</span>'}</dd></div>
      </dl>
      ${rBarHtml(s)}
      <div class="sc-time num">${escapeHtml(timeLine(s))}</div>
      <div class="sc-foot">
        <span class="num muted">${escapeHtml(costLine(s))}${excluded ? `${costLine(s) ? ' · ' : ''}excluded from stats` : ''}</span>
        <span class="sc-more">Detail →</span>
      </div>
    </a>`;
}

// One row of the signals table (wide screens)
export function signalRowHtml(s) {
  const excluded = isExcluded(s);
  return `
    <tr data-href="${signalUrl(s)}"${excluded ? ' class="is-excluded"' : ''}>
      <td><a class="sym" href="${signalUrl(s)}">${escapeHtml(s.symbol)}</a>${chipsHtml(s)}</td>
      <td>${sideHtml(s.side, false)}</td>
      <td class="num muted">${escapeHtml(`${s.strategy_key || 'macd_cross'}@${s.strategy_version || '?'}`)}</td>
      <td>${outcomeHtml(s.win_status)}</td>
      <td class="r num">${resultHtml(s)}${excluded ? '<div class="excl-note">excluded from stats</div>' : ''}</td>
      <td class="r num">${formatPrice(s.entry_price || s.cross_price)}</td>
      <td class="num muted">${escapeHtml(formatWIB(s.cross_time))}</td>
    </tr>`;
}

// Rows with a data-href open their signal page on click or Enter, without hijacking links inside them
export function bindRowLinks(tbody) {
  tbody.addEventListener('click', (e) => {
    if (e.target.closest('a')) return;
    const row = e.target.closest('tr[data-href]');
    if (row) window.location.assign(row.dataset.href);
  });
}
