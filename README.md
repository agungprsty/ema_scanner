# Crossline

Crossline scans Binance USDⓈ-M futures for MACD crossovers on the 2-hour chart. For each crossover it records an entry, a stop loss and a first target, follows the trade until one of those levels is hit, and keeps a running win rate and PnL.

It runs as a FastAPI service backed by PostgreSQL and Redis, with public research pages (every signal, a shareable page per signal, an honest track record per strategy version, status and methodology) and owner tools at `/ops` for scans, the watchlist and a private journal.

## What it does

- **2H only.** Candle ingestion, signal detection and outcome tracking all use closed 2-hour candles. Forming candles can be included in a manual scan, but the scheduled scan only uses closed ones.
- **Trade levels per signal.**
  - Entry: the open of the candle after the cross, the first price you can trade once the cross is confirmed. The interpolated cross price is kept for reference.
  - Stop loss: 0.2% beyond the recent swing low (long) or swing high (short).
  - Target (TP1): 1.5 times the risk distance from entry.
- **Outcome tracking.** Open signals are checked against later candles. A trade closes at its target, at its stop, or as expired when neither is hit within `SIGNAL_EXPIRY_CANDLES` (default 48 candles, 4 days). A candle that touches both levels counts as a stop. Closed trades go into `profitloss_history` with exit price, PnL after fees and the result in R (multiples of the planned risk).
- **Performance in R.** The pages report mean net R with its 95% interval and sample size, win rate against break-even, and the largest drawdown in R, per strategy version.
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
| Public pages | Jinja2 server-rendered shells with Open Graph tags; vanilla ES modules, no build step; TradingView Lightweight Charts 4.2 |
| Deployment | Docker Compose |

## Project layout

```
src/
  config/settings.py         MACD, risk and timeframe settings
  data_feed/binance_client.py  Binance futures client (HMAC/RSA)
  data_feed/ohlcv.py         2H kline ingestion and chart payloads
  strategy/macd.py           Crossover detection, stop loss and target calculation
  strategy/base.py           Strategy interface (detect on closed candles, resolve exits, hashes)
  strategy/context.py        As-of market context: closed candles per timeframe only
  strategy/registry.py       Registered strategy versions and the frozen-version rule
  strategy/macd_cross.py     macd_cross@2.2.0, the production rules on that interface
  strategy/versions.lock.json  Frozen params_hash / code_hash of every frozen version
  backtest/                  Replay harness, CI gate and the research/trials.jsonl log
  ledger/                    Hash chain, verifier and `python -m src.ledger verify`
  services/db.py             Schema, signal storage, outcome evaluation, ledger writes
  services/ledger.py         Ledger reads: head, per-signal events, exports, server-side verify
  services/redis_client.py   Redis client and crossover deduplication
  services/cron_scheduler.py Scheduled 2H scan and evaluation
  services/telegram.py       Telegram alerts
  web/pages.py               Public pages (Jinja routes, titles and Open Graph tags)
  web/views.py               What the pages say: outcome banners, signal facts, MAE/MFE, research docs
  templates/                 Jinja templates, one per page, extending base.html
  static/css/base.css        Styles (IBM Plex, dark tokens, mobile first)
  static/js/                 ES modules: core, format, cards, chart, kpi, events, and pages/<page>.js entries
  main.py                    API routes
docs/changelog.json          Versioned changelog served at /changelog and /api/changelog
tests/                       Unit and integration tests
```

## Running locally

```bash
cp .env.example .env        # set API_SECRET_KEY; Telegram is optional, Binance keys are not needed
docker compose up --build -d
docker compose ps
```

Then open <http://localhost:7860>. Over plain http, set `COOKIE_SECURE=false` or `/ops` cannot sign in.

### Pages

