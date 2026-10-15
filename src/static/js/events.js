// Ledger events as the pages list them: what happened, to which signal, when, and the event's hash.
import { escapeHtml, formatR, formatWIB, shortHash } from './format.js';

const EVENT_LABELS = {
  detected: 'Detected',
  entered: 'Entered',
  resolved: 'Resolved',
  restated: 'Restated',
  alert_sent: 'Alert sent',
  excluded: 'Excluded',
};

export function eventLabel(type) {
  return EVENT_LABELS[type] || String(type);
}

// One line of detail for an event, from its payload (feed rows carry result/net_r/reason/kind directly)
export function eventDetail(ev) {
  const p = ev.payload || ev;
  if (ev.event_type === 'resolved') {
    const parts = [p.result ? String(p.result).replace(/_/g, ' ').toLowerCase() : 'closed'];
    if (p.net_r != null) parts.push(`${formatR(p.net_r)} net`);
    if (p.cost_model_version) parts.push(`cost model ${p.cost_model_version}`);
    return parts.join(' · ');
  }
  if (ev.event_type === 'excluded') return p.reason ? `excluded: ${p.reason}` : 'excluded from stats';
  if (ev.event_type === 'alert_sent') return p.kind ? `${p.kind} alert` : 'alert';
  if (ev.event_type === 'detected' && p.strategy_ref) return p.strategy_ref;
  return '';
}

export function feedItemHtml(ev) {
  const who = ev.symbol
    ? `<a href="/s/${encodeURIComponent(ev.signal_id)}"><span class="sym">${escapeHtml(ev.symbol)}</span> ${escapeHtml(ev.side || '')}</a>`
    : '<span class="muted">chain</span>';
  const detail = eventDetail(ev);
  const ref = ev.strategy_key ? `${ev.strategy_key}@${ev.strategy_version || '?'}` : '';
  return `
    <li>
      <div class="ev-main">
        <span class="ev-type">${escapeHtml(eventLabel(ev.event_type))}</span>
        ${who}
        <span class="ref num">${escapeHtml(ref)}</span>
        ${detail ? `<span>${escapeHtml(detail)}</span>` : ''}
      </div>
      <div class="ev-time num">${escapeHtml(formatWIB(ev.occurred_at))} WIB · #${escapeHtml(ev.id)} · <span class="ev-hash" title="${escapeHtml(ev.hash)}">${escapeHtml(shortHash(ev.hash, 16))}…</span></div>
    </li>`;
}

// Server-side verification of the chain (/api/ledger/verify) in one line
export function verifyHtml(v) {
  if (!v) return '<span class="muted">–</span>';
  const age = Number(v.cached_for_seconds) > 0 ? ` (cached result, ${escapeHtml(Math.round(v.cached_for_seconds))}s old; re-checked whenever the ledger changes)` : '';
  const when = v.verified_at ? ` at ${escapeHtml(formatWIB(v.verified_at))} WIB${age}` : '';
  if (v.ok) {
    return `<span class="is-up">Verified</span><span class="hint">${escapeHtml(v.event_count ?? '')} events, chain, heads, candle windows and rows all check out${when}</span>`;
  }
  const failed = Object.entries(v).filter(([, section]) => section && typeof section === 'object' && section.ok === false)
    .map(([name]) => name);
  const reason = (v.chain && v.chain.reason) || (failed.length ? `failed: ${failed.join(', ')}` : 'a check failed');
  const at = v.first_bad_event != null ? ` at event #${escapeHtml(v.first_bad_event)}` : '';
  return `<span class="is-down">Verification FAILED${at}</span><span class="hint">${escapeHtml(reason)}${when}</span>`;
}
