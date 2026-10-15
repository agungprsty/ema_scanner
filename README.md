# Crossline

Crossline scans Binance USDⓈ-M futures for MACD crossovers on the 2-hour chart. For each crossover it records an entry, a stop loss and a first target, follows the trade until one of those levels is hit, and keeps a running win rate and PnL.

It runs as a FastAPI service backed by PostgreSQL and Redis, with a small dashboard for running scans, reviewing signals and managing the watchlist.

## What it does

- **2H only.** Candle ingestion, signal detection and outcome tracking all use closed 2-hour candles. Forming candles can be included in a manual scan, but the scheduled scan only uses closed ones.
- **Trade levels per signal.**
  - Entry: the open of the candle after the cross, the first price you can trade once the cross is confirmed. The interpolated cross price is kept for reference.
  - Stop loss: 0.2% beyond the recent swing low (long) or swing high (short).
  - Target (TP1): 1.5 times the risk distance from entry.
- **Outcome tracking.** Open signals are checked against later candles. A trade closes at its target, at its stop, or as expired when neither is hit within `SIGNAL_EXPIRY_CANDLES` (default 48 candles, 4 days). A candle that touches both levels counts as a stop. Closed trades go into `profitloss_history` with exit price, PnL after fees and the result in R (multiples of the planned risk).
- **Performance in R.** The dashboard reports win rate, net result in R and percent after fees, and the largest drawdown in R.
- **Trade journal.** For any recorded signal you can note whether you took it, at which fills, and why. The journal compares your own results with the tracked results of the signals you took and skipped.
- **Scheduled scans.** A background job runs every 2 hours, just after each 2H candle closes. It evaluates open trades, then scans the active watchlist, re-checking the last few closed candles so crosses missed during a restart are caught up. New signals and closed trades can be sent to Telegram.
- **Forming candles.** A manual scan can include the candle that is still forming. Crosses on it are shown but not recorded or alerted, because they can disappear before the close.
- **Deduplication.** Redis makes sure a crossover is stored and alerted only once, even when manual and scheduled scans overlap.
- **Watchlist in the database.** Symbols can be added, removed, or paused without a redeploy.

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI, Uvicorn |
| Runtime | Python 3.12+ (`asyncio`) |
| Exchange | binance-futures-connector (UM futures) |
| Database | PostgreSQL 16 via SQLAlchemy 2.0 and asyncpg |
| Cache and dedup | Redis 7 (`redis.asyncio`) |
| Alerts | Telegram Bot API via httpx |
| Dashboard | Plain HTML, CSS and JavaScript; TradingView Lightweight Charts 4.2 |
| Deployment | Docker Compose |

## Project layout

```
src/
  config/settings.py         MACD, risk and timeframe settings
  data_feed/binance_client.py  Binance futures client (HMAC/RSA)
  data_feed/ohlcv.py         2H kline ingestion and chart payloads
  strategy/macd.py           Crossover detection, stop loss and target calculation
  services/db.py             Schema, signal storage, outcome evaluation
  services/redis_client.py   Redis client and crossover deduplication
  services/cron_scheduler.py Scheduled 2H scan and evaluation
  services/telegram.py       Telegram alerts
  static/                    Dashboard (index.html, app.js)
  main.py                    API routes
tests/                       Unit and integration tests
```

## Running locally

```bash
cp .env.example .env        # set API_SECRET_KEY; Telegram is optional, Binance keys are not needed
docker compose up --build -d
docker compose ps
```

Then open <http://localhost:7860>. Over plain http, set `COOKIE_SECURE=false` or the dashboard cannot sign in.

The signal ledger is public: signals, closed trades, performance, summary, charts, the watchlist, health and cron status need no key. Everything else is owner-only: the journal (read and write), watchlist changes, scans, evaluations, the cron trigger and `POST /api/profitloss-history`. Scripts send `API_SECRET_KEY` in the `X-API-KEY` header (or `Authorization: Bearer`). The dashboard signs in through a small dialog: `POST /api/auth/login` checks the key and sets an HttpOnly, Secure, SameSite=Strict cookie holding an HMAC-signed token that expires after `SESSION_TTL_SECONDS` (12 hours by default); the key itself is never stored in the browser. Cookie-authenticated writes must also send `X-Requested-With: crossline`. `POST /api/auth/logout` clears the cookie and `GET /api/auth/session` reports `{authenticated}`. Tokens are stateless, so rotating `API_SECRET_KEY` signs every session out. Without a valid `API_SECRET_KEY` the owner endpoints answer 503.

