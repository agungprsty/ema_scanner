# Crossline

Crossline (project `simple-trade`) scans Binance USDⓈ-M futures for RSI(14) crosses into oversold (28) or overbought (72) on one configurable timeframe (default 1h). Each signal becomes a limit order at the signal candle's low or high, with a fixed take profit and stop loss. The app follows every order through fill and exit, and keeps a running win rate, fill rate and result in R.

It runs as a FastAPI service backed by PostgreSQL and Redis, with a small dashboard for active and closed signals, the trade journal and the watchlist.

## Strategy

1. RSI(14), Wilder smoothing, computed on closed candles of `TIMEFRAME` from the last `SCAN_CANDLES` candles (default 500), so values match charting platforms.
2. **LONG** when RSI crosses down to the oversold level: previous RSI > 28 and current RSI <= 28. **SHORT** when RSI crosses up to the overbought level: previous RSI < 72 and current RSI >= 72.
3. Levels, all fixed at signal time:

| | LONG | SHORT |
|---|---|---|
| Limit entry `E` | signal candle low | signal candle high |
| Take profit (`TP_PERCENT=2`) | `E × 1.02` | `E × 0.98` |
| Stop loss (`SL_PERCENT=1`) | `E × 0.99` | `E × 1.01` |

Only closed candles count, for both detection and tracking. A candle that is still forming is never used.

## Trade lifecycle

`PENDING → OPEN → TP_HIT | SL_HIT | EXPIRED`, or `PENDING → NOT_FILLED`.

- **PENDING:** the limit waits for a fill during the fill window (`FILL_WINDOW_HOURS`, default 12 hours), counted from the candle after the signal candle. A LONG fills when a candle's low reaches `E`, a SHORT when its high reaches `E`. The signal candle itself never fills.
- **NOT_FILLED:** no fill within the window. The order is cancelled and there is no P/L.
- **OPEN:** filled. The trade is held for at most `MAX_HOLD_HOURS` (default 24 hours) from the fill candle.
- **TP_HIT / SL_HIT:** a candle reaches the target or the stop. A candle that opens past a level exits at its open. A candle that touches both levels counts as a stop. On the fill candle, the target only counts when the candle opened at or past `E`, since the candle data cannot show whether the target came before the fill.
- **EXPIRED:** neither level is hit within the hold window. The trade closes at the last candle's close.

P/L is `(exit − fill) / fill × 100` (sign flipped for SHORT) minus `ROUND_TRIP_FEE_PERCENT`. R is P/L divided by the planned risk (`SL_PERCENT`), so a target hit is about +1.9R and a stop about −1.1R after fees.

The fill and hold windows are set in **hours**, then rounded up to whole candles of `TIMEFRAME`, so they mean the same time on every timeframe: 1h gives 12 and 24 candles, 15m gives 48 and 96, 2h gives 6 and 12, 1d gives 1 and 1. Each run replays the lifecycle from the stored levels and the closed candles, so the result is the same however often it runs.

Each signal is stored once per symbol, side, timeframe and signal candle. Status changes are written only forward, and each change is alerted only once.

## Schedule

A background job runs right after each `TIMEFRAME` candle closes, plus `CRON_SAFETY_DELAY_SECONDS` (default 15 s). Each run:

1. scans the active watchlist (up to `MAX_SYMBOLS`) and re-checks the last `CRON_LOOKBACK_CANDLES` closed candles, so signals missed during a restart are caught up;
2. stores new signals;
3. evaluates every PENDING and OPEN signal, including the new ones;
4. sends Telegram messages: one with every signal not alerted yet (including ones from a run that failed or restarted before alerting) in its current state, and, when `TELEGRAM_OUTCOME_ALERTS` is on, one with target hits, stops, expiries and unfilled orders.

Run times in WIB (UTC+7). Candle boundaries follow UTC, so 4h and longer timeframes land on these WIB hours:

