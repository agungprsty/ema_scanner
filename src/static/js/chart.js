// Charts: the self-hosted lightweight-charts library, loaded on demand and pinned by hash, a signal's
// candle chart (at publish or after close), and the equity and drawdown curves of the track record.
import { CANDLE_MS, escapeHtml, parseTime } from './format.js';

// Self-hosted copy of lightweight-charts 4.2.1 (identical to the npm tarball); the hash pins those exact bytes
const CHART_LIB_URL = '/static/vendor/lightweight-charts-4.2.1.standalone.production.js';
const CHART_LIB_INTEGRITY = 'sha384-6pZys+XyCH0tACkmBDDBmI3cqbXjHWKKPtaZLx45PUShrKwmpQljmRsBkU58Ln1F';
// Lightweight Charts renders timestamps as UTC; shift them so the axis reads in WIB
const UTC7_OFFSET_SEC = 7 * 3600;

export const CANDLE_SOURCE_NOTE = 'Candles: Binance USDⓈ-M futures 2H klines (fapi/v1/klines). Closed signals are drawn from the window stored when they were settled.';

let chartLibPromise = null;

// The library is only needed where a chart is drawn, so it is fetched the first time one is wanted
export function loadChartLibrary() {
  if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
  if (!chartLibPromise) {
    chartLibPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = CHART_LIB_URL;
      script.integrity = CHART_LIB_INTEGRITY;
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
      chartLibPromise = null; // allow a retry
      throw err;
    });
  }
  return chartLibPromise;
}

export function showChartMessage(viewport, message, isError = false) {
  viewport.innerHTML = `<div class="chart-msg${isError ? ' err' : ''}">${escapeHtml(message)}</div>`;
}

function tokens() {
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  return {
    surface: color('--surface'),
    text3: color('--text-3'),
    line: color('--line'),
    long: color('--long'),
    short: color('--short'),
    entry: color('--entry'),
    longBand: color('--long-band'),
    shortBand: color('--short-band'),
    fontMono: color('--font-mono'),
  };
}

function baseOptions(t, compact) {
  return {
    autoSize: true,
    layout: {
      background: { color: t.surface },
      textColor: t.text3,
      fontFamily: t.fontMono,
      fontSize: compact ? 10 : 11,
      // No logo link inside every chart (it nested links in the chart grid and added a tab stop per
      // chart); the library is credited once in the page footer, as its licence asks
      attributionLogo: false,
    },
    grid: {
      vertLines: { color: compact ? 'transparent' : t.line },
      horzLines: { color: t.line },
    },
    rightPriceScale: { borderColor: t.line, visible: !compact },
    timeScale: { borderColor: t.line, timeVisible: true, secondsVisible: false, visible: !compact, rightOffset: 3 },
    localization: { dateFormat: 'yyyy-MM-dd' },
    handleScroll: !compact,
    handleScale: !compact,
  };
}

// Candles to chart: `asOfMs` keeps only those closed by then (what was known when the signal was published)
export function visibleCandles(candles, asOfMs = null) {
  return candles.filter(c => asOfMs == null || c.time * 1000 + CANDLE_MS <= asOfMs);
}

// Candles drawn around a trade: from `before` candles before the cross to `after` candles past the exit
// candle (open trades run to the newest candle). A stored window can be much longer (strategies that
// read hundreds of candles of history to settle, or the whole hold limit past an early exit); the
// whole window stays in the API and its hash, only the drawn range is trimmed
export const CHART_FRAME = { before: 30, after: 6 };
export const COMPACT_FRAME = { before: 10, after: 5 };

export function framedCandles(candles, sig, { before, after } = CHART_FRAME) {
  if (sig.cross_candle_ms == null) return candles;
  const from = Number(sig.cross_candle_ms) - before * CANDLE_MS;
  const exit = sig.win_status && sig.win_status !== 'PENDING' ? parseTime(sig.exit_time) : null;
  // An exit stamped at a candle close (expiry, time exit) belongs to the candle before
  const closeStamp = ['EXPIRED', 'TIME_EXIT'].includes(sig.win_status);
  const exitOpen = exit ? exit.getTime() - (closeStamp ? 1 : 0) : null;
  const to = exitOpen != null ? exitOpen - (exitOpen % CANDLE_MS) + after * CANDLE_MS : Infinity;
  return candles.filter(c => c.time * 1000 >= from && c.time * 1000 <= to);
}

