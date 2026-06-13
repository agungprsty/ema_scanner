# EMA7/EMA50 Cross Scanner — Quantitative Futures Trading Bot

A production-grade Python bot for Binance USDⓈ-M Futures that implements a quantitative **EMA7/EMA50 cross strategy** with a **4H macro trend filter**. The system features a modular architecture with Firebase-backed state management and real-time Telegram notifications.

## Key Capabilities

- **Two-Stage Screening Pipeline**: Macro-level trend filter (4H) narrows the watchlist before triggering entry signals on the 1H timeframe
- **EMA7/EMA50 Cross Entry**: Detects EMA7/EMA50 crossovers (golden cross for LONG, death cross for SHORT) on the 1H chart
- **4H Macro Trend Filter**: Restricts trading to symbols trading above a rising EMA50 on the 4H timeframe
- **Volume-Confirmed Entries**: Requires volume to exceed the 20-period moving average before triggering
- **ATR-Based Stop Loss**: Dynamic stop loss calculated from the lowest low of the lookback window minus 1.5× ATR(14)
- **Partial Take-Profit (50%)**: Half the position exits at TP1 (EMA50 of the 4H macro timeframe), the remainder trails to breakeven
- **Automated Breakeven Management**: Stop loss automatically moves to breakeven (entry + 0.05% fee buffer) after TP1 is hit
- **Real-Time Telegram Alerts**: Trade signals, fills, and status updates delivered via Telegram Bot API
- **Firestore State Management**: Crash-resistant state persistence using Firestore transactions and write batches
- **BTC Macro Bias Filter**: Scans are gated by Bitcoin's EMA7/EMA50 directional bias (bullish/bearish) on the 4H timeframe with configurable strength thresholds
- **Liquidity Filter**: Automatically excludes low-volume pairs below a configurable daily USDT threshold
- **Auto-Cancel Stale Orders**: Monitors open limit orders and cancels those exceeding a configurable time-to-live
- **Concurrent Symbol Scanning**: Processes up to 10 symbols in parallel, reducing scan time by approximately 4x
- **Historical Backtesting Engine**: Full-featured simulator with performance metrics and visual chart output

## Technology Stack

