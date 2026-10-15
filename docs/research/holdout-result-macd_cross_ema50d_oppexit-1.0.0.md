# Holdout result: `macd_cross_ema50d_oppexit@1.0.0`

| | |
|---|---|
| Verdict | **FAIL.** All four pre-registered criteria fail. |
| Primary metric | Mean net R per trade under `cm1`, 12 symbols: **−0.0243R** (n 7,354, t −1.35, weekly-block 95% CI [−0.0785, +0.0346]) |
| Consequence | Per pre-registration §8: the strategy **stays research-only and is not promoted**. It is not forward-shadowed as a promotion candidate (see §7 below). No parameter of 1.0.0 may change, and no unseen history is left for a changed version. |
| Run | Once, 2026-10-15T03:30:31Z, with the §5 command, on the sealed 2020-01 → 2024-09 holdout. Logged as line 3 of `research/trials.jsonl` (purpose `holdout`, `prereg.verdict = FAIL`) |
| Pre-registration | [`preregistration-macd_cross_ema50d_oppexit-1.0.0.md`](preregistration-macd_cross_ema50d_oppexit-1.0.0.md) |

## 1. Integrity checks before the run

### 1.1 Strategy identity

Both hashes were recomputed **independently** of the registry, from the rule in the pre-registration
§2: `params_hash` = SHA-256 of the canonical JSON; `code_hash` = SHA-256 over, for each module in the
listed order, `name + "\0" + source(LF) + "\0"`. They were also checked through the registry with
`python -m src.strategy verify`, which passed.

| | Pre-registered | Recomputed at run time | Match |
|---|---|---|---|
| `params_hash` | `3088c6678f741e4007d0dd3e4563211e3925784fc90782b559f8c3698da98d64` | `3088c6678f741e4007d0dd3e4563211e3925784fc90782b559f8c3698da98d64` | yes |
| `code_hash` | `dfc880b5e79bef9e710076dde64c7f54c5a801603cdf154c8e350f958dd87160` | `dfc880b5e79bef9e710076dde64c7f54c5a801603cdf154c8e350f958dd87160` | yes |

All seven per-module SHA-256 values in §2 matched one for one. So did the five analysis files:
`prereg.py` `007d12b5…`, `run.py` `ca84a31c…`, `replay.py` `db6cb7a8…`, `metrics.py` `90504427…` and
`data.py` `de64c85e…`. The runtime was Python 3.13.7, `pandas==3.0.1` and `pandas-ta==0.4.71b0`, with
no TA-Lib installed. Other files at run time:

| File | SHA-256 |
|---|---|
| `preregistration-macd_cross_ema50d_oppexit-1.0.0.md` | `2b2171da7110b0ffc579181ae74e0ffcc00ebb6c5d18d41b5b48950d595f53f7` |
| `src/strategy/versions.lock.json` | `190b83e5f4231b2accc8555e50f091ef5b24f5cd955536b4ef57d598e1a31f3e` |
| `requirements.txt` | `b178d17cf62a8a11bd32aeaa922746d898220520ab2ae30e2d3a6d0be6723d4a` |

`MACD_TIMEFRAME=4h` in the local `.env` was checked and has no effect. The strategy and harness both
hard-code `2h`, and the frozen params hash covers the rest. The `.env` sets no cost overrides, and the
evaluation uses `cm1` by value anyway.

### 1.2 Holdout data

| | |
|---|---|
| Directory | `holdout_klines/` in the session scratchpad: 12 files `{SYMBOL}_2h.csv` plus `MANIFEST.json` |
| Manifest SHA-256 | `77f2645bab85df786985dd2e4a99106fd24dc739a0a81bf0335796ae16c25b71` (created 2026-10-15T02:22:10Z by `scripts/fetch_klines.py`; every source zip was verified against its `.CHECKSUM`) |
| File check | **12 of 12** files match the manifest's SHA-256 and byte count. The harness recorded the same hashes in the report and in `trials.jsonl` |

