# 🏎️💨 Tokyo Drift // Crypto MACD 2H Quant Scanner

A production-grade, asynchronous quantitative scanner for **Binance USDⓈ-M Futures** that identifies **MACD Crossovers** strictly on the **2H Timeframe**, featuring **PostgreSQL state persistence**, **Redis signal deduplication**, dynamic **Watchlist Management**, **Win Rate & Trade Outcome Tracking**, and a high-octane **Tokyo Drift** cyberpunk aesthetic dashboard with interactive candlestick charts.

---

## ⚡ Highlights & Enhancements

- **Strict 2H Timeframe**: Strategy execution, candle ingestion, and signal tracking are strictly locked to `2h` bars.
- **Database Watchlist Management**: Watchlist symbols are stored in PostgreSQL with dynamic controls (Add pair, Remove pair, Toggle active state). The scanner defaults to scanning active watchlist symbols.
- **Tokyo Drift Aesthetics**: Neon Sakura Magenta (`#ff007f`) & Electric Cyan (`#00f0ff`) theme with circular RPM/Winrate Speedometer gauges, street racer telemetry cards, and Japanese cyberpunk styling (`東京ドリフト`).
- **Signal Detail & Interactive Candlestick Charts**: Clicking on any detected signal opens a telemetry modal with full TradingView candlestick charts, MACD indicators, and price level markers.
- **Strategy Parameters (Entry, Stop Loss, TP1)**:
  - **Entry Price**: Exact crossover price calculated via linear interpolation.
  - **Stop Loss**: Dynamic swing low/high of recent candles with buffer.
  - **TP1 (Take Profit 1)**: Asymmetric reward target calibrated to a strict 1:1.5 Risk-to-Reward ratio.
- **Automated 2H Cronjob (Jam 00, 02, 04, ...)**: Runs background scan and profit/loss verification precisely at the turn of every even hour, ensuring only confirmed 2H candles are evaluated and stored.
- **Dedicated Profit/Loss History Ledger (`profitloss_history`)**: Records resolved trades with exact Entry price, Exit price, outcome (`PROFIT` / `LOSS`), PnL percentage, and timestamps.
- **Multi-Parameter Filtering**: Full querying by Date (`YYYY-MM-DD` or range), Timeframe, Direction (`LONG` / `SHORT`), and Result (`PROFIT`, `LOSS`, `PENDING`) across both signals and profit/loss history.
- **Automated Trade Outcome & Win Rate Evaluation**: Evaluates whether in-progress signals reached TP1 first (Win) or Stop Loss first (Loss), dynamically calculating historical win rates.
- **Full Dockerized Stack**: Seamless deployment via Docker Compose with FastAPI app, PostgreSQL 16, and Redis 7.

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
| Charting & Frontend | TradingView Lightweight Charts (v4.2.1), Vanilla CSS, HTML5 |
| Containerization | Docker & Docker Compose |

---

## 📁 Project Structure

```
.
├── Dockerfile                   # Multi-layer container definition
├── docker-compose.yml           # Compose spec (App, PostgreSQL, Redis)
├── requirements.txt             # Python dependencies
├── .env.example                 # Environment variables template
├── README.md                    # Documentation
├── src/
│   ├── config/
│   │   └── settings.py          # Strict 2h timeframe, MACD, and risk parameters
│   ├── data_feed/
│   │   ├── binance_client.py    # Binance Futures client (RSA/HMAC, Proxy support)
│   │   └── ohlcv.py             # 2H Kline ingestion and chart payload builder
│   ├── strategy/
│   │   └── macd.py              # MACD crossover detection with swing SL & TP1 calculation
│   ├── services/
│   │   ├── db.py                # PostgreSQL schema (Signals & Watchlist), outcome evaluator
│   │   ├── redis_client.py      # Redis async client and 2h cross deduplication
│   │   └── telegram.py          # Asynchronous Telegram alert dispatcher
│   ├── static/
│   │   ├── index.html           # Tokyo Drift theme dashboard with Speedometer gauge
│   │   └── app.js               # Reactive client with interactive candlestick charting
│   └── main.py                  # FastAPI routes (Scanner, Watchlist, Signals, Klines)
└── tests/
    ├── test_macd.py             # MACD strategy and trade level unit tests
    └── test_api.py              # Watchlist lifecycle and API integration tests
```