`/docs`, `/redoc` and `/openapi.json` are off unless `ENABLE_API_DOCS=true`. Binance credentials are never loaded: every call is public market data, and `BINANCE_AUTH_ENABLED=true` is required before the client reads any key. Security headers (HSTS, CSP, Permissions-Policy) are set in nginx; see `nginx.conf.example` and mirror it on the server.

## API

### Scanning and charts

- `POST /api/scan` (alias `POST /api/macd-scan`) runs a manual scan over the active watchlist. Query: `lookback` (default 5), `include_current` (default false), `save_db` (default false), `send_telegram` (default false). By default it is a preview: nothing is stored, alerted or evaluated. Signals it stores are labelled `source=manual` and never count in the track record.
- `GET /api/klines?symbol=BTCUSDT` returns 2H candles and the MACD series for charting.
- `GET /api/signals/{signal_id}` returns one stored signal with its journal entry.
- `GET /api/signals/{signal_id}/candles` returns the candles around one signal's trade: from 30 candles before the cross to now while it is open, or to 6 candles after the exit once it has closed. Closed windows are cached for 7 days.
- `POST /api/evaluate` checks open signals against later price action and marks them `TP1_HIT`, `SL_HIT` or `EXPIRED`.

### Watchlist

- `GET /api/watchlist` lists symbols with their active state and notes.
- `POST /api/watchlist` adds a symbol: `{ "symbol": "SUIUSDT", "notes": "optional" }`.
- `PATCH /api/watchlist/{symbol}/toggle` sets `is_active`.
- `DELETE /api/watchlist/{symbol}` removes a symbol.

### History and performance

- `GET /api/performance` is the track record of one strategy version. Query: `strategy_version` (default: the running version), `symbol`, `side`. Only eligible trades count: on-time signals from the scheduled scan, entered at the next candle open, with the outcome the evaluator booked (`booked_by = evaluator`). Net R uses cost model `cm1`: 0.1% round-trip fee, one-way slippage BTC/ETH 2, SOL 8, XRP 10, ZEC 20, NEAR 25 and 10 bps elsewhere, stops at 1.5x. Overriding `SLIPPAGE_BPS_*` or `STOP_SLIPPAGE_MULTIPLIER` books under a derived `cmx-<hash>` label instead. On the 2026-10-15 production snapshot the restated v2.2.0 headline is n=6, 1 target and 5 stops, total net −4.72R. It returns `n_closed`, wins, losses and expired; `win_rate` (target hits over all closed trades, expired included) with a Wilson 95% interval; `mean_net_r` with a Student t 95% interval (null below 2 trades); `total_net_r`, `mean_gross_r`, `mean_cost_r`, net `profit_factor`; `max_drawdown_r` on net R in exit-time order; `break_even_win_rate` = (1 + mean_cost_r) / (target_rr + 1); `open_count`; `excluded` signal counts by reason (`legacy`, `catchup`, `manual`, `other_version`, `other`), which with the counted signals add up to every stored signal; a `sample_size_warning` below 30 trades; `min_sample_for_totals` (100: the dashboard hides summed totals below it); first and last exit time; and `definitions` stating each formula. Rates are percents.
- `GET /api/summary` is kept for older clients. Its outcome fields (target/stop/expired counts, `winrate`, `closed_trades`, `total_r`, `avg_r`, `max_drawdown_r`) use the same eligible trades and win rate definition as `/api/performance`; signal counts cover every recorded signal. `target_rr` is the planned reward-to-risk (it replaces `avg_rr`). `total_net_pnl` sums percents across different stop distances and is deprecated.
- `GET /api/signals` lists stored signals. Filters: `symbol`, `direction` or `side`, `timeframe` (default `2h`), `result` or `win_status` (`PROFIT`, `LOSS`, `PENDING`), `date`, `start_date`, `end_date`, `tz_offset_minutes`, `limit`, `offset`. Dates are days in `YYYY-MM-DD` form, matched on the cross candle: UTC days by default, or local days `tz_offset_minutes` east of UTC (the dashboard sends 420 for WIB). `end_date` includes its whole day, and any other format is rejected with 422. Signals are listed newest cross first. In the summary, `eligible_closed_count`, `eligible_net_r` and `excluded_count` follow `/api/performance`; `total_r` and `total_pnl_percent` are deprecated as-booked sums over every row.
- `GET /api/profitloss-history` lists closed trades with a summary. `eligible_closed_count`, `eligible_win_count`, `eligible_win_rate` and `eligible_net_r` count only the evaluator's trades for eligible signals of `strategy_version`, as `/api/performance` does. `winrate`, `total_pnl_percent`, `total_r` and `avg_r` are deprecated as-booked figures over every listed row, legacy and hand-entered rows included. Each row carries `booked_by`: `evaluator`, `legacy` (fee-free, pre-v2.2) or `manual`. Same filters, with `result` one of `PROFIT`, `LOSS` or `EXPIRED`.
- `POST /api/profitloss-history` (owner) records a hand-entered trade (`result` one of `PROFIT`, `LOSS`, `EXPIRED`). It is stored as `booked_by = manual`, never restated under the cost model and never counted. A `signal_id` that names a stored signal is refused with 409, since the evaluator books that signal's outcome.
- `POST /api/profitloss-history/evaluate` evaluates open signals and writes the results to `profitloss_history`.