| Route | What it shows |
|---|---|
| `/` | Research badge, KPI strip per live and shadow strategy version (n, mean net R ± 95% CI, win rate with its sample vs break-even, insufficient-sample captions), open signals as cards, the last 10 ledger events, last/next scheduled scan |
| `/signals` | Every signal, filtered by strategy version, symbol, side, status and source; the filters live in the URL |
| `/s/{id}` | Shareable signal page: `<title>`/`og:title` `{SYMBOL} {SIDE} · {±R or OPEN} · Crossline {strategy}@{version}` for wins and losses alike, a neutral outcome banner, the chart with an *At publish / After close* toggle, the facts (version, params hash, source, entry basis, times, levels, hold, gross/cost/net R, MAE/MFE from the persisted window), the ledger timeline with hashes, recent signals on the symbol, raw JSON links. Unknown ids get a 404 page |
| `/track-record` | Version selector, the baseline backtest card and holdout reports, KPIs, equity curve and underwater pane in R, R histogram, breakdowns per side/symbol/month (groups under 20 trades greyed *not meaningful*), the full ledger with CSV/JSONL downloads, and a chart of every closed signal |
| `/status` | Health, last successful scheduled run, on-schedule ratio over 7 days, the last 50 runs, the ledger head, daily heads and server-side verification |
| `/methodology`, `/changelog` | Definitions (R, costs and the cm1 table, stop-first, exits per strategy, win rate, intervals, eligibility, sample thresholds, pre-registration); the versioned changelog |
| `/ops` | Owner tools behind the cookie sign-in: scan preview, evaluate, watchlist, journal |
| `/research/{file}` | The pre-registration and holdout documents under `docs/research/`, as plain text |

Pages carry no inline script (each loads one module from `/static/js/pages/`), so they run under the CSP in `nginx.conf.example`. `/history` redirects to `/signals`. Set `PUBLIC_BASE_URL` to the site's origin so `og:url` and canonical links are absolute behind the proxy. Page modules and the stylesheet are served with `Cache-Control: no-cache` so a deploy never mixes old and new modules.

The signal ledger is public: signals, closed trades, performance, summary, charts, the watchlist, health and cron status need no key. Everything else is owner-only: the journal (read and write), watchlist changes, scans, evaluations, the cron trigger and `POST /api/profitloss-history`. Scripts send `API_SECRET_KEY` in the `X-API-KEY` header (or `Authorization: Bearer`). The `/ops` page signs in with a form: `POST /api/auth/login` checks the key and sets an HttpOnly, Secure, SameSite=Strict cookie holding an HMAC-signed token that expires after `SESSION_TTL_SECONDS` (12 hours by default); the key itself is never stored in the browser. Cookie-authenticated writes must also send `X-Requested-With: crossline`. `POST /api/auth/logout` clears the cookie and `GET /api/auth/session` reports `{authenticated}`. Tokens are stateless, so rotating `API_SECRET_KEY` signs every session out. Without a valid `API_SECRET_KEY` the owner endpoints answer 503.

`/docs`, `/redoc` and `/openapi.json` are off unless `ENABLE_API_DOCS=true`. Binance credentials are never loaded: every call is public market data, and `BINANCE_AUTH_ENABLED=true` is required before the client reads any key. Security headers (HSTS, CSP, Permissions-Policy) are set in nginx; see `nginx.conf.example` and mirror it on the server.

## API

### Scanning and charts

- `POST /api/scan` (alias `POST /api/macd-scan`) runs a manual scan over the active watchlist. Query: `lookback` (default 5), `include_current` (default false), `save_db` (default false), `send_telegram` (default false). By default it is a preview: nothing is stored, alerted or evaluated. Signals it stores are labelled `source=manual` and never count in the track record.
- `GET /api/klines?symbol=BTCUSDT` returns 2H candles and the MACD series for charting.
- `GET /api/signals/{signal_id}` returns one stored signal with its journal entry (owner only), `strategy_status` (live/shadow), `expires_at` while open and its `permalink`.
- `GET /api/signals/{signal_id}/candles` returns the candles around one signal's trade. A closed signal is charted from the window persisted when it resolved (`persisted: true`, with its `sha256`; see the signal ledger below), so its chart never changes; a candle still forming at resolution carries `partial: true`. `?view=publish` instead returns the candles that had closed when the signal was detected (the 30 before the cross, the cross candle, and any that closed before a late detection), final and cached. Open signals, and signals closed before windows were persisted, run from 30 candles before the cross to now while open, or to 6 candles after the exit once closed, with closed windows cached for 7 days.
- `POST /api/evaluate` checks open signals against later price action and marks them `TP1_HIT`, `SL_HIT` or `EXPIRED`.

### Watchlist

- `GET /api/watchlist` lists symbols with their active state and notes.
- `POST /api/watchlist` adds a symbol: `{ "symbol": "SUIUSDT", "notes": "optional" }`.
- `PATCH /api/watchlist/{symbol}/toggle` sets `is_active`.
- `DELETE /api/watchlist/{symbol}` removes a symbol.

### History and performance