// Context candles the API sends before a stored window (never part of it), then the window itself
export function chartCandles(payload) {
  const window = Array.isArray(payload.candles) ? payload.candles : [];
  const context = Array.isArray(payload.context) ? payload.context : [];
  const first = window.length ? window[0].time : Infinity;
  return [...context.filter(c => c.time < first), ...window];
}

// Draw a signal on its candles. Returns the chart (call .remove() to dispose of it)
export function renderSignalChart(viewport, sig, rawCandles, { asOfMs = null, compact = false } = {}) {
  const LC = window.LightweightCharts;
  const t = tokens();
  const shown = framedCandles(visibleCandles(rawCandles, asOfMs), sig, compact ? COMPACT_FRAME : CHART_FRAME);
  if (!shown.length) {
    showChartMessage(viewport, 'No closed candles to show for this moment.');
    return null;
  }
  viewport.innerHTML = '';
  const chart = LC.createChart(viewport, {
    ...baseOptions(t, compact),
    crosshair: { mode: compact ? LC.CrosshairMode.Hidden : LC.CrosshairMode.Normal },
  });
  const candles = shown.map(c => ({ time: c.time + UTC7_OFFSET_SEC, open: c.open, high: c.high, low: c.low, close: c.close }));
  const isLong = sig.side === 'LONG';
  const entry = sig.entry_price || sig.cross_price;
  const crossSec = sig.cross_candle_ms != null ? Math.floor(Number(sig.cross_candle_ms) / 1000) + UTC7_OFFSET_SEC : null;
  const exitDate = asOfMs == null && sig.exit_price != null ? parseTime(sig.exit_time) : null;

  // Risk and reward bands over the trade, from the entry candle to the exit (or the last candle shown)
  if (asOfMs == null && entry && crossSec != null) {
    const startSec = crossSec + CANDLE_MS / 1000;
    const endSec = exitDate ? Math.floor(exitDate.getTime() / 1000) + UTC7_OFFSET_SEC : candles[candles.length - 1].time;
    const span = candles.filter(c => c.time >= startSec && c.time <= endSec);
    const band = (price, fill) => {
      if (!price || !span.length) return;
      const series = chart.addBaselineSeries({
        baseValue: { type: 'price', price: entry },
        topLineColor: 'transparent',
        bottomLineColor: 'transparent',
        topFillColor1: fill,
        topFillColor2: fill,
        bottomFillColor1: fill,
        bottomFillColor2: fill,
        lineWidth: 1,
        lastValueVisible: false,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
      });
      series.setData(span.map(c => ({ time: c.time, value: price })));
    };
    band(sig.tp1, t.longBand);
    band(sig.stop_loss, t.shortBand);
  }

  const candleSeries = chart.addCandlestickSeries({
    upColor: t.long,
    downColor: t.short,
    borderVisible: false,
    wickUpColor: t.long,
    wickDownColor: t.short,
    // The last-price line would read as one more, unlabelled level next to entry, stop and target
    priceLineVisible: false,
    lastValueVisible: false,
  });
  candleSeries.setData(candles);

  const candleAt = (sec) => candles.find(c => c.time <= sec && sec < c.time + CANDLE_MS / 1000) || null;
  const markers = [];
  const crossCandle = crossSec != null ? candleAt(crossSec) : null;
  if (crossCandle) {
    markers.push({
      time: crossCandle.time,
      position: isLong ? 'belowBar' : 'aboveBar',
      color: isLong ? t.long : t.short,
      shape: isLong ? 'arrowUp' : 'arrowDown',
      text: compact ? '' : 'Cross',
    });
  }
  if (exitDate) {
    // An exit stamped at a candle close (expiry, time exit) belongs to the candle before
    const closeStamp = ['EXPIRED', 'TIME_EXIT'].includes(sig.win_status);
    const exitCandle = candleAt(Math.floor(exitDate.getTime() / 1000) + UTC7_OFFSET_SEC - (closeStamp ? 1 : 0));
    if (exitCandle) {
      markers.push({
        time: exitCandle.time,
        position: sig.exit_price >= entry ? 'aboveBar' : 'belowBar',
        color: t.text3,
        shape: 'circle',
        text: compact ? '' : 'Exit',
      });
    }
  }
  if (markers.length) candleSeries.setMarkers(markers.sort((a, b) => a.time - b.time));

  [
    [entry, t.entry, LC.LineStyle.Solid, 'Entry'],
    [sig.tp1, t.long, LC.LineStyle.Dashed, 'Target'],
    [sig.stop_loss, t.short, LC.LineStyle.Dashed, 'Stop'],
  ].forEach(([price, lineColor, lineStyle, label]) => {
    if (!price) return;
    candleSeries.createPriceLine({
      price,
      color: lineColor,
      lineWidth: 1,
      lineStyle,
      axisLabelVisible: !compact,
      title: compact ? '' : label,
    });
  });
  chart.timeScale().fitContent();
  return chart;
}

