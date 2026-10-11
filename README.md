# Crypto MACD Scanner — Binance USDⓈ-M Futures

A production-grade, asynchronous automated scanner for **Binance USDⓈ-M Futures** that identifies **MACD Crossovers** (Bullish Golden Crosses & Bearish Death Crosses) with **PostgreSQL state persistence**, **Redis signal deduplication & caching**, and optional **real-time Telegram notifications**.

The application is completely containerized with **Docker Compose**, providing a streamlined stack with FastAPI, PostgreSQL, and Redis.

---

## 🚀 Key Features

- **Focused Alpha Strategy**: Dedicated specifically to MACD (12, 26, 9) signal detection on any timeframe (15m, 1h, 4h, 1d).
- **Sub-Second Precision & Interpolation**: Calculates exact cross prices using linear interpolation between candle closes.
- **Forming vs. Confirmed Signals**: Supports both strictly closed candle confirmation or in-progress candle monitoring.
- **Concurrent Async Screening**: Scans dozens of liquid symbols in parallel using `asyncio.Semaphore`.
- **Top Liquidity Auto-Discovery**: Automatically queries Binance 24h ticker data to scan the highest-volume USDT futures pairs.
- **PostgreSQL Persistence**: Stores detected crossover signals in a relational database with indexed query capabilities.
- **Redis Deduplication & Caching**: Prevents duplicate alerts and re-processing for the same candle crossover.
- **Telegram Alert Delivery**: Non-blocking asynchronous alerts formatted with status, prices, and MACD indicators.
- **Modern Web Dashboard**: Glassmorphic, dark-themed UI to trigger scans, monitor system health, and view historical crosses.
- **Containerized Deployment**: One-command launch via `docker compose` with built-in healthchecks for all services.

---

## 🛠️ Technology Stack

