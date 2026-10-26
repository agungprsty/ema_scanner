// Signal detail page (/signals/{id}): levels, chart with EMA, RSI and volume delta, and the admin journal form.
// Needs common.js loaded first.
document.addEventListener('DOMContentLoaded', () => {
  const CHART_LIB_URL = 'https://unpkg.com/lightweight-charts@5.0.8/dist/lightweight-charts.standalone.production.js';
  const signalId = Number(window.location.pathname.split('/').pop());

  let chart = null;
  let chartRequest = 0;
  let currentSignal = null;

  const chartViewport = $('chart-viewport');
  const journalForm = $('journal-form');
  const journalEntry = $('journal-entry');
  const journalExit = $('journal-exit');
  const journalNotes = $('journal-notes');
  const journalResult = $('journal-result');
  const journalSave = $('journal-save');
  const levelsForm = $('levels-form');
  const levelsEntry = $('levels-entry');
  const levelsSl = $('levels-sl');
  const levelsTp = $('levels-tp');
  const levelsResult = $('levels-result');
  const levelsSave = $('levels-save');

  // Signal detail
  function pctFrom(entry, level) {
    if (!entry || !level) return '';
    return `<small>${formatPct(((level - entry) / entry) * 100)}</small>`;
  }

  function detailSubtitle(sig) {
    return `Signal candle ${formatWIB(sig.signal_time)} WIB on the ${sig.timeframe} chart`;
  }

  function fillHtml(sig) {
    if (sig.fill_price != null) {
      return `${formatPrice(sig.fill_price)}<span class="hint">Filled ${escapeHtml(formatWIB(sig.fill_time))} WIB</span>`;
    }
    if (sig.status === 'NOT_FILLED') {
      return `<span class="muted">Not filled</span><span class="hint">Window ended ${escapeHtml(formatWIB(sig.exit_time))} WIB</span>`;
    }
    return `<span class="muted">Waiting</span><span class="hint">Fill by ${escapeHtml(formatWIB(sig.fill_deadline))} WIB, else cancelled</span>`;
  }

  function exitHtml(sig) {
    if (sig.exit_price != null) {
      return `${formatPrice(sig.exit_price)}<span class="hint">${escapeHtml(formatWIB(sig.exit_time))} WIB, `
        + `${formatR(sig.r_multiple)} (${formatPct(sig.pnl_percent)} after fees)</span>`;
    }
    if (sig.status === 'OPEN' && sig.hold_deadline) {
      return `<span class="muted">Open</span><span class="hint">Closes by ${escapeHtml(formatWIB(sig.hold_deadline))} WIB at the latest</span>`;
    }
    return '–';
  }

  function fillDetail(sig) {
    const entry = sig.entry_price;
    const isLong = sig.side === 'LONG';
    $('signal-title').innerHTML = `${escapeHtml(sig.symbol)} ${sideHtml(sig.side)}`;
    $('signal-time').textContent = detailSubtitle(sig);
    document.title = `${sig.symbol} ${sig.side === 'LONG' ? 'Long' : 'Short'} · simple-trade`;

    $('detail-entry').innerHTML = formatPrice(entry) + `<span class="hint">${sig.levels_edited_at
      ? `Edited ${escapeHtml(formatWIB(sig.levels_edited_at))} WIB` : `Signal candle ${isLong ? 'low' : 'high'}`}</span>`;
    // From the fill once filled, the price the trade's P/L is measured from
    const base = sig.fill_price ?? entry;
    $('detail-sl').innerHTML = formatPrice(sig.stop_loss) + pctFrom(base, sig.stop_loss);
    $('detail-tp').innerHTML = formatPrice(sig.take_profit) + pctFrom(base, sig.take_profit);
    $('detail-outcome').innerHTML = outcomeHtml(sig.status);

    $('detail-rsi-label').textContent = `RSI(${config.rsi_period})`;
    $('legend-ema').textContent = `EMA ${config.ema_period}`;
    $('legend-rsi').textContent = `RSI (${config.rsi_period}) ${config.rsi_short_min}/${config.rsi_midline}/${config.rsi_long_max}`;
    // The band this side's entry filter required, then the RSI one candle before
    const band = isLong ? `${config.rsi_midline}–${config.rsi_long_max}` : `${config.rsi_short_min}–${config.rsi_midline}`;
    $('detail-rsi').innerHTML = formatRsi(sig.rsi)
      + `<span class="hint">Band ${band}, previous ${formatRsi(sig.prev_rsi)}</span>`;
    // Net of fees, the same reward and risk R is measured against
    // The target in R against the trade's 1R, which stays the risk at the fill after an admin moves the stop
    const atTarget = tradeReturn(sig, base, sig.take_profit);
    $('detail-rr').textContent = atTarget && atTarget.r != null ? `1:${Number(atTarget.r.toFixed(2))}` : '–';
    $('detail-fill').innerHTML = fillHtml(sig);
    $('detail-exit').innerHTML = exitHtml(sig);
  }

  async function loadSignal(id) {
    loadChartLibrary().catch(() => {});
    showChartMessage('<span class="spinner on"></span> Loading signal…');
    try {
      currentSignal = (await getJson(`/api/signals/${encodeURIComponent(id)}`)).data;
    } catch (e) {
      $('signal-title').textContent = 'Signal unavailable';
      journalForm.hidden = true;
      $('chart-legend').hidden = true;
      showChartMessage(escapeHtml(`Could not load this signal: ${e.message}`), true);
      return;
    }
    fillDetail(currentSignal);
    renderLevelsForm(currentSignal);
    renderJournalForm(currentSignal);
    renderChart(currentSignal);
  }

  // Levels form (admin, signals awaiting fill)
  let targetByHand = false;

  function levelsSide() {
    const checked = levelsForm.querySelector('input[name="levels-side"]:checked');
    return checked ? checked.value : 'LONG';
  }

  // A suggested price: as many decimals as the typed entry, and at least 5 significant digits, so
  // "1.2" still gets a target above it; written out in full, never as 1.2e-7
  function likeEntry(value) {
    const typed = levelsEntry.value.trim().replace(',', '.');
    const typedDecimals = /e/i.test(typed) ? 0 : (typed.split('.')[1] || '').length;
    const decimals = Math.min(12, Math.max(typedDecimals, 4 - Math.floor(Math.log10(Math.abs(value)))));
    const text = value.toFixed(Math.max(0, decimals));
    return text.includes('.') ? text.replace(/\.?0+$/, '') : text;
  }

  // The scanner's 1:2 after fees: both exits pay the round-trip fee, so the net reward is twice the net risk
  function defaultTarget(side, entry, stop) {
    const fee = config.fee_percent;
    const distance = (2 * (Math.abs(entry - stop) / entry * 100 + fee) + fee) / 100;
    return side === 'LONG' ? entry * (1 + distance) : entry * (1 - distance);
  }

  const isFilled = (sig) => sig && sig.status === 'OPEN';

  // The level problem the server would reject, or '' when the levels fit the side. Once filled the
  // stop may sit past the entry (break-even, trailing), it only has to stay on the far side of the target.
  function levelsProblem(side, entry, stop, target, filled) {
    if (![entry, stop, target].every(v => v > 0)) return 'Prices must be positive numbers';
    const long = side === 'LONG';
    if (filled) {
      return (long ? stop < target : stop > target) ? '' : `For a ${long ? 'long' : 'short'} the stop must be ${long ? 'below' : 'above'} the target`;
    }
    if (long ? !(stop < entry && entry < target) : !(target < entry && entry < stop)) {
      return `For a ${long ? 'long' : 'short'} the stop must be ${long ? 'below' : 'above'} the entry and the target ${long ? 'above' : 'below'} it`;
    }
    return '';
  }

  function updateLevelsPreview() {
    const side = levelsSide();
    const entry = parseFill(levelsEntry);
    const stop = parseFill(levelsSl);
    const target = parseFill(levelsTp);
    const filled = isFilled(currentSignal);
    const problem = levelsProblem(side, entry, stop, target, filled);
    if (problem) {
      levelsResult.innerHTML = `<span class="is-short">${escapeHtml(problem)}</span>`;
      return;
    }
    if (filled) {
      // Against the actual fill and the trade's original 1R
      const fill = currentSignal.fill_price;
      const at = (level) => {
        const res = tradeReturn(currentSignal, fill, level);
        return `${formatPct(res.pnl)} (${formatR(res.r)})`;
      };
      levelsResult.textContent = `From the fill at ${formatPrice(fill)}, after fees: target ${at(target)}, stop ${at(stop)}`;
      return;
    }
    const fee = config.fee_percent;
    const risk = Math.abs(entry - stop) / entry * 100 + fee;
    const reward = Math.abs(target - entry) / entry * 100 - fee;
    levelsResult.textContent = `After the ${fee}% fee: target +${reward.toFixed(2)}%, stop −${risk.toFixed(2)}%, R:R 1:${Number((reward / risk).toFixed(2))}`;
  }

  function followTarget() {
    const entry = parseFill(levelsEntry);
    const stop = parseFill(levelsSl);
    if (!targetByHand && entry > 0 && stop > 0) levelsTp.value = likeEntry(defaultTarget(levelsSide(), entry, stop));
    updateLevelsPreview();
  }

  function renderLevelsForm(sig) {
    const filled = isFilled(sig);
    levelsForm.hidden = !(sig.status === 'PENDING' || filled);
    $('levels-help').textContent = filled
      ? 'The order filled, so only the stop and target can move: to break-even or trailing, for example. '
        + 'New levels apply from the next candle, earlier candles keep the old ones, and R stays against the risk at the fill. Telegram gets the new levels.'
      : 'While the order awaits a fill. The target follows 1:2 after fees until you type your own. '
        + 'The order can only fill on candles that open after you save, and Telegram gets the new levels.';
    levelsForm.querySelectorAll('input[name="levels-side"]').forEach(r => {
      r.checked = r.value === sig.side;
      r.disabled = filled;
    });
    levelsEntry.value = sig.entry_price;
    levelsEntry.readOnly = filled;
    levelsSl.value = sig.stop_loss;
    levelsTp.value = sig.take_profit;
    // A moved stop no longer measures the risk, so the target is not derived from it once filled
    targetByHand = filled;
    updateLevelsPreview();
  }

  levelsEntry.addEventListener('input', followTarget);
  levelsSl.addEventListener('input', followTarget);
  levelsTp.addEventListener('input', () => {
    targetByHand = levelsTp.value.trim() !== '';
    updateLevelsPreview();
  });
  // A new side mirrors the stop across the entry, so the risk stays the same size
  levelsForm.querySelectorAll('input[name="levels-side"]').forEach(r => r.addEventListener('change', () => {
    const entry = parseFill(levelsEntry);
    const stop = parseFill(levelsSl);
    if (entry > 0 && stop > 0) levelsSl.value = likeEntry(2 * entry - stop);
    targetByHand = false;
    followTarget();
  }));

  levelsForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const sig = currentSignal;
    if (!sig) return;
    const values = {
      side: levelsSide(),
      entry_price: parseFill(levelsEntry),
      take_profit: parseFill(levelsTp),
      stop_loss: parseFill(levelsSl),
    };
    const filled = isFilled(sig);
    if (levelsProblem(values.side, values.entry_price, values.stop_loss, values.take_profit, filled)) return;
    const labels = { side: 'Side', entry_price: 'Entry', take_profit: 'Target', stop_loss: 'Stop' };
    const changes = Object.keys(values).filter(k => values[k] !== sig[k]).map(k => `${labels[k]}: ${sig[k]} → ${values[k]}`);
    if (!changes.length) {
      showToast('Nothing changed');
      return;
    }
    const when = filled
      ? 'They apply from the next candle; earlier candles keep the old levels.'
      : 'The order can only fill on candles that open after now.';
    if (!confirm(`Save the new levels?\n\n${changes.join('\n')}\n\n${when} Telegram gets the new levels.`)) return;

    levelsSave.disabled = true;
    try {
      const res = await fetch(`/api/signals/${encodeURIComponent(sig.id)}/levels`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(values),
      });
      if (!res.ok) throw new Error(await responseError(res));
      currentSignal = (await res.json()).data;
      fillDetail(currentSignal);
      renderLevelsForm(currentSignal);
      renderChart(currentSignal);
      showToast('Levels saved');
    } catch (err) {
      levelsResult.innerHTML = `<span class="is-short">${escapeHtml(`Could not save: ${err.message}`)}</span>`;
    } finally {
      levelsSave.disabled = false;
    }
  });

  // Journal form
  function selectedJournalStatus() {
    const checked = journalForm.querySelector('input[name="journal-status"]:checked');
    return checked ? checked.value : '';
  }

  function parseFill(input) {
    const raw = input.value.trim().replace(',', '.');
    if (!raw) return null;
    const num = Number(raw);
    return Number.isFinite(num) && num > 0 ? num : NaN;
  }

  function updateJournalPreview() {
    const status = selectedJournalStatus();
    journalForm.querySelectorAll('[data-taken]').forEach(el => { el.hidden = status !== 'TAKEN'; });
    if (status !== 'TAKEN' || !currentSignal) {
      journalResult.textContent = '';
      return;
    }
    const entry = parseFill(journalEntry);
    const exit = parseFill(journalExit);
    if (Number.isNaN(entry) || Number.isNaN(exit)) {
      journalResult.innerHTML = '<span class="is-short">Prices must be positive numbers</span>';
      return;
    }
    const res = entry && exit ? tradeReturn(currentSignal, entry, exit) : null;
    if (res) {
      journalResult.innerHTML = `Your result: <span class="${signClass(res.pnl)}">${formatR(res.r)}</span> (${formatPct(res.pnl)} after fees)`;
    } else {
      journalResult.textContent = entry ? 'Add the exit fill once the trade is closed.' : '';
    }
  }

  function renderJournalForm(sig) {
    const j = sig.journal || null;
    const status = j ? j.status : '';
    journalForm.querySelectorAll('input[name="journal-status"]').forEach(r => { r.checked = r.value === status; });
    journalEntry.value = j && j.entry_fill != null ? j.entry_fill : '';
    journalExit.value = j && j.exit_fill != null ? j.exit_fill : '';
    journalNotes.value = j ? j.notes || '' : '';
    journalEntry.placeholder = `Signal ${sig.fill_price != null ? 'fill' : 'limit'} ${formatPrice(sig.fill_price ?? sig.entry_price)}`;
    updateJournalPreview();
  }

  journalForm.addEventListener('input', updateJournalPreview);
  journalForm.addEventListener('change', updateJournalPreview);

  async function saveJournal(sig, status, entry, exit) {
    const url = `/api/signals/${encodeURIComponent(sig.id)}/journal`;
    if (!status) return fetch(url, { method: 'DELETE' });
    return fetch(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        status,
        entry_fill: status === 'TAKEN' ? entry : null,
        exit_fill: status === 'TAKEN' ? exit : null,
        notes: journalNotes.value.trim(),
      }),
    });
  }

  journalForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const sig = currentSignal;
    if (!sig) return;
    const status = selectedJournalStatus();
    const entry = parseFill(journalEntry);
    const exit = parseFill(journalExit);
    if (status === 'TAKEN' && (Number.isNaN(entry) || Number.isNaN(exit))) return;
    if (status === 'TAKEN' && exit && !entry) {
      journalResult.innerHTML = '<span class="is-short">Add the entry fill first</span>';
      return;
    }
    if (!status && !sig.journal) {
      showToast('Nothing to save');
      return;
    }

    journalSave.disabled = true;
    try {
      const res = await saveJournal(sig, status, entry, exit);
      if (!res.ok) throw new Error(await responseError(res));
      const saved = status ? (await res.json()).data : null;
      sig.journal = saved ? saved.journal : null;
      renderJournalForm(sig);
      showToast(status ? 'Saved to your journal' : 'Removed from your journal');
    } catch (err) {
      journalResult.innerHTML = `<span class="is-short">${escapeHtml(`Could not save: ${err.message}`)}</span>`;
    } finally {
      journalSave.disabled = false;
    }
  });

  // Chart
  let chartLibPromise = null;

  // Loaded from unpkg at runtime: CSP only allows /static/ script tags in the page itself
  function loadChartLibrary() {
    if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
    if (!chartLibPromise) {
      chartLibPromise = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = CHART_LIB_URL;
        script.async = true;
        script.onload = () => (window.LightweightCharts
          ? resolve(window.LightweightCharts)
          : reject(new Error('chart library did not initialise')));
        script.onerror = () => {
          script.remove();
          reject(new Error('chart library failed to load'));
        };
        document.head.appendChild(script);
      }).catch((err) => {
        chartLibPromise = null; // allow a retry on reload
        throw err;
      });
    }
    return chartLibPromise;
  }

  function destroyChart() {
    chartRequest += 1;
    if (chart) {
      chart.remove();
      chart = null;
    }
  }

  function showChartMessage(message, isError = false) {
    chartViewport.innerHTML = `<div class="chart-msg${isError ? ' err' : ''}">${message}</div>`;
  }

  async function renderChart(sig) {
    destroyChart();
    const request = chartRequest;
    showChartMessage('<span class="spinner on"></span> Loading candles…');

    try {
      // ?v=2: closed charts are browser-cached for a week, and older copies lack ema and volume_delta
      const res = await fetch(`/api/signals/${encodeURIComponent(sig.id)}/chart?v=2`);
      if (request !== chartRequest) return;
      if (!res.ok) {
        showChartMessage(escapeHtml(`Chart unavailable: ${await responseError(res)}`), true);
        return;
      }
      const payload = (await res.json()).data || {};
      if (request !== chartRequest) return;
      if (!Array.isArray(payload.candles) || payload.candles.length === 0) {
        showChartMessage('No candle data for this symbol.');
        return;
      }
      try {
        await loadChartLibrary();
      } catch {
        if (request === chartRequest) showChartMessage('The chart library could not be loaded. Check your connection and reopen the signal.', true);
        return;
      }
      if (request !== chartRequest) return;
      chartViewport.innerHTML = '';

      const css = getComputedStyle(document.documentElement);
      const color = (name) => css.getPropertyValue(name).trim();
      const longColor = color('--long');
      const shortColor = color('--short');
      const entryColor = color('--entry');

      chart = LightweightCharts.createChart(chartViewport, {
        autoSize: true,
        layout: {
          background: { color: color('--surface') },
          textColor: color('--text-3'),
          fontFamily: color('--font-mono'),
          fontSize: 11,
          // The logo would cover the first volume bars; the legend credits TradingView instead
          attributionLogo: false,
          panes: { separatorColor: color('--line-strong'), enableResize: false },
        },
        grid: {
          vertLines: { color: color('--line') },
          horzLines: { color: color('--line') },
        },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        rightPriceScale: { borderColor: color('--line') },
        timeScale: {
          borderColor: color('--line'),
          timeVisible: true,
          secondsVisible: false,
        },
        localization: { dateFormat: 'yyyy-MM-dd' },
      });

      // Entry, TP and SL lines do not count toward autoscale, so widen the price range to keep them in view
      const levels = sig.status === 'NOT_FILLED' ? [] : [sig.entry_price, sig.take_profit, sig.stop_loss].filter(Boolean);
      const candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
        upColor: longColor,
        downColor: shortColor,
        borderVisible: false,
        wickUpColor: longColor,
        wickDownColor: shortColor,
        autoscaleInfoProvider: (original) => {
          const range = original();
          if (!range || !levels.length) return range;
          return {
            ...range,
            priceRange: {
              minValue: Math.min(range.priceRange.minValue, ...levels),
              maxValue: Math.max(range.priceRange.maxValue, ...levels),
            },
          };
        },
      });

      // Three panes on one time axis, each with its own scale: price with EMA, RSI, then volume delta
      const emaSeries = chart.addSeries(LightweightCharts.LineSeries, {
        color: color('--warn'),
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      const rsiSeries = chart.addSeries(LightweightCharts.LineSeries, {
        color: color('--text-2'),
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
      }, 1);
      const deltaSeries = chart.addSeries(LightweightCharts.HistogramSeries, {
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: { type: 'volume' },
      }, 2);
      const [pricePane, rsiPane, deltaPane] = chart.panes();
      pricePane.setStretchFactor(3);
      rsiPane.setStretchFactor(1);
      deltaPane.setStretchFactor(1);

      // Lightweight Charts renders timestamps as UTC; shift them so the axis reads in WIB
      const UTC7_OFFSET_SEC = 7 * 3600;
      const candles = payload.candles.map(c => ({ ...c, time: c.time + UTC7_OFFSET_SEC }));
      candleSeries.setData(candles);
      const shift = (points) => (points || []).map(p => ({ time: p.time + UTC7_OFFSET_SEC, value: p.value }));
      emaSeries.setData(shift(payload.ema));
      rsiSeries.setData(shift(payload.rsi));
      // Green when taker buys outweigh taker sells in the candle, red otherwise
      deltaSeries.setData(shift(payload.volume_delta).map(p => ({ ...p, color: p.value >= 0 ? longColor : shortColor })));
      [config.rsi_short_min, config.rsi_midline, config.rsi_long_max].forEach(price => rsiSeries.createPriceLine({
        price,
        color: color('--text-3'),
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true,
        title: '',
      }));

      // Candle containing a moment; an expiry is stamped at the close, so it belongs to the candle before
      const intervalSec = intervalMs(sig.timeframe) / 1000;
      const candleAt = (ms, closeStamp = false) => {
        if (ms == null || !intervalSec) return null;
        const ts = Math.floor(ms / 1000) + UTC7_OFFSET_SEC - (closeStamp ? 1 : 0);
        return candles.find(c => c.time <= ts && ts < c.time + intervalSec) || null;
      };

      const markers = [];
      const isLong = sig.side === 'LONG';
      const addMarker = (ms, closeStamp, marker) => {
        const c = candleAt(ms, closeStamp);
        if (c) markers.push({ time: c.time, ...marker });
      };
      addMarker(sig.signal_candle_ms, false, {
        position: isLong ? 'belowBar' : 'aboveBar',
        color: isLong ? longColor : shortColor,
        shape: isLong ? 'arrowUp' : 'arrowDown',
        text: 'Signal',
      });
      if (sig.fill_ms != null) {
        addMarker(sig.fill_ms, false, {
          position: isLong ? 'belowBar' : 'aboveBar',
          color: entryColor,
          shape: 'circle',
          text: 'Fill',
        });
      }
      if (sig.exit_price != null) {
        addMarker(sig.closed_ms, sig.status === 'EXPIRED', {
          position: sig.exit_price >= sig.fill_price ? 'aboveBar' : 'belowBar',
          color: (sig.r_multiple ?? sig.pnl_percent) >= 0 ? longColor : shortColor,
          shape: 'circle',
          text: 'Exit',
        });
      }
      if (markers.length) LightweightCharts.createSeriesMarkers(candleSeries, markers.sort((a, b) => a.time - b.time));

      // A limit that never filled had no position, so it gets no levels; a resting limit is dashed
      if (sig.status !== 'NOT_FILLED') {
        const lineStyle = sig.status === 'PENDING' ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid;
        [
          [sig.entry_price, entryColor, 'Entry'],
          [sig.take_profit, longColor, 'TP'],
          [sig.stop_loss, shortColor, 'SL'],
        ].forEach(([price, lineColor, label]) => {
          if (!price) return;
          candleSeries.createPriceLine({
            price,
            color: lineColor,
            lineWidth: 1,
            lineStyle,
            axisLabelVisible: true,
            title: label,
          });
        });
      }

      chart.timeScale().fitContent();
    } catch (e) {
      if (request !== chartRequest) return;
      console.error('Failed to render chart:', e);
      showChartMessage('Could not load candles from Binance.', true);
    }
  }

  try {
    const from = new URL(document.referrer);
    if (from.origin === window.location.origin && from.pathname === '/dashboard') $('back-link').href = from.pathname + from.search;
  } catch {
    // no referrer: keep the plain /dashboard link
  }

  initAuth();
  bindLogout();
  // Indicator settings and the fee come from /api/info; the defaults in config apply until it answers
  loadStrategyConfig().catch(() => {}).finally(() => loadSignal(signalId));
});