| File | SHA-256 | First candle (UTC) | Bars | Missing bars |
|---|---|---|---|---|
| BTCUSDT_2h.csv | `c2501842f01df04303b85fc46b79767fa75ecb7603c9f630fd444527550966ee` | 2020-01-01 00:00 | 20,820 | 0 |
| ETHUSDT_2h.csv | `28f5b1cb4a7df7a20e4ef1552f485a068b08d48d1ee74e93eeb93c85c1f3bf45` | 2020-01-01 00:00 | 20,820 | 0 |
| SOLUSDT_2h.csv | `3931275cfe6ada7af1b85d147c509afa636e3cc2e71cbb1691f76dc443cc6034` | 2020-09-14 06:00 | 17,673 | 60 |
| XRPUSDT_2h.csv | `fbfdb70fb5da5108b065d7f0ea56e1a3a96525af8b209e949dd71578f44001cd` | 2020-01-06 08:00 | 20,696 | 60 |
| NEARUSDT_2h.csv | `62ed1687beed6667c1a9b0483ba79621fb176d184428320aa1b1599af1c07d61` | 2020-10-15 08:00 | 17,300 | 60 |
| ZECUSDT_2h.csv | `e8ee5e442fe8068a41c1aad4a44c447ddfd02ddfcfdf640135b4c26339301d09` | 2020-02-05 08:00 | 20,336 | 60 |
| BNBUSDT_2h.csv | `52d4aebc57544db4c27acdd4b52f3e7a22ef57fa9286307b12bbb0e810a78dce` | 2020-02-10 08:00 | 20,336 | 0 |
| DOGEUSDT_2h.csv | `187f0a8f33d9f4b0c6a3e0e260ba5295ec8968628f80275ac6448b2749dd0be2` | 2020-07-10 08:00 | 18,524 | 0 |
| ADAUSDT_2h.csv | `48be140c3c2ecbe2f114d77752c5af752bd6e93e45224ad3941fa3d7e827c17d` | 2020-01-31 08:00 | 20,456 | 0 |
| LINKUSDT_2h.csv | `a2501368fb44711bbc38d69e322ecf6b7503f88277eb0fd930b30c2ebeb2f2a1` | 2020-01-17 08:00 | 20,624 | 0 |
| AVAXUSDT_2h.csv | `16aec1e94ce30a72136cb0fa5e596863b69764129c44f01ef242f75b94e4e9a6` | 2020-09-23 06:00 | 17,625 | 0 |
| LTCUSDT_2h.csv | `4d0d45763a91a6889e71c7dc69d9508cd54ab243856ab2e478176475eb2172b6` | 2020-01-09 08:00 | 20,660 | 0 |

Every file ends at the 2024-09-30 22:00 candle and has no duplicates, misaligned or malformed rows.

**Deviations from what the pre-registration expected. Both are disclosed, and neither changes a rule.**

1. **No 2019 warm-up.** §4 says "data from 2019-09 where the archive has it" and assumes BTCUSDT's
   first archived month is 2019-09. The `um` 2h monthly archive has no 2019 months at all:
   `BTCUSDT-2h-2019-09.zip` and `-2019-12.zip` both return HTTP 404. So every series starts in
   2020-01 or later.
   - The rule as written is satisfied ("where the archive has it"). The parenthetical example in
     §4 was factually wrong.
   - Effect: the first crosses that clear the 200-closed-day EMA window fall on 2020-07-19 for BTC
     and ETH, not in late March 2020. The other symbols start later, from 2020-07-25 (XRP) to
     2021-05-07 (NEAR).
   - Roughly four months of potential BTC/ETH crosses (2020-03 → 2020-07) are not in the sample.
     This was fixed by the archive, not by a choice made after seeing results.
2. **Archive gaps.** SOL, XRP, NEAR, ZEC and LTC have the same two holes:
   - 2022-02-26 → 2022-02-28, 36 bars;
   - 2022-04-01 → 2022-04-02, 24 bars.

   Per §4, "files are read as they are". The MACD runs across the gap as if the candles were
   adjacent, and those calendar days are simply absent from the resampled daily series. None of
   the candidate's trades was open across a gap. The effect is limited to signals and EMA windows
   near those dates.

**Procedural note.** At run time the pre-registration, the lock and the strategy code were still
uncommitted on `feat/phase1-research-platform`, at parent commit `f8b4a3c`. The registration
commit called for in §1 of the pre-registration therefore does not predate the run. The evidence
that the rules came first is:

- the lock's `frozen_at` of 2026-10-15T03:12:04Z;
- the file hashes above, recorded before the run;
- the run's own timestamp of 03:30:31Z, recorded in `trials.jsonl`.

The commit that adds these files should include this report and the unchanged pre-registration.

## 2. What was run

