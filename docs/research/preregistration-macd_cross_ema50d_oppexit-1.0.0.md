# Pre-registration: `macd_cross_ema50d_oppexit@1.0.0`

| | |
|---|---|
| Status | Frozen and registered as `shadow`. **Pre-registered before any holdout run.** |
| Written | 2026-10-15, on branch `feat/phase1-research-platform` (parent commit `f8b4a3c`). The commit that adds this file is the registration record. |
| Holdout | Not run. Nobody has opened the sealed holdout data. |
| Source | Institutional review 2026-10-14, finding A2 and the P1 row "Shadow strategy pre-registered". Benchmark 2026-10-15 §5.5 and §6, Phase 1. |

This document fixes the strategy, the data, the one primary metric and the pass criteria before
the strategy meets data it was not selected on. Changing anything below after the holdout run
makes the result worthless. The rules for what happens next are in §8.

## 1. Rules

The timeframe is 2h, on Binance USDⓈ-M perpetual futures. Every time is UTC epoch milliseconds.

**Signal.**

1. A MACD(12, 26, 9) cross on a **closed** 2h candle. This is the production detection:
   `compute_macd` and `detect_macd_cross`, with the same comparisons as `macd_cross@2.2.0`.
   - A golden cross (MACD ≤ signal on the previous candle, MACD > signal on this one) is LONG.
   - A death cross (≥, then <) is SHORT.
   - A cross needs at least 498 closed 2h candles before the cross candle. That is what a live
     scan of 500 klines (the last one still forming) has.
2. **Daily trend filter.** Take the newest UTC daily candle (00:00–24:00) that has **closed by
   the close of the cross candle**. A daily candle that closes after the cross candle is never
   used. A cross candle closing at 12:00 reads the day that closed at 00:00. The 22:00 candle
   closes at 24:00, together with its own day, so it reads that day.
   - The EMA50 is computed over **exactly the last 200 closed daily closes**, seeded the way
     `compute_macd` seeds its EMAs: `pandas_ta.ema(length=50, talib=False)`. That means the SMA
     of the first 50 closes in the window, then `alpha = 2/51` with `adjust=False`.
   - LONG is taken only if the daily close is **strictly above** the EMA. SHORT is taken only if
     it is **strictly below**. A close equal to the EMA, or fewer than 200 closed days, means no
     trade.
   - Live, the daily candles are Binance `1d` klines. In the replay they are resampled from the
     2h klines: first open, max high, min low, last close. The two are identical whenever the day
     has all 12 2h candles (`test_resample_matches_exchange_daily_candles_when_present`).
3. **Entry** is at the open of the 2h candle after the cross candle (`next_open`).

**Stop.** This is the production swing stop, `calculate_trade_levels`:

- LONG: the lowest low of the cross candle and the 5 candles before it, × 0.998.
- SHORT: the highest high of those candles, × 1.002.
- If that level is on the wrong side of the entry, the stop falls back to 1.5% from the entry.
- The level is rounded to 6 decimals.

**There is no fixed target** (`tp1 = 0`).

**Exit.** Each candle is checked in turn, starting with the entry candle, j = 1 … 96:

1. **Stop first.** If the candle touches the stop (LONG: low ≤ stop; SHORT: high ≥ stop), the
   trade is booked at the stop as `STOPPED`, at the candle's open time. A candle that touches
   the stop and also closes with an opposite cross is a stop. Gaps through the stop are still
   booked at the stop, as `macd_cross@2.2.0` does. The cost model's stop slippage stands in for
   the gap.
2. **Opposite cross.** Otherwise, if the candle closes with the MACD cross opposite to the trade
   (a death cross for a LONG, a golden cross for a SHORT), the trade exits at the **open of the
   next candle**, as `OPPOSITE_CROSS`.
3. **Time stop.** Otherwise, at the close of the 96th candle, the trade exits at that close, as
   `TIME_EXIT`.

