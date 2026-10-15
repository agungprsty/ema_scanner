# Backtest harness

Replays any registered strategy version (default `macd_cross@2.2.0`, the production rules) over
historical klines and reports the result in R, gross and net of costs. It runs the same `detect()`
and `resolve()` as the scheduled scan and the evaluator (see `src/strategy/README.md`), not a copy:

| Step | Production function |
|---|---|
| Decision after each closed candle | `Strategy.detect(MarketContext)`, closed candles only, next open as entry |
| `macd_cross`: MACD, cross, entry, stop, TP1 | `compute_macd` + `detect_macd_cross`, inside `MacdCross.detect` |
| Outcome | `Strategy.resolve`; for `macd_cross`, `resolve_outcome` (SL first, expiry after 48 candles) |
| Costs (fee + slippage, cost model `cm1`) | `src.strategy.outcome.cost_adjusted_return`, exit labels priced by `src.strategy.exits.cost_result` |

A strategy whose exit reads indicator history (`resolve_history_bars > 0`, such as the shadow
`macd_cross_ema50d_oppexit@1.0.0`) gets in `ResolveContext.history` exactly the candles the live
evaluator fetches: that many before the cross candle, then the cross candle.

Strategy parameters are the frozen params of the version (`--strategy key@version`), not the
environment. Costs still come from `src/config/settings.py`, as in the app.

`replay_strategy` decides after every closed candle, or only after the candles that the strategy's
`candidate_bars()` shortcut lists. For `macd_cross` those are the crossing candles;
`test_candidate_shortcut_skips_no_signal` proves the shortcut drops no signal.
`replay_symbol`, the Phase 0 replay, is kept as the reference that
`test_port_replays_exactly_the_phase0_trades` compares the port against trade for trade. On the gate
dataset, the trades CSV and the whole report (outside the new `strategy` block) are byte-identical
to Phase 0.

## Higher timeframes (context)

A strategy that reads `context_timeframes` (for example `("1d",)`) gets those klines in one of two
ways:

- `--context-source resample` (default): built from the base series. A 1d candle is the UTC day
  00:00 to 24:00: first 2h open, max high, min low, last 2h close, summed volume, plus `bars`, the
  count of 2h candles in it. When all 12 candles are present this is exactly Binance's 1d kline.
  Against data.binance.vision for BTCUSDT and ETHUSDT, 2024-11 to 2026-01 (457 days each), OHLC
  matched bit for bit and volume to within float rounding (relative difference 2e-16).
  `test_resample_matches_exchange_daily_candles_when_present` checks this on the committed
  `tests/fixtures/klines/BTCUSDT_1d.csv`. A day with missing 2h candles is kept, but its `bars` is
  below 12 and it can differ from the exchange's candle.
- `--context-source csv`: read `{SYMBOL}_{tf}.csv` from `--data`, for example from
  `python scripts/fetch_klines.py --interval 1d ...`.

Either way, `MarketContext` only returns a context candle once it has closed (open + 1 day ≤
decision). The daily candle of day D is therefore first visible to the decision made at 00:00 UTC of
D+1, and a partial last day is never visible.

## Trial log (multiple testing)

Every run appends one JSON line to `research/trials.jsonl`. Override the path with `--trials-log`
or `$RESEARCH_TRIALS_LOG`; the tests point it at a temporary file. Each line holds:

- the strategy, version, params and code hash
- `purpose` (`research`, `gate`, `holdout`, `robustness` or `adhoc`)
- the signal window, symbols and data SHA-256
- cost model and harness version
- the headline metrics per cost scenario, plus out-of-sample when `--oos-start` is given
- `ts`, taken from `--trial-ts` or the current UTC time

Lines are only ever appended, and the file is committed. It is the denominator for any deflated
t-stat: a configuration that was tried and dropped still counts.

`--record-trial` also inserts the run into the `research_trials` table of `$DATABASE_URL`, creating
the table if needed.

## Data

```bash
python scripts/fetch_klines.py --symbols BTCUSDT ETHUSDT --start 2024-11 --end 2026-01 --out data/klines
```

This downloads USDⓈ-M monthly klines from data.binance.vision. Each zip is checked against its
`.CHECKSUM` file and kept in `data/klines_zip/`. A rerun only downloads the months that are
missing or corrupt. The script writes `data/klines/{SYMBOL}_2h.csv` with the columns
`open_time_ms,open,high,low,close,volume,quote_volume`. It needs only the standard library.
`data/` is gitignored.

## Run

```bash
venv/bin/python -m src.backtest --data data/klines --symbols BTCUSDT ETHUSDT \
    --start 2025-01-01 --end 2026-01-01 --oos-start 2025-07-01 \
    --out report.json [--trades-out trades.csv] [--warmup 498] \
    [--strategy macd_cross@2.2.0] [--context-source resample|csv] \
    [--purpose research] [--trial-ts 2026-10-15T00:00:00Z] [--record-trial]
```

- `--start` / `--end`: the window that cross candles must open in (UTC; the end is exclusive).
  Data before `--start` is still used for indicator warm-up. Data after `--end` is used to settle
  trades.
