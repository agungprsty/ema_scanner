# Backtest harness

Replays the production MACD strategy over historical 2h klines and reports the result in R,
gross and net of costs. It runs the live code, not a copy of it:

| Step | Production function |
|---|---|
| MACD | `src.strategy.macd.compute_macd` |
| Cross, entry (next open), stop, TP1 | `src.strategy.macd.detect_macd_cross` (called once per cross) |
| Outcome (SL first, expiry after 48 candles) | `src.strategy.outcome.resolve_outcome` |
| Costs (fee + slippage, cost model `cm1`) | `src.strategy.outcome.cost_adjusted_return` |

Parameters come from `src/config/settings.py`, so the harness uses the same environment variables
as the app.

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
    --out report.json [--trades-out trades.csv] [--warmup 498]
```

- `--start` / `--end`: the window that cross candles must open in (UTC; the end is exclusive).
  Data before `--start` is still used for indicator warm-up. Data after `--end` is used to settle
  trades.
- `--warmup`: a cross needs at least this many candles before it in the file. The default is
  `MACD_CANDLES - 2` = 498, which is the history a live scan has. With that much history the EMA
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
    --baseline backtest/baseline.json --update-baseline
venv/bin/python -m src.backtest --data tests/fixtures/klines --oos-start 2025-02-01 \
    --baseline tests/fixtures/backtest_baseline.json --update-baseline
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