An opposite cross on the 96th candle exits at the 97th candle's open, which is the same instant
as the 96th candle's close. A cross on a candle that is still forming never counts. A stop touch
counts as soon as it prints. A new signal is taken whether or not an earlier trade on the symbol
is still open.

**MACD for the exit.** The exit's MACD is computed over the 498 candles before the cross candle,
the cross candle, and the closed candles after it. The live evaluator uses this window
(`resolve_history_bars = 498`, one Binance request of 596 klines from `cross − 498 × 2h`), and
so does the replay. `tests/test_shadow_strategy.py` proves on the fixture that:

- the evaluator books every trade on the same candle, at the same price, with the same label as
  the harness, at the first 2h run that can know it;
- the cron path detects exactly the harness's signals;
- both match an independent implementation that computes the MACD once over the whole series.

## 2. Frozen identity

Params (canonical JSON):

```json
{"macd_candles":500,"macd_fast":12,"macd_signal":9,"macd_slow":26,"max_hold_candles":96,"swing_lookback_candles":5,"trend_ema_bars":200,"trend_ema_period":50}
```

| | SHA-256 |
|---|---|
| `params_hash` (canonical JSON above) | `3088c6678f741e4007d0dd3e4563211e3925784fc90782b559f8c3698da98d64` |
| `code_hash` (`base.source_hash` of the modules below, in this order) | `dfc880b5e79bef9e710076dde64c7f54c5a801603cdf154c8e350f958dd87160` |

The `code_hash` covers the strategy module and every module it imports from `src/strategy/`.
These are the per-module SHA-256 values (source with LF line endings):

| Module | SHA-256 |
|---|---|
| `src.strategy.macd_cross_ema50d_oppexit` | `f74a57ea9f7dac4431fc9d5a41f3d52514ed98bfa112a61b19770caa02716315` |
| `src.strategy.base` | `b724d749b4f18804cd4ebda2454462bdfeab6043930572368ff86afe7b68a175` |
| `src.strategy.context` | `c485335ba55d8585ff9798893c90e782e62636e2171fe159bed9ee955962e19d` |
| `src.strategy.exits` | `1055c0d7c1bf8929a6c2b3060b3725e2b0216cb2e53d77a1ab9d541f57893661` |
| `src.strategy.macd` | `4b15d7926c4cfabb06d91ebd17ec1aee4c15701bfba00655739b90552cae7eca` |
| `src.strategy.macd_cross` | `d09e134ea8ea7a71e442f393f35da9c1591992985823430335329155c2694f20` |
| `src.strategy.outcome` | `2b061c4cc3983fa34a1da4e7701817424d9969437289c746c5791543daf6963d` |

The lock entry is in `src/strategy/versions.lock.json`. The engine refuses to emit signals for
this version if either hash drifts, and `tests/test_shadow_strategy.py` fails if this document
stops matching the lock.

The libraries are pinned in `requirements.txt`: `pandas==3.0.1` and `pandas-ta==0.4.71b0`.
TA-Lib is not installed and is not used.

These analysis modules were in place when this was registered. Their SHA-256 values are
informational, and the holdout report records its own:

| File | SHA-256 |
|---|---|
| `src/backtest/prereg.py` | `007d12b57b3e45efeb30a669e0f7307f1f255b986dd23786e5bf8373a0ae597b` |
| `src/backtest/run.py` | `ca84a31c1eb51236277842a61f640eee6603a3c8ed775f3c0a0323f3260d8185` |
| `src/backtest/replay.py` | `db6cb7a89d21a70eec42251fb26e42119f896217acc31749d548f1a9ca8f47de` |
| `src/backtest/metrics.py` | `9050442772f77a1a2f63fb4c2d1d3ab0cc6db663c8d83225f03ce2995b102dec` |
| `src/backtest/data.py` | `de64c85e8e0a52bdd879692bff9c1937feee4ab279869c4552a162d397da5884` |

## 3. Cost model `cm1`

These are the Phase 0 cost model's constants. The evaluation uses them by value
(`prereg.CM1`), whatever the environment overrides.