// The curves start at zero one second before the first exit, so the first trade's result is drawn
// as a move from zero like every other one (and max drawdown is measured from zero, as stated)
export function withOrigin(points) {
  if (!points.length) return points;
  return [{ time: points[0].time - 1, equity_r: 0, drawdown_r: 0, n: 0 }, ...points];
}

// Cumulative net R per exit, a baseline at zero. `showTotal` false keeps the running total's label off
// the price axis while summed totals are held back (too few trades)
export function renderEquityChart(viewport, points, { showTotal = true } = {}) {
  const LC = window.LightweightCharts;
  const t = tokens();
  viewport.innerHTML = '';
  const chart = LC.createChart(viewport, { ...baseOptions(t, false), crosshair: { mode: LC.CrosshairMode.Normal } });
  const series = chart.addBaselineSeries({
    lastValueVisible: showTotal,
    priceLineVisible: false,
    baseValue: { type: 'price', price: 0 },
    topLineColor: t.long,
    bottomLineColor: t.short,
    topFillColor1: t.longBand,
    topFillColor2: 'transparent',
    bottomFillColor1: 'transparent',
    bottomFillColor2: t.shortBand,
    lineWidth: 2,
    priceFormat: { type: 'custom', formatter: (v) => `${v.toFixed(2)}R`, minMove: 0.01 },
  });
  series.setData(withOrigin(points).map(p => ({ time: p.time + UTC7_OFFSET_SEC, value: p.equity_r })));
  chart.timeScale().fitContent();
  return chart;
}

// Distance below the running peak of the equity curve, in R (zero or negative)
export function renderDrawdownChart(viewport, points) {
  const LC = window.LightweightCharts;
  const t = tokens();
  viewport.innerHTML = '';
  const chart = LC.createChart(viewport, { ...baseOptions(t, false), crosshair: { mode: LC.CrosshairMode.Normal } });
  const series = chart.addAreaSeries({
    lineColor: t.short,
    topColor: t.shortBand,
    bottomColor: t.shortBand,
    lineWidth: 2,
    invertFilledArea: true,
    priceLineVisible: false,
    priceFormat: { type: 'custom', formatter: (v) => `${v.toFixed(2)}R`, minMove: 0.01 },
    // Drawdown is zero or below: the scale tops out at zero, never at a positive tick
    autoscaleInfoProvider: (original) => {
      const res = original();
      if (!res || !res.priceRange) return res;
      const min = Math.min(res.priceRange.minValue, -0.5);
      return { ...res, priceRange: { minValue: min, maxValue: 0 } };
    },
  });
  series.setData(withOrigin(points).map(p => ({ time: p.time + UTC7_OFFSET_SEC, value: p.drawdown_r })));
  chart.timeScale().fitContent();
  return chart;
}