| TF | Runs |
|---|---|
| 1m / 3m / 5m | every 1 / 3 / 5 minutes |
| 15m | :00 :15 :30 :45 |
| 30m | :00 :30 |
| 1h (default) | every hour at :00 |
| 2h | odd hours |
| 4h | 03 07 11 15 19 23 |
| 6h | 01 07 13 19 |
| 8h | 07 15 23 |
| 12h | 07 19 |
| 1d | 07:00 |

**1m and 3m are allowed but heavy.** Every run spends one klines request per watchlist symbol plus at least one per active signal, close to Binance's request weight limit. A run that overruns more than `CRON_LOOKBACK_CANDLES` boundaries loses signals; the log warns with the number of skipped boundaries.

## Settings

All settings come from `.env` (see `.env.example`).

| Setting | Default | Meaning |
|---|---|---|
| `TIMEFRAME` | `1h` | One of 1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d. Anything else stops the app at startup. |
| `RSI_PERIOD` | `14` | RSI length |
| `RSI_OVERSOLD` | `28` | LONG trigger level |
| `RSI_OVERBOUGHT` | `72` | SHORT trigger level |
| `TP_PERCENT` | `2` | Take profit, percent from the limit entry |
| `SL_PERCENT` | `1` | Stop loss, percent from the limit entry; also the planned risk for R |
| `FILL_WINDOW_HOURS` | `12` | How long the limit waits for a fill |
| `MAX_HOLD_HOURS` | `24` | How long a filled trade is held before it expires |
| `ROUND_TRIP_FEE_PERCENT` | `0.1` | Fees for opening plus closing, subtracted from P/L |
| `SCAN_CANDLES` | `500` | Candles fetched per symbol for RSI |
| `MAX_SYMBOLS` | `50` | Watchlist symbols scanned per run |
| `SCAN_CONCURRENCY` | `10` | Parallel klines requests |
| `CRON_ENABLED` | `true` | Run the scheduled job |
| `CRON_SAFETY_DELAY_SECONDS` | `15` | Delay after each candle close |
| `CRON_LOOKBACK_CANDLES` | `3` | Closed candles re-checked per run |
| `TELEGRAM_OUTCOME_ALERTS` | `true` | Send trade updates to Telegram |

Server, database, Redis, Binance and Telegram credentials are documented in `.env.example`.

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI, Uvicorn |
| Runtime | Python 3.12+ (`asyncio`) |
| Exchange | binance-futures-connector (UM futures) |
| Database | PostgreSQL 16 via SQLAlchemy 2.0 and asyncpg |
| Cache and locks | Redis 7 (`redis.asyncio`) |
| Alerts | Telegram Bot API via httpx |
| Dashboard | Plain HTML, CSS and JavaScript; TradingView Lightweight Charts 4.2 |
| Deployment | Docker Compose |

## Project layout

```
src/
  config/settings.py           Timeframe, RSI, TP/SL and window settings
  data_feed/binance_client.py  Binance futures client (HMAC/RSA)
  data_feed/ohlcv.py           Kline fetching
  strategy/rsi.py              RSI, signal detection and trade levels
  strategy/outcome.py          Fill/exit lifecycle and P/L
  services/db.py               Schema, signal storage, evaluation, journal, watchlist
  services/redis_client.py     Redis cache and scheduler locks
  services/cron_scheduler.py   Per-candle scheduled job
  services/alerts.py           Telegram message text
  services/telegram.py         Telegram transport
  static/                      Dashboard (index.html, app.js)
  main.py                      API routes
tests/                         Unit and integration tests
```

## Running locally

```bash
cp .env.example .env        # fill in Binance and Telegram credentials
docker compose up --build -d
docker compose ps
```

Then open <http://localhost:7860>.

Write endpoints (cron trigger, watchlist and journal changes) require the `X-API-KEY` header to match `API_SECRET_KEY`. The dashboard asks for the key the first time it is needed and remembers it in the browser.

## API

### Signals