- `--warmup`: a cross needs at least this many candles before it in the file. The default is the
  strategy's `warmup_bars`; for `macd_cross` that is `macd_candles - 2` = 498, which is the history
  a live scan has. With that much history the EMA
  seeds have decayed below float precision, so computing MACD once over the full series gives the
  same values as the live scan's rolling 500-candle window.
  `test_replay_matches_the_live_scan_bar_by_bar` checks this by running the real scan after
  every candle close.
- A cross whose outcome the data cannot settle yet (because it is too close to the end of the
  data) is counted as `unresolved` and left out of the metrics.

The report gives these metrics for each cost scenario:

- n
- outcome mix
- win rate (TP / closed; expired trades are counted separately, never as wins)
- mean gross, cost and net R
- t-stat
- weekly-block bootstrap 95% CI of mean net R (2,000 resamples, fixed seed)
- profit factor
- max drawdown in R, with trades ordered by exit time

Each metric is also broken down per symbol, per side and per half-year, and split in-sample /
out-of-sample at `--oos-start`.

| Scenario | Costs |
|---|---|
| `prod` | What the ledger books: `ROUND_TRIP_FEE_PERCENT` plus per-symbol slippage (`slippage_bps_for`; stops × `STOP_SLIPPAGE_MULTIPLIER`) |
| `fee_only` | `ROUND_TRIP_FEE_PERCENT` only (0.1%) |
| `gross` | none |

## Regression gate

`backtest/baseline.json` holds the gate dataset: BTCUSDT and ETHUSDT, with crosses in 2025 and
2025-07-01 as the OOS split. It also holds the SHA-256 of each CSV, the strategy and cost
parameters, and, for each scenario, `n`, `mean_net_r` and `total_net_r` rounded to 4 dp.

The `backtest-gate` job in `.github/workflows/deploy.yml` fetches the dataset, runs the harness
with `--baseline backtest/baseline.json`, and fails on any difference. `build-and-push` needs this
job to pass, so nothing deploys until it does. The report is uploaded as a build artifact.

A change to the strategy, its parameters or the cost model therefore has to update the baseline
in the same PR, and the reviewer sees the old and new numbers side by side:

```bash
python scripts/fetch_klines.py --symbols BTCUSDT ETHUSDT --start 2024-11 --end 2026-01 --out data/klines
venv/bin/python -m src.backtest --data data/klines --symbols BTCUSDT ETHUSDT \
    --start 2025-01-01 --end 2026-01-01 --oos-start 2025-07-01 \
    --baseline backtest/baseline.json --update-baseline --purpose gate
venv/bin/python -m src.backtest --data tests/fixtures/klines --symbols BTCUSDT --oos-start 2025-02-01 \
    --baseline tests/fixtures/backtest_baseline.json --update-baseline --purpose gate
```

The second baseline is the offline copy of the gate. It covers 2,000 BTCUSDT candles committed
in `tests/fixtures/klines/`, and `tests/test_backtest.py` asserts it, so the gate also runs in
`pytest` with no network access.

## Reference result (12 symbols, Oct 2024 to Sep 2026)

This run matches the 2026-10-14 institutional review:

```bash
venv/bin/python -m src.backtest --data <klines dir> --start 2024-10-01 --end 2026-10-01 \
    --oos-start 2026-01-01 --warmup 300
```

| | Harness | Review (window A) |
|---|---|---|
| n | 7,606 | 7,576 |
| gross | +0.0249R, t 1.86, PF 1.046 | +0.025R, t 1.87, PF 1.05 |
| fee_only | −0.0137R, t −1.03, PF 0.976, max DD 182R | −0.014R, t −1.0, PF 0.976, max DD 180R |
| prod (`cm1`) | −0.0829R, t −6.05, PF 0.866, max DD 658R | n/a (cm1 did not exist yet) |

The 30 extra trades are crosses less than 48 candles before the end of the data that already hit
their stop or target. The harness settles them the way the live evaluator would. The review's
engine dropped them.

With `--warmup 500` (the review's window B), the harness reproduces the review's 7,450 trades one
for one: the same crosses, entries and outcomes, with levels within the 1e-6 rounding of
production.

## Pre-registered holdout test (`macd_cross_ema50d_oppexit@1.0.0`)

`src/backtest/prereg.py` holds the evaluation fixed in
`docs/research/preregistration-macd_cross_ema50d_oppexit-1.0.0.md`: the 12 symbols, crosses from
2020-01-01 to 2024-09-30, the primary metric (mean net R per trade under `cm1`, by its constants
whatever the environment says) and the four pass criteria. It refuses to run when the strategy
differs from its frozen lock entry, and logs the run to `research/trials.jsonl` with purpose
`holdout`:

```bash
venv/bin/python -m src.backtest.prereg --data <sealed holdout dir> --out holdout-report.json
```

`--start`/`--end` exist for sanity runs on data that has already been seen. Such a run is labelled
as not the pre-registered test and logged as `adhoc`.

On the already-seen 2024-10 to 2026-09 data (crosses from 2025-04-19, when the 200-day EMA window
is first full) the sanity run gives n 2,642 and +0.0037R per trade under `cm1` (t 0.12). Under the
review's costs (0.1% fee, 2 bps slippage on six majors and 10 bps elsewhere, stops not marked up,
no funding) the same trades give +0.0420R, against +0.0422R from the review's own engine on that
window. 2,639 trades match that engine one for one with identical exits; 4 differ by EMA seeding
or partial days.