| Parameter | Value |
|---|---|
| Round-trip fee | 0.1% of notional |
| One-way slippage on market fills | BTCUSDT 2, ETHUSDT 2, SOLUSDT 8, XRPUSDT 10, ZECUSDT 20, NEARUSDT 25 bps; every other symbol (BNB, DOGE, ADA, LINK, AVAX, LTC) 10 bps |
| Stop fill | slippage × 1.5 |

How each exit is filled:

- **Entry:** market, so it pays slippage.
- **`STOPPED`:** fills like a stop, at 1.5 × slippage.
- **`OPPOSITE_CROSS`** (next open) and **`TIME_EXIT`** (close): market fills with plain slippage.

The mapping is `src.strategy.exits.cost_result`. R is measured against the planned risk
|entry − stop| / entry, and net R = gross R − cost R.

Funding is **not** in `cm1`, and funding history for 2020–2024 has not been acquired. This is a
known omission. It is fixed now and cannot be added afterwards to rescue or sink the result.

## 4. Holdout dataset

| | |
|---|---|
| Symbols (12) | Watchlist: BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, NEARUSDT, ZECUSDT. Control: BNBUSDT, DOGEUSDT, ADAUSDT, LINKUSDT, AVAXUSDT, LTCUSDT |
| Market | Binance USDⓈ-M perpetuals, 2h klines, monthly archives from `data.binance.vision/data/futures/um/monthly/klines/{SYMBOL}/2h/`, each zip checked against its published `.CHECKSUM` (`scripts/fetch_klines.py`) |
| Signals | Cross candles opening from **2020-01-01 00:00** to **2024-09-30 22:00** inclusive (`--start 2020-01-01 --end 2024-10-01`, end exclusive) |
| Warm-up | Data from **2019-09** where the archive has it. A symbol listed later starts at its first archived month. A cross counts only once there are 498 2h candles before it and 200 closed days for the EMA, so BTCUSDT (first archived month 2019-09) can signal from about late March 2020, and later listings correspondingly later |
| Daily candles | Resampled from the same 2h files (`context_source = resample`) |
| Location | A sealed directory of `{SYMBOL}_2h.csv` files, downloaded for this test and not opened by any analysis before the run. Its file SHA-256 values are recorded by the run, in the report and in `research/trials.jsonl`. They are not listed here, because computing them would mean opening the data |
| End of data | Files are read as they are. A trade still open at the end of the files is reported as `unresolved` and excluded. Seen data from 2024-10 is **not** appended to settle it |

## 5. Primary metric and pass criteria

The single primary metric is the **mean net R per trade under `cm1`**, over all 12 symbols.

The strategy **passes** only if all four criteria hold:

1. **Edge and significance.** Mean net R > 0, **and** the weekly-block bootstrap 95% CI has a
   lower bound > 0, **and** t ≥ 2.0.
   - The CI resamples whole calendar weeks (Monday 00:00 UTC, by the cross candle), with 2,000
     resamples and seed 20261015 (`metrics.weekly_block_ci`).
   - t = mean / (sd / √n), with sample sd (ddof 1).
2. **Impact.** Mean net R is still > 0 with an extra **+10 bps round-trip** impact: the fee
   becomes 0.2%, everything else stays `cm1`.
3. **Tails.** Mean net R is still > 0 after removing the **top 1% of trades by net R**:
   ⌈0.01 · n⌉ trades, ranked under `cm1`.
4. **Breadth.** The result is not driven by one symbol: mean net R > 0 in **at least 7 of the 12
   symbols**. A symbol with no trades counts as not positive.

**Secondary results.** These are reported and never change the verdict. The same figures are
computed separately for:

- the **control basket** (BNB, DOGE, ADA, LINK, AVAX, LTC);
- the **watchlist** (BTC, ETH, SOL, XRP, NEAR, ZEC).

For a 6-symbol basket, criterion 4 is held to the same share, 4 of 6. The report also gives:

- the count of each exit label;
- the mean gross R;
- the per-symbol figures.