| Layer | Technology |
|---|---|
| API Framework | [FastAPI](https://fastapi.tiangolo.com/) |
| Exchange Connectivity | [binance-futures-connector-python](https://github.com/binance/binance-futures-connector-python) (UM-Futures) |
| Database | [Firebase Admin SDK](https://firebase.google.com/docs/admin/setup) — Firestore |
| Data Processing | [Pandas](https://pandas.pydata.org/) + [pandas-ta](https://github.com/twopirllc/pandas-ta) |
| Messaging | [httpx](https://www.python-httpx.org/) → Telegram Bot API |
| Runtime | FastAPI + Uvicorn (ASGI) |

## Project Structure

```
src/
├── config/           # Credentials and trading parameters (settings.py)
├── data_feed/        # OHLCV data ingestion, Binance client, BTC macro filter
├── strategy/         # Technical indicators (EMA, ATR, RSI, ADX) and signal logic
├── risk_manager/     # Position sizing, stop-loss, and take-profit calculator
├── execution/        # Order routing, auto-cancellation, position monitoring
├── services/         # Firebase Firestore and Telegram integration layer
└── main.py           # FastAPI application entry point and orchestrator
```

## Strategy Overview: EMA7/EMA50 Cross (1H / 4H)

### Stage 1 — Macro Trend Filter (4H Watchlist)

The system first screens all liquid USDT pairs against a macro-level trend filter. A symbol enters the watchlist when both conditions are satisfied:

| Condition | Formula |
|---|---|
| Price above EMA50 | `Close(4H) > EMA50(4H)` |
| EMA50 sloping upward | `EMA50(4H)[current] >= EMA50(4H)[3 bars ago]` |

### Stage 2 — Entry Trigger (1H Cross)

Symbols that pass the macro filter are evaluated on the 1H timeframe for an entry trigger. Direction depends on the macro bias:

| Condition | Formula |
|---|---|
| Golden Cross (LONG) | `EMA7(1H) crosses above EMA50(1H)` when macro is **BULLISH** |
| Death Cross (SHORT) | `EMA7(1H) crosses below EMA50(1H)` when macro is **BEARISH** |
| Volume confirmation | `Volume(1H) > SMA_Volume_20(1H)` |

### Risk Management Framework

| Parameter | Formula |
|---|---|
| Stop Loss | `Lowest Low (10 candles) - 1.5 × ATR(14)` |
| TP1 (50% position) | `EMA50(4H)` at time of entry |
| Post-TP1 behavior | Stop loss moves to breakeven `Entry × (1 + 0.05%)` |
| Residual position | Trails until breakeven or manual exit |

## Performance Optimizations

The following optimizations were implemented to reduce average scan execution time from approximately 40 seconds to approximately 5–8 seconds:

| Optimization | Impact |
|---|---|
| Concurrent symbol scanning (10-way parallelism) | Reduced from ~40s to ~5-8s |
| Exchange info caching (300s TTL) | Eliminates redundant API calls |
| 24h ticker caching (60s TTL) | Eliminates redundant API calls |
| Singleton Binance client | Avoids repeated connection setup overhead |
| Lazy indicator computation | Macro filter runs without full ATR/RSI/ADX computation |

## Operating Modes

The system operates in two modes controlled by the `DRY_RUN` environment variable:

| Mode | `DRY_RUN` | Behavior |
|---|---|---|
| **Development** (default) | `true` | Full scanning and signal generation enabled; **no real orders** placed on Binance. Monitor loop is disabled. |
| **Production** | `false` | Limit orders are submitted to Binance Futures. Monitor loop is active. |

Configuration in `.env`:
```env
DRY_RUN=true   # safe mode for development and testing
DRY_RUN=false  # live trading mode
```

The mode can also be overridden per request using the `?dry_run=false` parameter on the `/api/scan` endpoint.

## Getting Started

### Prerequisites

- Python 3.12+
- Binance Futures API credentials
- Telegram Bot API credentials
- Firebase service account (Firestore)

### Installation

1. **Clone the repository and set up the environment**
   ```bash
   git clone <repository-url>
   cd ema_scanner
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure environment variables**
   ```bash
   cp .env.example .env
   ```
   Populate the `.env` file with the following:
   - `BINANCE_API_KEY` / `BINANCE_API_SECRET` — Binance Futures API credentials
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — Telegram bot credentials
   - `FIREBASE_CRED_PATH` — Path to the Firebase Admin SDK service account JSON file

3. **Set up Firebase credentials**
   - Download the service account JSON from the Firebase Console
   - Save it to the path specified in `FIREBASE_CRED_PATH`

### Running the Application

```bash
uvicorn src.main:app --reload
```

The server starts at `http://localhost:8000`.

## API Reference

### Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Health check — returns bot status, version, and active strategy |
| GET | `/api/scan` | Triggers a full market scan and optional trade execution |

### `/api/scan` Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `timeframe` | string | `1h` | Entry timeframe for EMA7/EMA50 cross detection |
| `htf` | string | `4h` | Macro timeframe for the trend filter |
| `limit` | integer | `500` | Number of entry candles to fetch |
| `macro_limit` | integer | `200` | Number of macro candles to fetch |
| `volume_m` | integer | `50` | Minimum 24h volume threshold (in millions of USDT) |
| `send_telegram` | boolean | `true` | Whether to send notifications via Telegram |
| `dry_run` | boolean | — | Overrides the global DRY_RUN mode for this request |

### Sample Response

```json
{
  "status": "success",
  "mode": "DRY RUN",
  "btc_bias": "BULLISH",
  "btc_strength": 45.2,
  "btc_vol_regime": "NORMAL",
  "timeframe_entry": "1h",
  "timeframe_macro": "4h",
  "execution_time": "4.20s",
  "total_scanned": 48,
  "signals": [
    {
      "symbol": "SOLUSDT",
      "side": "LONG",
      "entry": 145.20,
      "stop_loss": 141.80,
      "take_profit": 152.50,
      "quantity": 0.68,
      "quantity_tp1": 0.34,
      "reason": "golden_cross_1h",
      "status": "DRY RUN"
    }
  ]
}
```

## Backtesting

The backtesting engine simulates the EMA7/EMA50 cross strategy (4H macro filter + 1H entry) against historical Binance data. It supports golden cross, death cross, and early-entry signals (LONG/SHORT) with partial TP1/BEP/TP2 exit logic.

### Running a Backtest

```bash
python backtester.py --symbol SOLUSDT --timeframe 1h --htf 4h --limit 5000 --balance 100
```

This produces a console report and an interactive HTML trade chart.

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `--symbol` | `SOLUSDT` | Trading pair to backtest |
| `--timeframe` | `1h` | Entry timeframe for the EMA7/EMA50 cross |
| `--htf` | `4h` | Macro timeframe for the trend filter |
| `--limit` | `2000` | Number of historical candles |
| `--balance` | `100` | Simulated starting capital (USDT) |

### Sample Output

```
====================================================================
           BACKTEST SETUP — Market Order (EMA7/50 + RSI)
====================================================================
Symbol               : SOLUSDT
TF Stack             : 1h / 4h
Strategi             : Market Order @ Close + 1:2 R:R + TP1/BEP/TP2
Volume Threshold      : SMA20 x 1.45 (min)
Early Entry          : EMA gap narrowing + RSI > 55 (LONG) / < 45 (SHORT)
Initial Capital      : $100.00
Risk per Trade       : 2.0%
Leverage             : 10x

====================================================================
         BALANCE COMPARISON (BEFORE vs AFTER)
====================================================================
[BEFORE] Initial Balance : $100.00
[AFTER]  Final Balance   : $172.34
────────────────────────────────────────────────────────────────────
Net Profit/Loss ($)      : +$72.34
Net PnL %                : +72.34%

====================================================================
           STRATEGY PERFORMANCE METRICS
====================================================================
Pending Signals          : 34
Filled                   : 28
Expired                  : 6

Wins                     : 18
Losses                   : 10
Win Rate                 : 64.29%

Profit Factor            : 2.14
Max Drawdown             : -8.45%

====================================================================
           SIGNAL INFO
====================================================================
GC (Golden Cross)        : 22
DC (Death Cross)         : 3
EARLY_LONG (RSI > 55)    : 7
EARLY_SHORT (RSI < 45)   : 2
====================================================================
```

### Performance Metrics

| Metric | Description |
|---|---|
| **Net PnL** | Absolute and percentage profit or loss over the test period |
| **Win Rate** | Percentage of filled trades that closed with a positive PnL |
| **Profit Factor** | Gross profit divided by gross loss (values > 1.0 indicate profitability) |
| **Max Drawdown** | Largest peak-to-trough decline in portfolio value |
| **Filled / Expired** | Number of signals that filled vs. expired before execution |
| **TP1 Hit Rate** | Percentage of filled trades that reached partial take-profit |
| **Signal Breakdown** | Count of golden cross, death cross, and early-entry signals |

### Interactive Trade Chart

Running a backtest generates an interactive **HTML chart** (Plotly) displaying:
- Price candles with EMA7 and EMA50 overlays
- Entry markers (green triangles for longs, red for shorts)
- TP1, TP2, BEP, and Stop Loss levels per trade
- Annotated trade timeline with PnL labels

The chart is saved as `backtest_<SYMBOL>_<TF>_<HTF>.html` in the project root.

---

## Open to Contribute

Contributions, bug reports, and feature requests are welcome. Whether you are a quantitative researcher, a Python developer, or a crypto trading enthusiast, there are many ways to get involved:

### How to Contribute

1. **Fork** the repository
2. **Create a feature branch** (`git checkout -b feature/my-improvement`)
3. **Commit your changes** (`git commit -am 'Add my improvement'`)
4. **Push to the branch** (`git push origin feature/my-improvement`)
5. **Open a Pull Request**

### Areas for Contribution

- **Strategy Enhancement**: Add new entry/exit signals, improve filter logic, or implement multi-asset portfolio optimization
- **Risk Management**: Integrate Kelly Criterion, VaR-based sizing, or dynamic leverage adjustment
- **Data Pipeline**: Add support for additional exchanges or real-time WebSocket data feeds
- **Backtesting**: Extend the backtester with Monte Carlo simulation, walk-forward optimization, or Sharpe ratio reporting
- **Infrastructure**: Dockerize the application, add CI/CD pipelines, or implement Kubernetes deployment manifests
- **Documentation**: Improve README, add inline code comments, or create a strategy development guide

### Reporting Issues

Found a bug or have a suggestion? Open an issue with a clear title and detailed description, including steps to reproduce if applicable.

### Questions

For questions about the strategy, architecture, or deployment, feel free to reach out via the repository's discussion board or issue tracker.
