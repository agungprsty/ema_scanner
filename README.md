# Crossline

Crossline (project `simple-trade`) scans Binance USDⓈ-M futures for RSI(14) crosses into oversold (28) or overbought (72) on one configurable timeframe (default 30m, so the job runs every 30 minutes). Each signal becomes a limit order at the signal candle's low or high, with a fixed take profit and stop loss. The app follows every order through fill and exit, and keeps a running win rate, fill rate and result in R.

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

The fill and hold windows are set in **hours**, then rounded up to whole candles of `TIMEFRAME`, so they mean the same time on every timeframe: 30m gives 24 and 48 candles, 1h gives 12 and 24, 15m gives 48 and 96, 2h gives 6 and 12, 1d gives 1 and 1. Each run replays the lifecycle from the stored levels and the closed candles, so the result is the same however often it runs.

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
| 30m (default) | :00 :30 (01:00, 01:30, 02:00, ...) |
| 1h | every hour at :00 |
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
| `TIMEFRAME` | `30m` | One of 1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d. Anything else stops the app at startup. |
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
| `API_SECRET_KEY` | empty | Optional machine/CI administrator access via `Authorization: Bearer`; at least 16 characters, empty disables it |
| `SESSION_TTL_HOURS` | `168` | Login session lifetime; absolute, never extended |
| `SESSION_COOKIE_SECURE` | `true` | `Secure` flag on the session cookie. Browsers accept it on `http://localhost`; set `false` only for plain HTTP on another host |
| `CORS_ORIGINS` | `*` | Comma list of browser origins (`scheme://host[:port]`) or `*`. Anything else stops the app at startup. |
| `LOG_LEVEL` | `INFO` | Root log level |
| `LOG_FORMAT` | `json` | `json` (one object per line) or `text` for local development |
| `RATE_LIMIT_ENABLED` | `true` | Per-IP sliding-window rate limits on `/api/*` |
| `RATE_LIMIT_PER_MINUTE` | `120` | Reads (any other `/api/*` request) |
| `RATE_LIMIT_WRITE_PER_MINUTE` | `20` | `POST`, `PUT`, `PATCH`, `DELETE` |
| `RATE_LIMIT_TRIGGER_PER_MINUTE` | `5` | `POST /api/cron/trigger` |
| `RATE_LIMIT_AUTH_PER_MINUTE` | `5` | `POST /api/auth/login` and `/api/auth/register` |
| `FORWARDED_ALLOW_IPS` | `172.16.0.0/12` in the image | Proxies whose `X-Forwarded-For`/`-Proto` uvicorn trusts; see [Behind nginx](#behind-nginx) |

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
| Frontend | Plain HTML and JavaScript without a bundler; Tailwind CSS 3 (prebuilt, committed) for the landing, login and register pages; TradingView Lightweight Charts 4.2 |
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
  pages/                       HTML pages: landing.html, login.html, register.html, dashboard.html, signal.html
  static/                      Frontend assets: base.css, app.css, tailwind.css (built from input.css),
                               common.js (shared helpers), app.js (dashboard), signal.js (signal page), auth.js
  api/                         HTTP foundation: envelopes, exceptions, logging, auth, security headers, rate limits
  main.py                      API routes and page handlers
scripts/create_admin.py        Create an account, reset its password or change its role
tests/                         Unit and integration tests
```

`src/static/tailwind.css` is committed, so the image and the deploy need no Node. After changing Tailwind classes in `src/pages/` or `src/static/`, rebuild it and commit the result:

```bash
npm install
npm run build:css      # or npm run watch:css while editing
```

## Running locally

```bash
cp .env.example .env        # fill in Binance and Telegram credentials
docker compose up --build -d
docker compose ps
```

Then open <http://localhost:7860> for the landing page.

- Visitor accounts can self-register at `/register` to view signals, charts, watchlist, and stats (read-only).
- To bootstrap an administrator account, run:
  ```bash
  docker compose exec app python -m scripts.create_admin --email admin@example.com --username admin
  ```
- Interactive API docs are at `/docs` and `/redoc`.

## API

### Responses

Every JSON response uses one envelope. `code` repeats the HTTP status, `status` is `success` for 2xx and `error` otherwise, and `meta.path` has no query string. The pages `/`, `/login`, `/register`, `/dashboard`, `/signals/{id}`, `/static/*`, `/docs`, `/redoc` and `/openapi.json` are not wrapped, but their errors are.

Success:

```json
{
  "code": 200,
  "status": "success",
  "message": "OK",
  "data": {"id": 42, "symbol": "SUIUSDT", "side": "LONG", "status": "OPEN"},
  "meta": {"request_id": "8ae00a92a43b4e0ea81fc21f4257d93b", "timestamp": "2026-10-21T00:45:40.081Z", "path": "/api/signals/42"}
}
```

Paginated (`page` from 1, default 1; `per_page` 1-200, default 50; `total_pages` is 0 when there is no data):

```json
{
  "code": 200,
  "status": "success",
  "message": "OK",
  "data": [{"id": 42, "symbol": "SUIUSDT"}],
  "meta": {
    "request_id": "8ae00a92a43b4e0ea81fc21f4257d93b", "timestamp": "2026-10-21T00:45:40.081Z", "path": "/api/signals",
    "total_data": 101, "total_pages": 3, "current_page": 1, "per_page": 50
  }
}
```

Error (4xx/5xx). `errors` lists field problems and is empty otherwise:

```json
{
  "code": 422,
  "status": "error",
  "message": "Validation failed",
  "data": null,
  "errors": [{"field": "per_page", "message": "Input should be less than or equal to 200"}],
  "meta": {"request_id": "8ae00a92a43b4e0ea81fc21f4257d93b", "timestamp": "2026-10-21T00:45:40.081Z", "path": "/api/signals"}
}
```

Unknown paths return 404 and wrong methods 405 in the same shape. Unexpected failures return 500 `Internal server error` with no exception details; the traceback is logged with the request id.

### Authentication

- **Session cookie**: Browser authentication uses an opaque session token in an `HttpOnly`, `SameSite=Lax`, `Secure` cookie (`st_session`), backed by PostgreSQL `user_sessions` with stdlib scrypt password hashing.
- **Roles**:
  - `user`: Read-only access to signals, charts, watchlist, summary stats, and cron status. Journal entries and write actions are restricted.
  - `administrator`: Full access including watchlist management, trade journaling, and manual cron triggers.
- **Endpoints**: `POST /api/auth/register` (`{email, username, password}`, always role `user`, 201), `POST /api/auth/login` (`{email, password}`), `POST /api/auth/logout` and `GET /api/auth/me`. Register and login set the cookie and return the user, never the token. Email is the only login identifier; passwords are 10-128 characters and may not equal the email or username.
- **Pages**: `/dashboard` and the signal page `/signals/{id}` need a session and redirect to `/login` (303) without one. `/login` and `/register` redirect to `/dashboard` when already signed in. `/api/health`, `/api/info`, register and login stay public, and logout answers 200 even without a session; every other `/api/*` route answers 401 without credentials.
- **CSRF**: cookie-authenticated `POST`/`PUT`/`PATCH`/`DELETE`, plus login, register and logout, are rejected with 403 when the `Origin` header names another host. Requests without `Origin` (curl, CI) pass.
- **Administrators**: public registration never grants `administrator`. Create the first one, reset a password or change a role with the CLI below. It prompts for the password twice, and updating an existing email revokes all of that user's sessions:
  ```bash
  docker compose -f docker-compose.prod.yml exec app python -m scripts.create_admin \
    --email you@example.com --username admin [--role administrator|user]
  ```
- **Machine/CI access**: `Authorization: Bearer <API_SECRET_KEY>` acts as an administrator on every route. When the key is empty, a placeholder or shorter than 16 characters, Bearer requests return 401 and the app logs a warning at startup.

### Request id

Send `X-Request-ID` (1-64 characters of `A-Z a-z 0-9 . _ -`) to set the id, otherwise the app generates one. It comes back in the `X-Request-ID` header and `meta.request_id`, and is on every log line written while the request runs.

### Rate limits

Each client IP gets a sliding one-minute window per bucket: `auth` for `POST /api/auth/login` and `/api/auth/register`, `trigger` for `POST /api/cron/trigger`, `write` for other `POST`/`PUT`/`PATCH`/`DELETE`, and `default` for any other `/api/*` request. `/api/health` and everything outside `/api/` are exempt. Windows live in Redis; while Redis is unreachable each worker counts on its own.

Limited responses carry `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` (seconds until a slot frees). Over the limit the app returns 429 `Too many requests` with the same headers plus `Retry-After`.

### Logging

Logs go to stdout, one JSON object per line with `timestamp`, `level`, `logger`, `message`, `request_id` and any extra fields (tracebacks in `exc_info`). Each request writes one access line from `src.api.middleware` with `method`, `path`, `status`, `duration_ms`, `client_ip` and `user_agent`; uvicorn's own access log is off. Authorization headers, bearer tokens, API keys, signatures, passwords, cookies and Telegram bot tokens are redacted, and request bodies are never logged. Set `LOG_FORMAT=text` for readable local logs.

### Signals

- `GET /api/signals` lists stored signals, newest first, paginated. Filters: `status` (comma-separated, e.g. `PENDING,OPEN`), `side` (`LONG`, `SHORT`), `symbol`, `timeframe`, `date`, `start_date`, `end_date` (WIB days, `YYYY-MM-DD`, end inclusive), plus `page` and `per_page`.
- `GET /api/signals/{signal_id}` returns one signal with its journal entry. Each signal has its levels, status, fill and exit prices and times, P/L, R, `fill_deadline` and `hold_deadline`.
- `GET /api/signals/{signal_id}/chart` returns the candles around one trade with three series on the same times: `rsi`, `ema` (EMA 50 of the close) and `volume_delta` (taker buy minus taker sell volume per candle, in the base asset). The window runs from 30 candles before the signal to now while it is active, or to 6 candles after the exit once it has closed. Closed charts are cached for 7 days. Returns 503 with `Retry-After` while the Binance request budget is used up.
- `GET /api/summary` takes the same filters as `/api/signals` (without paging) and returns signal counts per status, fill rate, win rate (target hits over closed trades; unfilled orders are left out), net P/L, total and average R, and max drawdown in R.

### Watchlist

- `GET /api/watchlist` lists symbols with their active state and notes.
- `POST /api/watchlist` adds a symbol: `{ "symbol": "SUIUSDT", "notes": "optional" }`.
- `PATCH /api/watchlist/{symbol}/toggle` sets `is_active`.
- `DELETE /api/watchlist/{symbol}` removes a symbol.

### Journal

- `GET /api/journal` lists journal entries with their signals, paginated. Filters: `status` (`TAKEN`, `SKIPPED`), `symbol`, `page`, `per_page`.
- `GET /api/journal/summary` returns taken and skipped counts and your R and P/L over all entries.
- `PUT /api/signals/{signal_id}/journal` saves an entry: `{ "status": "TAKEN", "entry_fill": 1.399, "exit_fill": 1.361, "notes": "optional" }`.
- `DELETE /api/signals/{signal_id}/journal` removes it.

### Operations

- `GET /api/info` returns the version, mode, timeframe and strategy settings.
- `GET /api/health` checks PostgreSQL, Redis and the scheduler. It always answers 200; `data.status` is `healthy` or `degraded`.
- `GET /api/cron/status` shows the scheduler state, last result and next run.
- `POST /api/cron/trigger` runs the job now: scan, store, evaluate and alert. Returns 409 while another run is going, and 503 when the run fails (for example, the database is not ready).

## Behind nginx

`nginx.conf.example` proxies to the app port published on `127.0.0.1`. Docker forwards those connections into the container from the compose network gateway, so without help every user looks like that one address: they share one rate-limit bucket, and the app never sees HTTPS, so it does not send HSTS.

The image runs uvicorn with `--proxy-headers`, which takes the client IP from `X-Forwarded-For` and the scheme from `X-Forwarded-Proto`, but only from peers in `FORWARDED_ALLOW_IPS` (uvicorn reads it from the environment). The image sets it to `172.16.0.0/12`; override it in the server `.env` only if the compose network gateway lies outside that range:

```bash
# Exact gateway of the deploy's compose network (project name comes from the folder, ema_scanner)
docker network inspect ema_scanner_default -f '{{(index .IPAM.Config 0).Gateway}}'   # e.g. 172.18.0.1
```

`172.16.0.0/12` covers Docker's default bridge pools and survives the network being recreated with a new subnet. It also trusts any other container on those networks to set the headers, so keep the app port bound to `127.0.0.1` (`APP_BIND_ADDR`) and never use `*`. nginx overwrites `X-Forwarded-For` with the real peer address, so clients cannot spoof it.

Clients of the old API must switch from `X-API-KEY` to `Authorization: Bearer`, from `limit`/`offset` to `page`/`per_page`, and read results under `data`.

## Upgrading to accounts (3.1.0)

Breaking: every `GET /api/*` except `/api/health` and `/api/info` now needs credentials, so external readers must send `Authorization: Bearer <API_SECRET_KEY>`. An empty or too short key now gives 401 instead of 503.

1. In the server `.env`, keep `SESSION_COOKIE_SECURE=true` (the site is HTTPS through nginx); `SESSION_TTL_HOURS` and `RATE_LIMIT_AUTH_PER_MINUTE` are optional.
2. Deploy as usual. Startup creates the `users` and `user_sessions` tables and leaves the existing tables alone.
3. Create the first administrator with `scripts.create_admin` (see [Authentication](#authentication)). Until then nobody can use admin actions in the UI; the scheduled job and Bearer requests keep working.
4. Log in at `/login` and check `/dashboard`. Browsers that stored the old API key drop it the first time they open the dashboard.

Rollback is the previous image; it ignores the new tables.

## Upgrading from the MACD version

The MACD version used the database `macd_scanner` and the tables `macd_signals`, `profitloss_history` and `trade_journal`. The RSI version uses the database `simple-trade` and the new tables `signals` and `signal_journal`, created at startup. Old signals and trades are dropped, not migrated; the watchlist is kept. No backup is taken.

The postgres image only creates `POSTGRES_DB` when its volume is first initialised, so an existing volume must be renamed by hand. Run this once on the server, in the deploy directory, **before** merging:

```bash
cd /var/www/ema_scanner
C="docker compose -f docker-compose.prod.yml"

# 1. Update .env: remove MACD_*, RISK_REWARD_RATIO, SWING_LOOKBACK_CANDLES, SIGNAL_EXPIRY_CANDLES,
#    VOLUME_THRESHOLD_USD; add the settings above (TIMEFRAME=1h, ...);
#    set POSTGRES_DB=simple-trade and point any DATABASE_URL at /simple-trade.
#    If FORWARDED_ALLOW_IPS is set, it must cover the compose gateway (see "Behind nginx"),
#    otherwise every visitor shares one rate-limit bucket.

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
