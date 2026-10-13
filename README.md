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
cp .env.example .env        # fill in Binance and Telegram credentials
docker compose up --build -d
docker compose ps
```

Then open <http://localhost:7860>.

Write endpoints (scan, evaluate, watchlist changes) require the `X-API-KEY` header to match `API_SECRET_KEY`. The dashboard asks for the key the first time it is needed and remembers it in the browser.

## API

### Scanning and charts

- `GET /api/macd-scan` runs a scan over the active watchlist. Query: `lookback` (default 5), `include_current` (default false), `send_telegram` (default true).
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

- `GET /api/summary` returns win rate, target/stop/expired counts, open trades, long/short counts, net PnL, total and average R, and max drawdown in R.
- `GET /api/signals` lists stored signals. Filters: `symbol`, `direction` or `side`, `timeframe` (default `2h`), `result` or `win_status` (`PROFIT`, `LOSS`, `PENDING`), `date`, `start_date`, `end_date`, `limit`, `offset`.
- `GET /api/profitloss-history` lists closed trades with a summary (`total_trades`, `profit_count`, `loss_count`, `expired_count`, `winrate`, `total_pnl_percent`, `total_r`, `avg_r`). Same filters, with `result` one of `PROFIT`, `LOSS` or `EXPIRED`.
- `POST /api/profitloss-history/evaluate` evaluates open signals and writes the results to `profitloss_history`.

### Journal

- `GET /api/journal` lists journal entries with their signals. Filters: `status` (`TAKEN`, `SKIPPED`), `symbol`, `limit`, `offset`.
- `PUT /api/signals/{signal_id}/journal` saves an entry: `{ "status": "TAKEN", "entry_fill": 1.399, "exit_fill": 1.361, "notes": "optional" }`.
- `DELETE /api/signals/{signal_id}/journal` removes it.

### Operations

- `GET /api/cron/status` shows the scheduler state and next run.
- `POST /api/cron/trigger` runs the scheduled job now.
- `GET /api/health` checks PostgreSQL, Redis and the scheduler.

## Strategy

1. MACD with fast EMA 12, slow EMA 26 and signal EMA 9, computed on 2H candles.
2. A long signal fires when the MACD line crosses above the signal line; a short signal when it crosses below.
3. Entry, stop loss and target are set as described above. The risk/reward ratio is configurable through `RISK_REWARD_RATIO`.
4. A signal stays open until a later 2H candle's high or low reaches the target or the stop, or until it expires.
5. PnL subtracts `ROUND_TRIP_FEE_PERCENT` (default 0.1%, taker fees for opening and closing).

MACD is computed from the last `MACD_CANDLES` candles (default 500) so its values match charting platforms closely.