---

## 🐳 Running with Docker Compose

### 1. Launch Services

```bash
docker compose up --build -d
```

### 2. Verify Stack Status

```bash
docker compose ps
```

### 3. Open Tokyo Drift Dashboard

Navigate to [http://localhost:7860](http://localhost:7860) in your browser.

---

## 📡 API Endpoints

### 1. Scanner & Charts
- `GET /api/macd-scan`: Trigger a 2H MACD scan across active watchlist symbols.
  - Query params: `lookback` (default: 5), `include_current` (default: false), `send_telegram` (default: true).
- `GET /api/klines?symbol=BTCUSDT`: Returns 2H candlestick data and computed MACD series for charting.
- `GET /api/signals/{signal_id}`: Fetch detailed trade telemetry for a specific signal.
- `POST /api/evaluate`: Evaluates all pending signals against subsequent price action to determine `TP1_HIT` or `SL_HIT`.

### 2. Watchlist Management
- `GET /api/watchlist`: List all watchlist symbols with active status and notes.
- `POST /api/watchlist`: Add a new pair to the database watchlist.
  ```json
  { "symbol": "SUIUSDT", "notes": "Momentum L1" }
  ```
- `PATCH /api/watchlist/{symbol}/toggle`: Toggle symbol active state (`true` / `false`).
- `DELETE /api/watchlist/{symbol}`: Remove symbol from watchlist.

### 3. Performance & History
- `GET /api/summary`: Returns aggregate performance metrics (Win Rate %, Wins, Losses, In-Progress, Active Watchlist Count, LONG/SHORT distribution).
- `GET /api/signals`: Paginated historical signal records from PostgreSQL with filtering support:
  - Query params: `symbol`, `direction` / `side` (`LONG`/`SHORT`), `timeframe` (default `2h`), `result` / `win_status` (`PROFIT`, `LOSS`, `PENDING`), `date` (`YYYY-MM-DD`), `start_date`, `end_date`, `limit`, `offset`.
- `GET /api/profitloss-history`: Dedicated history of all resolved trades from `profitloss_history` table:
  - Query params: `symbol`, `direction`, `timeframe`, `result` (`PROFIT`/`LOSS`), `date`, `start_date`, `end_date`, `limit`, `offset`.
  - Returns paginated trade records plus performance summary (`total_trades`, `profit_count`, `loss_count`, `winrate`, `total_pnl_percent`).
- `POST /api/profitloss-history/evaluate`: Evaluates pending signals against Binance candle price action, writes outcomes (`PROFIT` or `LOSS`) to `profitloss_history`, and updates database records.
- `GET /api/cron/status`: Get the current execution status and next scheduled run for the 2H automated cronjob.
- `POST /api/cron/trigger`: Manually trigger the 2H cronjob (runs trade outcome evaluation then confirmed candle scan).
- `GET /api/health`: Healthcheck verifying PostgreSQL, Redis, Cron Scheduler, and 2H engine status.

---

## 🏁 Strategy Mechanics (2H Frame)

1. **Indicator Setup**:
   - Fast EMA = 12, Slow EMA = 26, Signal EMA = 9.
   - Applied strictly to **2-Hour** candles.
2. **Crossover Trigger**:
   - **Bullish Golden Cross (LONG)**: MACD line crosses above Signal line.
   - **Bearish Death Cross (SHORT)**: MACD line crosses below Signal line.
3. **Dynamic Swing Risk Management**:
   - **Entry Level**: Interpolated cross price.
   - **Stop Loss**: Placed 0.2% beyond the swing low (LONG) or swing high (SHORT) of the lookback window.
   - **Take Profit 1 (TP1)**: Target set at **1.5× Risk** ($RR = 1:1.5$).
4. **Outcome Validation**:
   - Signals are tracked until the 2H candle highs/lows reach either the TP1 target or the Stop Loss level.