### Journal

- `GET /api/journal` lists journal entries with their signals. Filters: `status` (`TAKEN`, `SKIPPED`), `symbol`, `limit`, `offset`.
- `PUT /api/signals/{signal_id}/journal` saves an entry: `{ "status": "TAKEN", "entry_fill": 1.399, "exit_fill": 1.361, "notes": "optional" }`.
- `DELETE /api/signals/{signal_id}/journal` removes it.

### Operations

- `GET /api/cron/status` shows the scheduler state, next run and last successful run (error text only when signed in).
- `POST /api/cron/trigger` runs the scheduled job now. Manual runs are not logged and do not count as scheduled runs.
- Every scheduled run writes a row to `cron_runs` (`slot_ms`, `started_at`, `finished_at`, `status` `ok`/`error`/`skipped`, `signals_count`, `evaluated_count`, `error`).
- `GET /api/health` returns only `status`, `db`, `redis`, `cron_fresh` and `version`. `status` is `degraded` when Redis is down or, with `CRON_ENABLED`, the last successful scheduled run is older than `CRON_STALE_SECONDS` (default 2h05m; read from `cron_runs`, else from memory, and timed from startup before the first success). With `REQUIRE_CRON=true` (set it in production) it is also `degraded` while `CRON_ENABLED` is off. It is `down` with HTTP 503 only when PostgreSQL is unreachable or does not answer within `HEALTH_PROBE_TIMEOUT_SECONDS` (default 2s; every check runs at once under that limit), so the endpoint answers inside the Docker healthcheck's 5s and fails it on the database alone.
- `HEALTHCHECK_PING_URL` (optional) is requested after every successful scheduled run, as a dead-man switch for an external monitor such as Healthchecks.io.

## Strategy

1. MACD with fast EMA 12, slow EMA 26 and signal EMA 9, computed on 2H candles.
2. A long signal fires when the MACD line crosses above the signal line; a short signal when it crosses below.
3. Entry, stop loss and target are set as described above. The risk/reward ratio is configurable through `RISK_REWARD_RATIO`.
4. A signal stays open until a later 2H candle's high or low reaches the target or the stop, or until it expires.
5. PnL subtracts `ROUND_TRIP_FEE_PERCENT` (default 0.1%, taker fees for opening and closing).

MACD is computed from the last `MACD_CANDLES` candles (default 500) so its values match charting platforms closely.