The evaluation is `src/backtest/prereg.py`. It refuses to run unless the strategy matches its
frozen lock entry:

```bash
venv/bin/python -m src.strategy verify
venv/bin/python -m src.backtest.prereg --data <sealed holdout dir> \
    --out docs/research/holdout-macd_cross_ema50d_oppexit-1.0.0.json
```

The run is appended to `research/trials.jsonl` with purpose `holdout`.

## 6. Multiple-testing context

This candidate is not a fresh hypothesis.

- **How it was chosen.** The 2026-10-14 review tested about **40 configurations** on the same
  12 symbols over **2024-10 → 2026-09**: filters, stops, targets and exits. This one was
  **chosen after seeing that data**, including the half that was meant to be out of sample.
- **Its record on that data.** Under the review's costs:
  - full sample: +0.054R per trade (t 2.11);
  - out of sample: +0.093R (t 2.02);
  - Bonferroni threshold for about 40 tries: t ≈ 3.2, which it does not reach.
- **Fragility on that data:**
  - +0.015R (t 0.61) with +10 bps impact;
  - −0.012R after dropping the 2 best trades per symbol (0.7% of trades);
  - most of the edge came from the 2024H2 EMA warm-up period, and about +0.011R was left without it.
- **What the holdout is.** It is one pre-registered test on data the selection never touched, so
  its t is judged against 2.0 and is not deflated for those 40 tries. The prior for a real edge
  is weak all the same.
- **Stricter gates elsewhere.** The roadmap's Phase 1 exit gate (benchmark §6) also asks for
  t ≥ ~3 deflated across all trials. The institutional review's gate for real money adds forward
  live-shadow evidence. Passing this pre-registration is necessary for those gates, but it is
  not enough to meet them.

**Sanity run on already-seen data only.** This checks that the code works. It is not evidence.
The rules above were replayed on 2024-10 → 2026-09. Crosses start on 2025-04-19, when the
200-day EMA window first fills.

| Cost assumption | Mean net R per trade |
|---|---|
| `cm1` | +0.0037R, n 2,642, t 0.12 |
| The review's costs: 0.1% fee, 2 bps on BTC/ETH/SOL/XRP/BNB/DOGE and 10 bps elsewhere, stop not marked up, no funding | +0.0420R |
| The review's own engine on the same window | +0.0422R |

2,639 trades match the review's engine one for one, with identical exit labels and times. The
other 4 differ because of EMA seeding (the review seeded at the first day of data) or partial
days. The trial is logged in `research/trials.jsonl` with purpose `adhoc`.

## 7. Live shadow

From deployment the version runs in the scheduled scan with status `shadow`:

- it is detected, stored in `macd_signals` under its own `strategy_key`/`strategy_version`, and
  evaluated;
- it is never alerted unless `SHADOW_ALERTS=true`;
- it is never counted in the headline track record.

Forward shadow trades are the only evidence that accrues after the holdout. The review's interim
rule applies: at least 12 months, and t ≥ 2 after realistic costs including impact. About 3,600
trades would be needed to show +0.05R at t = 2.

## 8. Commitments

1. **The result will be published whatever it is.** That includes the full JSON report, the
   trades, and the `trials.jsonl` line. They go in `docs/research/` next to this file, together
   with a short write-up, whether the verdict is PASS or FAIL.
2. **No parameter or rule of `macd_cross_ema50d_oppexit@1.0.0` may change after the result is
   seen.** Any change is a new version (`1.0.1`, `1.1.0`, …), and a new version needs a new
   holdout. No unseen history remains after this one, because 2020–2024 is the last of it. So
   for any changed version **only forward live-shadow evidence counts**.
3. **Bugs.** If a bug in the harness or the evaluation is found after the run, both runs are
   published, the original and the corrected one, with the fix. A fix may not touch the frozen
   strategy modules, whose `code_hash` must be unchanged or rehashed after a proven
   byte-identical replay. A fix that changes the rules is a new version (see 2).
4. **The holdout is run once**, with the command in §5, by someone who has not looked at the
   sealed files.