- `GET /api/signals` lists stored signals, newest first, with a summary for the same filters. Filters: `status` (comma-separated, e.g. `PENDING,OPEN`), `side` (`LONG`, `SHORT`), `symbol`, `timeframe`, `date`, `start_date`, `end_date` (WIB days, `YYYY-MM-DD`, end inclusive), `limit` (1-100, default 50), `offset`.
- `GET /api/signals/{signal_id}` returns one signal with its journal entry. Each signal has its levels, status, fill and exit prices and times, P/L, R, `fill_deadline` and `hold_deadline`.
- `GET /api/signals/{signal_id}/chart` returns the candles and RSI series around one trade: from 30 candles before the signal to now while it is active, or to 6 candles after the exit once it has closed. Closed charts are cached for 7 days.
- `GET /api/summary` returns signal counts per status, fill rate, win rate (target hits over closed trades; unfilled orders are left out), net P/L, total and average R, and max drawdown in R.

### Watchlist

- `GET /api/watchlist` lists symbols with their active state and notes.
- `POST /api/watchlist` adds a symbol: `{ "symbol": "SUIUSDT", "notes": "optional" }`.
- `PATCH /api/watchlist/{symbol}/toggle` sets `is_active`.
- `DELETE /api/watchlist/{symbol}` removes a symbol.

### Journal

- `GET /api/journal` lists journal entries with their signals. Filters: `status` (`TAKEN`, `SKIPPED`), `symbol`, `limit`, `offset`.
- `PUT /api/signals/{signal_id}/journal` saves an entry: `{ "status": "TAKEN", "entry_fill": 1.399, "exit_fill": 1.361, "notes": "optional" }`.
- `DELETE /api/signals/{signal_id}/journal` removes it.

### Operations

- `GET /api/info` returns the version, mode, timeframe and strategy settings.
- `GET /api/health` checks PostgreSQL, Redis and the scheduler. `status` is `healthy` or `degraded`.
- `GET /api/cron/status` shows the scheduler state, last result and next run.
- `POST /api/cron/trigger` runs the job now: scan, store, evaluate and alert. Returns 409 while another run is going, and 503 when the run fails (for example, the database is not ready).

## Upgrading from the MACD version

The MACD version used the database `macd_scanner` and the tables `macd_signals`, `profitloss_history` and `trade_journal`. The RSI version uses the database `simple-trade` and the new tables `signals` and `signal_journal`, created at startup. Old signals and trades are dropped, not migrated; the watchlist is kept. No backup is taken.

The postgres image only creates `POSTGRES_DB` when its volume is first initialised, so an existing volume must be renamed by hand. Run this once on the server, in the deploy directory, **before** merging:

```bash
cd /var/www/ema_scanner
C="docker compose -f docker-compose.prod.yml"

# 1. Update .env: remove MACD_*, RISK_REWARD_RATIO, SWING_LOOKBACK_CANDLES, SIGNAL_EXPIRY_CANDLES,
#    VOLUME_THRESHOLD_USD; add the settings above (TIMEFRAME=1h, ...);
#    set POSTGRES_DB=simple-trade and point any DATABASE_URL at /simple-trade.

# 2. Stop the app so nothing is connected to the database
$C stop app

# 3. Rename the database (connect to "postgres", a database cannot be renamed while in use)
$C exec postgres psql -U postgres -d postgres -c 'ALTER DATABASE macd_scanner RENAME TO "simple-trade";'

# 4. Drop the MACD tables (watchlist_symbols stays)
$C exec postgres psql -U postgres -d simple-trade -c 'DROP TABLE IF EXISTS trade_journal, profitloss_history, macd_signals CASCADE;'

# 5. Clear Redis (only caches, locks and old dedup keys)
$C exec redis redis-cli FLUSHDB
```

Then merge. The deploy runs `up -d --remove-orphans`, which replaces the `macd_scanner_*` containers with `simple-trade-*`; the volumes stay. The deploy fails if `/api/health` does not report `healthy` within two minutes. Check afterwards that `/api/info` shows the expected timeframe and `/api/cron/status` shows the next run.