| # | Run | Command | Logged |
|---|---|---|---|
| 1 | **The pre-registered test** (once) | `python -m src.strategy verify` then `python -m src.backtest.prereg --data holdout_klines --out docs/research/holdout-macd_cross_ema50d_oppexit-1.0.0.json` | `trials.jsonl` line 3, 03:30:31Z, purpose `holdout`, verdict FAIL |
| 2 | Reference: live `macd_cross@2.2.0` on the same data and window. Not selective; no verdict | `python -m src.backtest --data holdout_klines --strategy macd_cross@2.2.0 --symbols <the 12> --start 2020-01-01 --end 2024-10-01 --purpose research --out … --trades-out …` | `trials.jsonl` line 4, 03:32:22Z, purpose `research` |

Run 1 went through a thin wrapper that calls `prereg.main` with exactly those arguments. The
wrapper also saves the trade list that `run_backtest` returns, because the CLI does not write it.
It asserted that the replay ran exactly once.

Every figure below comes from the trade lists of these two runs. Nothing was replayed again, and no
other parameters were tried. The extra cost views (gross, the review's costs, review costs +10 bps)
re-price the same trades.

**Published artifacts** (pre-registration §8.1):

| File | SHA-256 |
|---|---|
| [`holdout-macd_cross_ema50d_oppexit-1.0.0.json`](holdout-macd_cross_ema50d_oppexit-1.0.0.json): evaluation plus full harness report | `ab68038a6ce710799e1c19a65bcb5fa0203f5d40daae4bd6ca17e4e8075a6449` |
| [`holdout-trades-macd_cross_ema50d_oppexit-1.0.0.csv`](holdout-trades-macd_cross_ema50d_oppexit-1.0.0.csv): 7,354 trades | `e7aa503c1051e8ab00e6d8f961d85a539a32f546472a1e39e3527f24df8dabb7` |
| [`holdout-reference-macd_cross-2.2.0.json`](holdout-reference-macd_cross-2.2.0.json): reference report | `1354e6e6f3acfacbaad8b72a3becea8bd4ca19cfbe255e17c27702207c171b68` |
| [`holdout-reference-trades-macd_cross-2.2.0.csv`](holdout-reference-trades-macd_cross-2.2.0.csv): 17,489 trades | `eb9274fe8e8443c327589130eb5755595762b66a59bcb848fcd7f64060cd7a4d` |

There were 7,356 crosses: 7,354 resolved and 2 left `unresolved` at the end of the data, which are
excluded per §4. Exits were 6,046 `OPPOSITE_CROSS` and 1,308 `STOPPED`. There were **no
`TIME_EXIT`s**: no trade reached the 96th candle without a stop or an opposite cross. Holding time
was a median of 9 candles (mean 10.9). The planned risk was a median of 3.64% of entry.

## 3. Verdict per criterion (12 symbols, `cm1`)

| # | Criterion | Required | Result | Verdict |
|---|---|---|---|---|
| 1 | Edge and significance | mean > 0, **and** CI lower bound > 0, **and** t ≥ 2.0 | mean **−0.0243R**; CI [−0.0785, +0.0346]; t **−1.35** | **FAIL** (all three parts) |
| 2 | Impact +10 bps round trip (fee 0.2%) | mean > 0 | **−0.0588R**, t −3.26, CI [−0.1133, −0.0001] | **FAIL** |
| 3 | Without the top 1% of trades by net R (⌈0.01·7,354⌉ = 74 dropped) | mean > 0 | **−0.1153R**, t −9.48, CI [−0.1575, −0.0729] | **FAIL** |
| 4 | Breadth | mean net R > 0 in ≥ 7 of 12 symbols | **4 of 12** (BTC, ETH, SOL, DOGE) | **FAIL** |
| | **Overall** | all four | | **FAIL** |

The secondary baskets fail all four criteria as well:

- **Control:** 1 of 6 symbols positive, against the 4 of 6 required.
- **Watchlist:** 3 of 6 positive.

## 4. Tables

Definitions:

- **WR** is the share of trades with net R > 0. There is no fixed target, so a target-hit win
  rate does not apply.
- **Cost** is the mean `cm1` cost in R.
- **CI** is the weekly-block bootstrap 95% CI of mean net R (2,000 resamples, seed 20261015).
- **PF** is the sum of winning net R over the sum of losing net R.
- **Max DD** is the deepest fall of cumulative net R, with trades ordered by exit time. Every trade
  counts as 1R of risk, and overlapping trades are summed. It is not a portfolio drawdown.

### 4.1 Headline and cost views

| View (12 symbols) | n | WR | Gross R | Cost R | Net R | t | 95% CI | PF | Max DD R | Total R |
|---|---|---|---|---|---|---|---|---|---|---|
| **`cm1` (primary)** | 7,354 | 31.5% | +0.0809 | 0.1052 | **−0.0243** | −1.35 | [−0.0785, +0.0346] | 0.945 | 525.0 | −178.5 |
| `cm1` +10 bps impact | 7,354 | 30.7% | +0.0809 | 0.1397 | −0.0588 | −3.26 | [−0.1133, −0.0001] | 0.875 | 717.3 | −432.2 |
| `cm1` without top 1% (74) | 7,280 | 30.8% | −0.0107 | 0.1046 | −0.1153 | −9.48 | [−0.1575, −0.0729] | 0.743 | 870.2 | −839.6 |
| Gross (no costs) | 7,354 | 34.1% | +0.0809 | 0 | +0.0809 | +4.50 | [+0.0272, +0.1379] | 1.219 | 105.9 | +595.3 |
| Review's costs* | 7,354 | 32.2% | +0.0809 | 0.0721 | +0.0088 | +0.49 | [−0.0452, +0.0673] | 1.021 | 351.2 | +64.7 |
| Review's costs* +10 bps | 7,354 | | | | −0.0257 | −1.43 | | | | |

\* 0.1% fee; 2 bps on BTC/ETH/SOL/XRP/BNB/DOGE and 10 bps elsewhere; no stop markup; no funding.
Shown only for comparison with §5.

The 74 best trades (net R ≥ +5.31 each) sum to **+661.1R**. All 7,354 trades together sum to
**−178.5R**.

### 4.2 Watchlist vs control (secondary)

| Basket | n | WR | Gross R | Cost R | Net R | t | 95% CI | PF | Max DD R | +10 bps | −top 1% | Positive symbols |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Watchlist (BTC ETH SOL XRP NEAR ZEC) | 3,629 | 31.5% | +0.0896 | 0.1059 | −0.0163 | −0.71 | [−0.0761, +0.0448] | 0.963 | 233.5 | −0.0518 | −0.1010 | 3/6 |
| Control (BNB DOGE ADA LINK AVAX LTC) | 3,725 | 31.5% | +0.0725 | 0.1046 | −0.0320 | −1.16 | [−0.1029, +0.0385] | 0.928 | 304.4 | −0.0655 | −0.1308 | 1/6 |

### 4.3 Per symbol (`cm1`)

| Symbol | Basket | n | WR | Gross R | Cost R | Net R | t | 95% CI | PF | Max DD R |
|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | W | 646 | 30.5% | +0.0946 | 0.0716 | **+0.0230** | +0.40 | [−0.0785, +0.1310] | 1.055 | 54.3 |
| ETHUSDT | W | 609 | 32.5% | +0.1668 | 0.0592 | **+0.1076** | +1.70 | [−0.0022, +0.2280] | 1.260 | 22.3 |
| SOLUSDT | W | 519 | 37.0% | +0.1271 | 0.0676 | **+0.0595** | +1.08 | [−0.0404, +0.1709] | 1.156 | 21.9 |
| XRPUSDT | W | 643 | 30.2% | +0.0720 | 0.1228 | −0.0509 | −0.89 | [−0.1584, +0.0629] | 0.890 | 63.3 |
| NEARUSDT | W | 556 | 31.8% | +0.0882 | 0.1596 | −0.0714 | −1.34 | [−0.1644, +0.0290] | 0.848 | 56.4 |
| ZECUSDT | W | 656 | 28.2% | +0.0018 | 0.1511 | −0.1493 | −3.13 | [−0.2380, −0.0538] | 0.705 | 100.6 |
| BNBUSDT | C | 654 | 31.8% | +0.1343 | 0.1365 | −0.0023 | −0.03 | [−0.1357, +0.1572] | 0.995 | 64.3 |
| DOGEUSDT | C | 569 | 33.2% | +0.2035 | 0.1075 | **+0.0960** | +0.78 | [−0.1040, +0.3813] | 1.215 | 68.3 |
| ADAUSDT | C | 637 | 30.9% | +0.0418 | 0.1017 | −0.0599 | −1.27 | [−0.1522, +0.0330] | 0.863 | 58.0 |
| LINKUSDT | C | 653 | 32.2% | +0.0305 | 0.0846 | −0.0541 | −1.14 | [−0.1406, +0.0387] | 0.872 | 51.9 |
| AVAXUSDT | C | 539 | 33.6% | +0.0767 | 0.0834 | −0.0067 | −0.13 | [−0.0994, +0.0847] | 0.984 | 30.5 |
| LTCUSDT | C | 673 | 28.2% | −0.0317 | 0.1100 | −0.1417 | −3.15 | [−0.2314, −0.0546] | 0.703 | 99.0 |

No single symbol has a CI that excludes zero on the positive side.

### 4.4 Per year of the cross candle (`cm1`)

| Year | n | WR | Gross R | Cost R | Net R | t | 95% CI | PF | Max DD R |
|---|---|---|---|---|---|---|---|---|---|
| 2020 (from 2020-07-19) | 484 | 30.2% | +0.1448 | 0.0899 | +0.0549 | +0.76 | [−0.1307, +0.2325] | 1.124 | 51.3 |
| 2021 | 1,753 | 37.7% | +0.2325 | 0.0675 | **+0.1650** | +3.26 | [+0.0361, +0.3084] | 1.432 | 54.4 |
| 2022 | 1,806 | 31.0% | +0.0365 | 0.0974 | −0.0609 | −2.23 | [−0.1613, +0.0356] | 0.855 | 170.4 |
| 2023 | 1,886 | 29.5% | +0.0687 | 0.1464 | −0.0777 | −2.33 | [−0.1824, +0.0384] | 0.841 | 212.8 |
| 2024 (to 2024-09-30) | 1,425 | 27.7% | −0.0545 | 0.1123 | −0.1669 | −5.32 | [−0.2697, −0.0405] | 0.661 | 245.7 |

### 4.5 Per side and per exit (`cm1`)

| Group | n | WR | Gross R | Cost R | Net R | t | 95% CI | PF | Max DD R |
|---|---|---|---|---|---|---|---|---|---|
| LONG | 3,450 | 33.4% | +0.1455 | 0.0904 | +0.0551 | +1.78 | [−0.0246, +0.1432] | 1.131 | 214.3 |
| SHORT | 3,904 | 29.8% | +0.0239 | 0.1183 | −0.0944 | −4.70 | [−0.1724, −0.0165] | 0.797 | 369.2 |
| `OPPOSITE_CROSS` | 6,046 | 38.3% | +0.3148 | 0.0930 | +0.2218 | | | | |
| `STOPPED` | 1,308 | 0% | −1.0000 | 0.1618 | −1.1618 | | | | |
| `TIME_EXIT` | 0 | | | | | | | | |

### 4.6 Reference: live `macd_cross@2.2.0` on the same holdout (not a test, no verdict)

This replays the same 12 symbols and the same cross window with `cm1`, through the same evaluation
functions. It gives a baseline for what the trend filter and the opposite-cross exit changed.

| View | n | WR (target hit) | Gross R | Cost R | Net R | t | 95% CI | PF | Max DD R |
|---|---|---|---|---|---|---|---|---|---|
| `cm1` | 17,489 | 37.0% | +0.0494 | 0.0996 | **−0.0502** | −5.51 | [−0.0735, −0.0266] | 0.917 | 908.6 |
| `cm1` +10 bps | 17,489 | | | | −0.0854 | −9.37 | [−0.1102, −0.0610] | 0.864 | 1,515.8 |
| `cm1` without top 1% (175) | 17,314 | | | | −0.0657 | −7.21 | [−0.0881, −0.0427] | 0.893 | 1,161.2 |
| Gross | 17,489 | 37.0% | +0.0494 | 0 | +0.0494 | +5.55 | [+0.0284, +0.0708] | 1.092 | 92.6 |
| Fee only (0.1%) | 17,489 | 37.0% | | | +0.0141 | +1.59 | [−0.0074, +0.0360] | 1.025 | 230.8 |
| Review's costs | 17,489 | | | | −0.0170 | −1.90 | [−0.0394, +0.0054] | 0.971 | 524.7 |

The reference was positive in 3 of 12 symbols (ETH, SOL, ADA). Its net R was negative in every year
except 2021 (+0.0240R, t 1.26):

| Year | 2020 | 2021 | 2022 | 2023 | 2024 |
|---|---|---|---|---|---|
| Net R | −0.0787 | +0.0240 | −0.0251 | −0.1244 | −0.0584 |

By side:

- LONG: −0.0550R
- SHORT: −0.0454R

By basket:

- watchlist: −0.0539R
- control: −0.0466R

Against this baseline, the candidate raised gross R per trade from +0.049 to +0.081. It also took
58% fewer trades and cut the net loss per trade by half, from −0.050R to −0.024R. That is still
short of breaking even under `cm1`, and nowhere near significance.

## 5. Comparison with the selection window (2024-10 → 2026-09)

| | Selection window (seen data) | Holdout 2020-07 → 2024-09 (unseen) |
|---|---|---|
| n | 2,642 (frozen code, crosses from 2025-04-19) | 7,354 |
| Gross R | +0.1267, t 4.26 | +0.0809, t 4.50 |
| **`cm1` net R** | **+0.0037, t 0.12** | **−0.0243, t −1.35** |
| Review's costs, frozen code | +0.0420 | +0.0088, t 0.49 |
| Review's costs, the review's own engine/window (selection figure) | +0.054, t 2.11; OOS half +0.093, t 2.02 | not applicable |
| +10 bps impact | +0.015R, t 0.61 (review's costs) | −0.0257, t −1.43 (review's costs); −0.0588, t −3.26 (`cm1`) |
| Drop best trades | −0.012R after the 2 best per symbol (0.7%) | −0.1153R after the top 1% (`cm1`) |
| Positive symbols (`cm1`) | not computed under `cm1` | 4 / 12 |

The gross edge survives out of sample and is about as significant: t 4.50 on n 7,354, against
t 4.26 on n 2,642. Its size fell by a third, from +0.127R to +0.081R per trade.

After realistic costs, the result fell from about zero to slightly negative:

- under the review's costs, from +0.042R to +0.009R;
- under `cm1`, from +0.004R to −0.024R.

That is the pattern of selection bias: the configuration that looked best after about 40 tries
gives back most of its apparent edge on unseen data. It is also the pattern predicted by the
fragility seen at selection time:

- the impact sensitivity;
- the dependence on a handful of trades;
- the edge concentrated in one warm-up period.

## 6. Interpretation

1. **There is no tradable edge after costs.**
   - Under the pre-registered cost model the mean is negative, and its CI is centred below zero.
   - It turns significantly negative with +10 bps of impact: t −3.26, with a CI upper bound just
     below 0.
   - The gross MACD/trend signal is real in the statistical sense (t 4.5). But a +0.08R gross edge
     on a median 3.6% stop cannot pay a cost of about 0.105R per trade.
   - The live strategy shows the same structure: +0.049R gross, −0.050R net.
2. **The mean depends on rare outliers.**
   - The 74 best trades, 1% of the total, are worth +661R.
   - Without them the strategy loses −0.115R per trade, with t −9.5.
   - The edge, such as it is, lives in a few trend legs that run for many candles before the
     opposite cross. These are mostly in 2021: +0.165R, t 3.26.
   - From 2022 on, every year is negative. 2024 is the worst, at −0.167R and t −5.3.
   - This is a regime-dependent profile, not a stable one.
3. **Breadth fails.**
   - Only BTC, ETH, SOL and DOGE are positive, and none of them significantly.
   - ZEC and LTC are significantly negative.
   - The control basket (1 of 6 positive) is worse than the watchlist (3 of 6). That points to
     noise rather than a watchlist effect.
4. **What not to do with the side split.**
   - LONG +0.055R (t 1.78) against SHORT −0.094R (t −4.70) looks like an invitation to make a
     long-only variant.
   - That would be a post-hoc choice made on this holdout, the last unseen history. Per §8.2 any
     such variant is a new version whose only admissible evidence is forward live shadow. The
     split is reported here only because §5 asks for per-side results.
   - It also coincides with the 2020–2021 bull market, which makes a regime explanation more
     likely than a structural one.
5. **Multiple-testing caveats.**
   - The holdout t is judged against 2.0 because it is a single pre-registered test, and it fails
     that undeflated bar by a wide margin.
   - The roadmap's Phase 1 gate asks for t ≥ ~3 deflated across all trials, which is further out of
     reach still.
   - Nothing in this result calls for a deflation argument: the point estimate is negative.
   - The reference run of `macd_cross@2.2.0` is a second, non-selective look at the same data. It
     selected nothing and changes no verdict.
   - The extra cost views in §4.1 and §5 re-price the same trades for comparison only.
6. **Limits of this test. None of them could turn the verdict to PASS.**
   - **Funding is not modelled.** This is fixed by the pre-registration, and it is a cost that
     would mostly lower results further.
   - **The 2019 warm-up did not exist in the archive.** The sample starts in 2020-07 for BTC and
     ETH (see §1.2).
   - **Two 2022 archive gaps on 5 symbols.** No candidate trade was open across them.
   - **Max DD sums overlapping 1R trades across 12 symbols.** It is a stress indicator, not a
     sizing-aware drawdown.

## 7. What happens next (per pre-registration §8)

- **`macd_cross_ema50d_oppexit@1.0.0` is not promoted.**
  - It stays research-only, and no money is put on it.
  - There are no paid distribution, OMS, or "edge" claims based on it.
  - The Phase 1 exit gate (benchmark §6) and the real-money pilot gate (institutional review) both
    fail at their first condition.
- **No parameter or rule of 1.0.0 changes.** Its lock entry and `code_hash` stay as they are, as
  the record of what was tested.
  - Any variant is a new version (for example a long-only one, other EMA lengths or a cost-aware
    universe).
  - With 2020–2024 now spent, a new version can only be judged on **forward live shadow**: at least
    12 months, and t ≥ 2 after realistic costs including impact. That is about 3,600 trades for
    +0.05R.
  - Every such variant also adds to the trial count.
- **Shadow running.** Pre-registration §7 put 1.0.0 in the scheduled scan as `shadow`. Shadow rows
  are never alerted (unless `SHADOW_ALERTS=true`) and never counted in the headline. They can keep
  accruing as a falsification record at no capital cost, but that is no longer a path to promotion.
  - The PASS → forward-shadow path in the task brief does not apply.
  - Whether to keep 1.0.0 in shadow (as an honest public record of a failed candidate) or retire it
    to `research` is the owner's call. Either way it must not be presented as a candidate edge.
- **For the product.** Per benchmark §7, decision 1, Crossline stays a research and alert product
  with an honest ledger. The live `macd_cross@2.2.0` is also negative after `cm1` on this holdout:
  −0.050R per trade, t −5.5, over 2020–2024. That agrees with the review's 2024–2026 finding, A1.
- **Bugs.** If a harness or evaluation bug is found later, both the original run (this one) and the
  corrected run are published together with the fix (§8.3). The trades CSV and full JSON are
  published so anyone can recompute every number here.

## Addendum (2026-10-15, after review; no number above changes)

- **Dataset identity.** The pre-registration did not list the holdout files' hashes. They existed
  before it was written: `holdout_klines/MANIFEST.json` (created 2026-10-15T02:22:10Z, its own
  SHA-256 `77f2645bab85df786985dd2e4a99106fd24dc739a0a81bf0335796ae16c25b71`) recorded every
  file's SHA-256 about an hour before the pre-registration file (03:22Z) and the run (03:30Z). The
  files the run read are those, as §1.2 checks. Future pre-registrations should pin the data
  hashes themselves and be timestamped outside the repository (a signed tag or a public post)
  before the run.
- **Ordering evidence is local.** `frozen_at`, file times and `trials.jsonl` can all be edited by
  whoever holds the repository, and the filesystem records no read times. The intended commit
  order is: the pre-registration, the lock and the strategy code unchanged in one commit, then
  this report and its data files in a later one.
- **Live settlement across kline gaps.** The replay slices the history the exit rule reads by
  position; the live evaluator first asked for it by time. With a hole in the exchange's klines
  (as in the 2022 archive gaps of §1.2), the live evaluator would have had too few history candles
  and left such a trade open. 154 of the 7,354 holdout trades have a gap inside their 498-candle
  history window. The evaluator now fetches the history by count when the time-anchored request
  comes back short (`src/services/db.py`, `fetch_settlement_klines`), so live and replay read the
  same candles. The strategy's code, parameters and hashes did not change, and neither does any
  result here: the replay was already positional.
- **Owner decision on shadow running.** `macd_cross_ema50d_oppexit@1.0.0` stays in the scheduled
  scan as `shadow`, as a public record of a failed candidate. Its forward signals keep accruing in
  the ledger at no capital cost. They are never alerted (unless `SHADOW_ALERTS=true`), never counted
  in the headline, and are not a path to promotion. Its status, parameters and `code_hash` do not
  change.