| Layer | Technology |
|---|---|
| API Framework | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) |
| Language & Runtime | Python 3.12 / 3.13 (`asyncio`) |
| Exchange Connectivity | [binance-futures-connector-python](https://github.com/binance/binance-futures-connector-python) (UM-Futures) |
| Relational Database | [PostgreSQL 16](https://www.postgresql.org/) via [SQLAlchemy 2.0](https://www.sqlalchemy.org/) + [asyncpg](https://github.com/MagicStack/asyncpg) |
| Cache & Dedup | [Redis 7](https://redis.io/) via `redis.asyncio` |
| Notifications | [httpx](https://www.python-httpx.org/) → Telegram Bot API |
| Containerization | Docker & Docker Compose |

---

## 📁 Project Structure

```
.
├── Dockerfile                   # Optimized multi-layer container definition
├── docker-compose.yml           # Compose spec (App, PostgreSQL, Redis)
├── requirements.txt             # Python dependencies
├── .env.example                 # Environment variables template
├── README.md                    # Documentation
├── src/
│   ├── config/
│   │   └── settings.py          # Centralized configuration via Pydantic/dotenv
│   ├── data_feed/
│   │   ├── binance_client.py    # Binance Futures client (HMAC, RSA, Proxy support)
│   │   └── ohlcv.py             # OHLCV Kline ingestion
│   ├── strategy/
│   │   └── macd.py              # MACD computation and crossover detection logic
│   ├── services/
│   │   ├── db.py                # PostgreSQL schema, models, and query handlers
│   │   ├── redis_client.py      # Redis async client and cross deduplication
│   │   └── telegram.py          # Asynchronous Telegram alert dispatcher
│   ├── static/
│   │   ├── index.html           # Modern dark-mode web dashboard
│   │   └── app.js               # Reactive frontend client
│   └── main.py                  # FastAPI application entrypoint & API routes
└── tests/
    ├── test_macd.py             # MACD strategy unit tests
    └── test_api.py              # API endpoint integration tests
```

---

## 🐳 Quick Start with Docker Compose

### 1. Clone & Configure Environment

```bash
git clone <repo-url>
cd ema_scanner

# Copy template to .env
cp .env.example .env
```

Edit `.env` to configure your Binance API credentials, Telegram tokens, or custom parameters.

### 2. Start the Stack

```bash
docker compose up --build -d
```

This launches three interconnected containers:
1. `macd_scanner_postgres`: PostgreSQL 16 database with persistent storage.
2. `macd_scanner_redis`: Redis 7 in-memory cache and deduplication store.
3. `macd_scanner_app`: FastAPI application running on port `7860`.

### 3. Verify Health

Check container status:
```bash
docker compose ps
```

Check the health endpoint:
```bash
curl http://localhost:7860/api/health
```

Expected response:
```json
{
  "status": "healthy",
  "database": true,
  "redis": true,
  "version": "2.0.0"
}
```

### 4. Access Web Dashboard

Open your browser and navigate to:
```
http://localhost:7860
```

---

## 💻 Local Development Setup (Without Docker)

If you prefer running directly on your host machine:

### 1. Prerequisites
- Python 3.12 or 3.13
- PostgreSQL & Redis running locally (e.g. via `docker compose up -d postgres redis`)

### 2. Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Run FastAPI Application

```bash
uvicorn src.main:app --host 0.0.0.0 --port 7860 --reload
```

### 4. Run Automated Tests

```bash
PYTHONPATH=. pytest tests/ -v
```

---

## 📡 API Reference

### Scan Endpoints

#### `GET /api/macd-scan` (or `/api/scan`)
Executes an on-demand MACD crossover scan across specified symbols or top liquid Binance futures.

**Query Parameters:**
| Parameter | Type | Default | Description |
|---|---|---|---|
| `symbols` | `string` | *(Optional)* | Comma-separated pairs (e.g. `BTCUSDT,ETHUSDT`). If omitted, scans top liquid pairs. |
| `timeframe` | `string` | `4h` | Candle timeframe (`15m`, `1h`, `4h`, `1d`). |
| `limit` | `int` | `100` | Number of historical candles to fetch (35 - 1000). |
| `lookback` | `int` | `5` | Search window in candles for crossover occurrence. |
| `include_current` | `bool` | `false` | Whether to evaluate in-progress (unclosed) candle. |
| `send_telegram` | `bool` | `true` | Send Telegram alert for newly detected signals. |
| `save_db` | `bool` | `true` | Persist new signals to PostgreSQL. |

**Example Request:**
```bash
curl "http://localhost:7860/api/macd-scan?timeframe=4h&lookback=3"
```

**Example Response:**
```json
{
  "status": "success",
  "timeframe": "4h",
  "limit": 100,
  "lookback": 3,
  "include_current": false,
  "execution_time": "1.42s",
  "total_scanned": 50,
  "new_signals_count": 2,
  "signals": [
    {
      "symbol": "BTCUSDT",
      "side": "LONG",
      "reason": "macd_golden_cross",
      "cross_time": "2026-10-11T00:00:00Z",
      "cross_price": 65120.45,
      "close": 65350.0,
      "macd": 12.45,
      "signal": 10.12,
      "histogram": 2.33,
      "bars_ago": 1,
      "confirmed": true,
      "is_new": true
    }
  ]
}
```

---

### History & Analytics Endpoints

#### `GET /api/signals`
Queries persisted MACD crossover signals from PostgreSQL.

**Query Parameters:**
- `symbol` (optional): Filter by symbol name (e.g. `BTCUSDT`).
- `side` (optional): Filter by `LONG` or `SHORT`.
- `timeframe` (optional): Filter by timeframe (e.g. `4h`).
- `limit` (default: `50`): Max records returned.
- `offset` (default: `0`): Pagination offset.

#### `GET /api/summary`
Returns aggregate statistics of detected crossovers stored in PostgreSQL:
```json
{
  "total_signals": 142,
  "long_signals": 84,
  "short_signals": 58,
  "symbols_count": 26,
  "latest_signal_time": "2026-10-11T08:00:00Z"
}
```

#### `GET /api/health`
Checks database and Redis connectivity.

---

## ⚙️ Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Server bind host |
| `PORT` | `7860` | Server bind port |
| `DRY_RUN` | `true` | Informational operating mode |
| `POSTGRES_USER` | `postgres` | PostgreSQL username |
| `POSTGRES_PASSWORD` | `postgres` | PostgreSQL password |
| `POSTGRES_DB` | `macd_scanner` | PostgreSQL database name |
| `POSTGRES_HOST` | `postgres` (or `localhost`) | PostgreSQL hostname |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `DATABASE_URL` | *(Built from above)* | Full SQLAlchemy `postgresql+asyncpg://` URI |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection URL |
| `BINANCE_API_KEY` | `""` | Binance API key |
| `BINANCE_API_SECRET` | `""` | Binance API secret |
| `BINANCE_PROXY` | `""` | HTTP/HTTPS Proxy (e.g. `http://user:pass@host:port`) |
| `TELEGRAM_BOT_TOKEN` | `""` | Telegram Bot Token from @BotFather |
| `TELEGRAM_CHAT_ID` | `""` | Target Telegram Chat / Channel ID |
| `MACD_FAST` | `12` | MACD Fast EMA period |
| `MACD_SLOW` | `26` | MACD Slow EMA period |
| `MACD_SIGNAL` | `9` | MACD Signal EMA period |
| `MACD_TIMEFRAME` | `4h` | Default scan timeframe |
| `MACD_CROSS_LOOKBACK_CANDLES` | `5` | Maximum candles ago to detect crossover |
| `MACD_MAX_SYMBOLS` | `50` | Maximum symbols to scan per request |
| `VOLUME_THRESHOLD_USD` | `50000000` | Minimum 24h USD volume for auto-discovered pairs |
| `SCAN_CONCURRENCY` | `10` | Parallel symbol scanner concurrency limit |

---

## 🛡️ License

MIT License. Designed for quantitative analysis and automated screening.