- `GET /api/performance` is the track record of one strategy version. Query: `strategy_key` (default `macd_cross`), `strategy_version` (default: the running version, or the registered version of another key), `symbol`, `side`. A strategy without a fixed target (the shadow `macd_cross_ema50d_oppexit`) reports `win_rule: net_r_positive`, `target_rr: null` and no break-even win rate; its wins are trades closed with a net R above zero. Only eligible trades count: on-time signals from the scheduled scan, entered at the next candle open, with the outcome the evaluator booked (`booked_by = evaluator`). Net R uses cost model `cm1`: 0.1% round-trip fee, one-way slippage BTC/ETH 2, SOL 8, XRP 10, ZEC 20, NEAR 25 and 10 bps elsewhere, stops at 1.5x. Overriding `SLIPPAGE_BPS_*` or `STOP_SLIPPAGE_MULTIPLIER` books under a derived `cmx-<hash>` label instead. On the 2026-10-15 production snapshot the restated v2.2.0 headline is n=6, 1 target and 5 stops, total net −4.72R. It returns `n_closed`, wins, losses and expired; `win_rate` (target hits over all closed trades, expired included) with a Wilson 95% interval; `mean_net_r` with a Student t 95% interval (null below 2 trades); `total_net_r`, `mean_gross_r`, `mean_cost_r`, net `profit_factor`; `max_drawdown_r` on net R in exit-time order; `break_even_win_rate` = (1 + mean_cost_r) / (target_rr + 1); `open_count`; `excluded` signal counts by reason (`legacy`, `catchup`, `manual`, `other_version`, `other`), which with the counted signals add up to every stored signal; a `sample_size_warning` below 30 trades; `min_sample_for_totals` (100: the pages hide summed totals below it); first and last exit time; and `definitions` stating each formula. Rates are percents.
- `GET /api/track-record?strategy_key=&strategy_version=` returns that `performance`, the `equity` curve (cumulative net R and drawdown, one point per exit instant, its lowest point equal to `max_drawdown_r`), the net R `histogram` (0.25R bins) and `breakdowns` per side, symbol and exit month (n, wins, mean net R with 95% CI, win rate, total, `meaningful` from 20 trades).
- `GET /api/versions` lists every strategy version: registered ones with params, hashes, status, frozen time and lock notes, versions found only in stored signals (`retired`), signal counts per source, and the holdout result (`verdict`, link) when `docs/research/holdout-result-{key}-{version}.md` exists.
- `GET /api/changelog` serves `docs/changelog.json`.
- `GET /api/summary` is kept for older clients. Its outcome fields (target/stop/expired counts, `winrate`, `closed_trades`, `total_r`, `avg_r`, `max_drawdown_r`) use the same eligible trades and win rate definition as `/api/performance`; signal counts cover every recorded signal. `target_rr` is the planned reward-to-risk (it replaces `avg_rr`). `total_net_pnl` sums percents across different stop distances and is deprecated.
- `GET /api/signals` lists stored signals. Filters: `strategy_key`, `strategy_version`, `source` (`cron`, `catchup`, `manual`, `legacy`), `symbol`, `direction` or `side`, `timeframe` (default `2h`), `result` (`PROFIT`, `LOSS`, `PENDING`) or `win_status` (any stored status, e.g. `STOPPED`, `TIME_EXIT`), `date`, `start_date`, `end_date`, `tz_offset_minutes`, `limit`, `offset`. Dates are days in `YYYY-MM-DD` form, matched on the cross candle: UTC days by default, or local days `tz_offset_minutes` east of UTC (send 420 for WIB days). `end_date` includes its whole day, and any other format is rejected with 422. Signals are listed newest cross first. In the summary, `eligible_closed_count`, `eligible_net_r` and `excluded_count` follow `/api/performance`; `total_r` and `total_pnl_percent` are deprecated as-booked sums over every row.
- `GET /api/profitloss-history` lists closed trades with a summary. `eligible_closed_count`, `eligible_win_count`, `eligible_win_rate` and `eligible_net_r` count only the evaluator's trades for eligible signals of `strategy_version`, as `/api/performance` does. `winrate`, `total_pnl_percent`, `total_r` and `avg_r` are deprecated as-booked figures over every listed row, legacy and hand-entered rows included. Each row carries `booked_by`: `evaluator`, `legacy` (fee-free, pre-v2.2) or `manual`. Same filters, with `result` one of `PROFIT`, `LOSS` or `EXPIRED`.
- `POST /api/profitloss-history` (owner) records a hand-entered trade (`result` one of `PROFIT`, `LOSS`, `EXPIRED`) in `profitloss_adjustments`, a table that no statistic, export or evaluator reads; `GET /api/profitloss-history/adjustments` (owner) lists them. Rows hand-entered before the ledger stay in `profitloss_history` as `booked_by = manual`, never counted. A `signal_id` that names a stored signal is refused with 409, since the evaluator books that signal's outcome (record your own fills in the journal).
- `POST /api/profitloss-history/evaluate` evaluates open signals and writes the results to `profitloss_history`.

