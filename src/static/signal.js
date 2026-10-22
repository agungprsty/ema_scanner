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

    $('detail-entry').innerHTML = formatPrice(entry)
      + `<span class="hint">Signal candle ${isLong ? 'low' : 'high'}</span>`;
    $('detail-sl').innerHTML = formatPrice(sig.stop_loss) + pctFrom(entry, sig.stop_loss);
    $('detail-tp').innerHTML = formatPrice(sig.take_profit) + pctFrom(entry, sig.take_profit);
    $('detail-outcome').innerHTML = outcomeHtml(sig.status);

    $('detail-rsi-label').textContent = `RSI(${config.rsi_period})`;
    $('legend-rsi').textContent = `RSI (${config.rsi_period}) ${config.oversold}/${config.overbought}`;
    $('detail-rsi').innerHTML = formatRsi(sig.rsi) + `<span class="hint">Crossed ${isLong
      ? `below ${config.oversold}` : `above ${config.overbought}`} from ${formatRsi(sig.prev_rsi)}</span>`;
    // Net of fees, the same reward and risk R is measured against
    const risk = Math.abs(entry - sig.stop_loss) / entry * 100 + config.fee_percent;
    const reward = Math.abs(sig.take_profit - entry) / entry * 100 - config.fee_percent;
    $('detail-rr').textContent = entry > 0 ? `1:${Number((reward / risk).toFixed(2))}` : '–';
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
    renderJournalForm(currentSignal);
    renderChart(currentSignal);
  }

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
      [config.oversold, config.overbought].forEach(price => rsiSeries.createPriceLine({
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
  // RSI levels and the fee come from /api/info; the defaults in config apply until it answers
  loadStrategyConfig().catch(() => {}).finally(() => loadSignal(signalId));
});
