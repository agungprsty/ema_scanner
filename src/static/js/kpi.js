// The honest KPI strip of one strategy version (/api/performance): net R first, every figure with its
// sample size, intervals where they apply, and totals held back until the sample is large enough.
import { DASH, escapeHtml, formatR, percentText, plural, rateText, signClass, signedNum } from './format.js';

// Every reason a signal is left out; the last two are only listed when they apply
const EXCLUSION_LABELS = [
  ['legacy', 'legacy', true],
  ['catchup', 'late', true],
  ['manual', 'manual', true],
  ['other_version', 'other versions', false],
  ['outside_ledger', 'outside the ledger', false],
  ['other', 'other', false],
];

export function exclusionText(p) {
  const ex = p.excluded || {};
  return EXCLUSION_LABELS
    .filter(([key, , always]) => always || ex[key])
    .map(([key, label]) => `${ex[key] || 0} ${label}`)
    .join(', ');
}

// Summed totals wait for enough trades, so a handful of results does not anchor anyone
export function totalsShown(p) {
  return (p.n_closed || 0) >= (p.min_sample_for_totals || 100);
}

function tile(label, value, valueClass, sub) {
  return `
    <div class="stat">
      <div class="stat-label">${escapeHtml(label)}</div>
      <div class="stat-value num ${valueClass}">${value}</div>
      <div class="stat-sub">${sub}</div>
    </div>`;
}

export function kpiTilesHtml(p) {
  const n = Number(p.n_closed) || 0;
  const warn = p.sample_size_warning
    ? `<span class="stat-warn">${escapeHtml(p.sample_size_warning)} (n &lt; ${Number(p.min_sample) || 30})</span>`
    : '';
  const ci = p.mean_net_r_ci95;
  const meanSub = (ci
    ? `95% CI [${escapeHtml(signedNum(ci[0]))}, ${escapeHtml(signedNum(ci[1]))}] · n=${n}`
    : `Per closed trade · n=${n}`) + warn;

  const noTarget = p.win_rule === 'net_r_positive';
  const wci = p.win_rate_ci95;
  const winCi = wci ? `95% CI [${escapeHtml(Number(wci[0]).toFixed(1))}, ${escapeHtml(Number(wci[1]).toFixed(1))}]% · ` : '';
  const winSub = !n ? 'No closed trades yet' : winCi + (noTarget
    ? 'no fixed target: a win is a net R above zero'
    : `break-even ≈${escapeHtml(percentText(p.break_even_win_rate))}; ${Number(p.expired) || 0} expired count as closed`);

  const showTotal = n > 0 && totalsShown(p);
  const totalSub = (showTotal
    ? 'After fees and slippage'
    : `Total shown from n=${Number(p.min_sample_for_totals) || 100} (now ${n})`)
    + (p.profit_factor != null && showTotal ? ` · profit factor ${Number(p.profit_factor).toFixed(2)}` : '');

  return [
    tile('Mean net R', n ? escapeHtml(formatR(p.mean_net_r)) : DASH, n ? signClass(p.mean_net_r) : '', meanSub),
    tile('Win rate', n ? escapeHtml(rateText(Number(p.wins) || 0, n)) : DASH, '', winSub),
    tile('Net R', showTotal ? escapeHtml(formatR(p.total_net_r)) : DASH, showTotal ? signClass(p.total_net_r) : '', escapeHtml(totalSub)),
    tile('Max drawdown', n ? escapeHtml(formatR(-(p.max_drawdown_r || 0))) : DASH, n && p.max_drawdown_r ? 'is-down' : '',
      'Peak to trough, in exit order'),
  ].join('');
}

export function kpiNote(p) {
  const parts = [
    `cost model ${p.cost_model_version}`,
    `${plural(Number(p.n_closed) || 0, 'closed trade')}, ${Number(p.open_count) || 0} open`,
    `excluded from stats: ${exclusionText(p)}`,
  ];
  return `${parts.join(' · ')}.`;
}

// A strategy's block: its reference, status and holdout verdict above the four tiles
export function kpiBlockHtml(strategy, p) {
  const holdout = strategy.holdout
    ? ` · <a href="${escapeHtml(strategy.holdout.url)}">holdout ${escapeHtml(strategy.holdout.verdict || 'result')}</a>`
    : '';
  return `
    <section class="kpi-block" aria-label="${escapeHtml(strategy.ref)}">
      <header class="kpi-head">
        <span class="ref num">${escapeHtml(strategy.ref)}</span>
        <span class="badge badge-${escapeHtml(strategy.status)}">${escapeHtml(strategy.status)}</span>
        <a class="kpi-link" href="/track-record?strategy=${encodeURIComponent(strategy.ref)}">Track record →</a>
      </header>
      <div class="stats">${kpiTilesHtml(p)}</div>
      <p class="stats-note">${escapeHtml(kpiNote(p))}${holdout}</p>
    </section>`;
}