### Signal ledger

Every fact about a signal is an event in `signal_events`, one global hash chain: `hash = sha256(prev_hash || canonical_json({id, signal_id, event_type, occurred_at, payload}))`, starting from 64 zeros (`src/ledger/chain.py` defines canonical JSON). PostgreSQL triggers refuse every `UPDATE`, `DELETE` and `TRUNCATE` on `signal_events`, `ledger_heads` and `signal_candles`, and appends are serialized with an advisory lock, so the chain stays linear. The triggers stop ordinary statements, not the tables' owner: the app's own role owns them (it recreates the triggers on every boot) and can disable a trigger, as can a superuser. An event changed or removed in the middle of the chain breaks it; removing the newest events, or rewriting the chain together with `ledger_heads`, is only caught against a head copied outside this database (`--head HASH` below). Events are written in the same transaction as the change they record:

- `detected` when a signal is stored: its levels, provenance (`strategy_ref`, `params_hash`, `code_hash`, `params_source` frozen or settings, `git_sha` from the `GIT_SHA` build arg) and `input_candles_sha256`, the hash of the closed candles the strategy read (also on `macd_signals.input_candles_sha256`).
- `resolved` when the evaluator closes it: outcome, exit, gross/cost/net R, `cost_model_version`, and the sha256 of the klines it was settled on, which are stored in `signal_candles`.
- `excluded` when a booking moves a signal to legacy (interpolated entry).
- `alert_sent` when a scheduled Telegram message announcing it (or its close) is delivered. Manual-scan alerts are not recorded.
- `restated` (plus `excluded` for rows outside their version's track record) once, on the boot that installs the ledger (while `signal_events` is empty), for every signal stored before it, as Phase 0 classified and restated it under `cm1`. Genesis never runs again: a signal that appears later without events was stored outside the app, so it stays without events, `/api/ledger/verify` lists it under `rows.without_events`, and every statistic leaves it out (`excluded.outside_ledger`).
- `entered` is reserved for strategies whose entry fills after detection (limit entries); next-open entries are known at detection.

The first scheduled run of each UTC day writes the chain head to `ledger_heads`.

- `GET /api/signals/{signal_id}/events` lists one signal's events with their hashes.
- `GET /api/ledger/head` returns the live head (`event_id`, `hash`, `event_count`) and the last 30 daily heads. Copy one somewhere outside this server: a later chain that does not contain it was rewritten.
- `GET /api/ledger/verify` recomputes the chain on the server (`ok`, `first_bad_event`) and checks the daily heads, the persisted candle windows, and that every signal's live row matches its latest event (`rows.drifted`; a booked trade whose row is gone shows as `trade.missing`, a signal whose row is gone under `rows.missing`). Once the chain is broken, the rows are not compared (`rows.skipped`) and heads past the break count as `heads.unverifiable`. The result is reused for up to 5 minutes only while no ledger table changes (row counts, highest ids and the newest writing transaction of `signal_events`, `ledger_heads`, `signal_candles`, `macd_signals` and `profitloss_history`); `cached_for_seconds` says how old it is. The exports are rebuilt at most once a minute, and only when the ledger changed.
- `GET /api/ledger.csv` lists every signal, counted or not, with `is_eligible`, `exclusion_reason`, `counted`, `headline`, the booked and restated R columns, `input_candles_sha256`, `candles_sha256` and its latest event hash. `?strategy_key=&strategy_version=` limits it to one strategy. `GET /api/ledger` returns the same rows as JSON. `GET /api/ledger/events?limit=10` lists the newest events of the chain with their signal and hashes. `GET /api/ledger.jsonl` adds every event and daily head.
- `python -m src.ledger verify` checks the database in `DATABASE_URL` (or `--database-url URL`), or an export with `--jsonl FILE` (`-` for stdin); `--head HASH` (repeatable) requires a previously published head to be part of the chain. It prints the report and exits 0 when everything checks out, 1 otherwise.

### Journal

- `GET /api/journal` lists journal entries with their signals. Filters: `status` (`TAKEN`, `SKIPPED`), `symbol`, `limit`, `offset`.
- `PUT /api/signals/{signal_id}/journal` saves an entry: `{ "status": "TAKEN", "entry_fill": 1.399, "exit_fill": 1.361, "notes": "optional" }`.
- `DELETE /api/signals/{signal_id}/journal` removes it.

### Operations

- `GET /api/cron/status` shows the scheduler state, next run and last successful run (error text only when signed in).
- `POST /api/cron/trigger` runs the scheduled job now. Manual runs are not logged and do not count as scheduled runs.
- Every scheduled run writes a row to `cron_runs` (`slot_ms`, `started_at`, `finished_at`, `status` `ok`/`error`/`skipped`, `signals_count`, `evaluated_count`, `error`).
- `GET /api/cron/runs?limit=50` lists the newest runs (error text only when signed in), the last success, and `schedule`: of the 2H slots due in the last 7 days (from the first recorded slot), how many have an `ok` run that started within 15 minutes of the slot.
- `GET /api/health` returns only `status`, `db`, `redis`, `cron_fresh` and `version`. `status` is `degraded` when Redis is down or, with `CRON_ENABLED`, the last successful scheduled run is older than `CRON_STALE_SECONDS` (default 2h05m; read from `cron_runs`, else from memory, and timed from startup before the first success). With `REQUIRE_CRON=true` (set it in production) it is also `degraded` while `CRON_ENABLED` is off. It is `down` with HTTP 503 only when PostgreSQL is unreachable or does not answer within `HEALTH_PROBE_TIMEOUT_SECONDS` (default 2s; every check runs at once under that limit), so the endpoint answers inside the Docker healthcheck's 5s and fails it on the database alone.
- `HEALTHCHECK_PING_URL` (optional) is requested after every successful scheduled run, as a dead-man switch for an external monitor such as Healthchecks.io.

## Deploying

Pushing to `master` builds and deploys (`.github/workflows/deploy.yml`). Before the first deploy of the research platform (Phase 1):

- **Rolling back below Phase 1 needs a database restore.** Phase 1 stores signals of several strategies on the same candle (a shadow signal next to a live or legacy one). The boot migration of older images deduplicates `macd_signals` on `(symbol, side, timeframe, cross_candle_ms)` without the strategy: on such a database it either fails to start the database layer or deletes signal rows and their trades, leaving their ledger events orphaned (verification then reports them under `rows.missing`). Take a `pg_dump` before deploying, and restore it if you revert to an older image.
- **Set `PUBLIC_BASE_URL=https://<domain>`** in the server's `.env`. Without it, canonical and `og:url` links follow the request's Host header and the scheme uvicorn sees (plain `http` behind the proxy); the app logs a warning at boot.
- **Mirror `nginx.conf.example` into the live nginx config**: it adds rate-limit zones for the whole-ledger endpoints (`ledger_limit`) and a looser one for per-signal candles (`candles_limit`), next to the CSP and HSTS headers.
- **Keep a copy of a daily head outside the server** (`GET /api/ledger/head`), so a later rewrite of the chain can be shown (`python -m src.ledger verify --jsonl FILE --head HASH`).

## Strategy

1. MACD with fast EMA 12, slow EMA 26 and signal EMA 9, computed on 2H candles.
2. A long signal fires when the MACD line crosses above the signal line; a short signal when it crosses below.
3. Entry, stop loss and target are set as described above, at 1.5R.
4. A signal stays open until a later 2H candle's high or low reaches the target or the stop, or until it expires.
5. PnL subtracts `ROUND_TRIP_FEE_PERCENT` (default 0.1%, taker fees for opening and closing).

MACD is computed from the last 500 candles so its values match charting platforms closely.

These rules are the registered strategy version `macd_cross@2.2.0` (`src/strategy/macd_cross.py`), whose
parameters are frozen in code. The scheduled scan runs every registered `live` and `shadow` version and
refuses (logs and skips) any whose `strategy_versions` row is not frozen or whose parameter or code hash
differs from what was frozen. Shadow signals are stored and evaluated but sent to Telegram only with
`SHADOW_ALERTS=true`, and never count in the headline (`STRATEGY_KEY@STRATEGY_VERSION`). The `MACD_*`,
`RISK_REWARD_RATIO`, `SWING_LOOKBACK_CANDLES` and `SIGNAL_EXPIRY_CANDLES` settings now only drive ad-hoc
`POST /api/scan` runs; the scheduler logs a warning when they differ from the frozen parameters. See
`src/strategy/README.md`.
