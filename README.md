# Crypto Blueprint Bot — Asymmetric Strategy

Bot perdagangan crypto futures (Binance) berbasis Python yang mendeteksi **Golden Cross** dan **Death Cross** pada EMA 15 & 100 secara real-time. Menggunakan arsitektur modular blueprint dengan Firebase sebagai state management dan Telegram sebagai notifikasi.

## Fitur Utama
- **High-Performance Scanning**: Multi-symbol async scan menggunakan `asyncio`
- **Asymmetric Bets Strategy**: Risk/Reward ratio minimal 1:3 (Risk 1.5%, TP1 4.5%, TP2 10%)
- **Telegram Notification**: Notifikasi real-time via Telegram Bot API
- **Firebase Integration**: State management tahan crash dengan Firestore transaction
- **Macro Filter**: Hanya trading saat BTC bias terkonfirmasi (bullish/bearish)
- **Auto-Liquidity Filter**: Hanya memindai koin dengan volume harian > $10,000,000 USDT
- **Auto-Cancel Order**: Monitoring order kadaluarsa dan cancel otomatis
- **Backtesting Engine**: Simulasi historis lengkap dengan metrik performa

## Tech Stack
- **Framework**: [FastAPI](https://fastapi.tiangolo.com/)
- **Exchange**: [binance-futures-connector-python](https://github.com/binance/binance-futures-connector-python) (UM-Futures)
- **Database**: [Firebase Admin](https://firebase.google.com/docs/admin/setup) (Firestore)
- **Data**: [Pandas](https://pandas.pydata.org/) + [pandas-ta](https://github.com/twopirllc/pandas-ta)
- **Notifikasi**: [httpx](https://www.python-httpx.org/) → Telegram Bot API
- **Runtime**: FastAPI + APScheduler + Uvicorn

## Struktur Direktori

```
src/
├── config/           # Kredensial & parameter (settings.py)
├── data_feed/        # Fetch OHLCV Binance, macro filter BTC
├── strategy/         # Indikator (EMA, ATR, RSI, ADX) & blueprint logic
├── risk_manager/     # Position sizing, SL, TP calculator
├── execution/        # Order routing, auto-cancel, monitoring
├── services/         # Firebase & Telegram integration
└── main.py           # FastAPI app (orchestrator)
```

## Mode: Development vs Production

Proyek memiliki dua mode yang dikontrol via environment variable `DRY_RUN`:

| Mode | `DRY_RUN` | Perilaku |
|---|---|---|
| **Development** (default) | `true` | Scanning & sinyal tetap diproses, tapi **tidak ada order nyata** yang dikirim ke Binance. Monitor loop nonaktif. |
| **Production** | `false` | Order LIMIT benar-benar dikirim ke Binance Futures. Monitor aktif. |

Setter di `.env`:
```env
DRY_RUN=true   # dev mode (aman)
DRY_RUN=false  # prod mode (real order)
```

Bisa juga di-override per request via parameter `?dry_run=false` di endpoint `/api/scan`.

## Persiapan

1. **Clone repo & setup environment**
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Konfigurasi environment variables**
   ```bash
   cp .env.example .env
   ```
   Isi `.env` dengan credentials:
   - `BINANCE_API_KEY` / `BINANCE_API_SECRET` — API Key Binance Futures
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — Bot Telegram
   - `FIREBASE_CRED_PATH` — Path ke file JSON Firebase Admin SDK

3. **Siapkan Firebase credentials**
   - Download service account JSON dari Firebase Console
   - Simpan sesuai path di `FIREBASE_CRED_PATH`

## Menjalankan Aplikasi

```bash
uvicorn src.main:app --reload
```

Server akan berjalan di `http://localhost:8000`.

## Endpoint API

- **`GET /`** — Status bot (running, version)
- **`GET /api/scan`** — Memicu pemindaian market dan eksekusi trading

Parameter query `/api/scan`:
| Parameter | Default | Deskripsi |
|---|---|---|
| `timeframe` | `1h` | Timeframe analys (1h, 4h, 1d, dll) |
| `limit` | `200` | Jumlah candle yang di-fetch |
| `volume_m` | `50` | Threshold volume (dalam juta USDT) |
| `send_telegram` | `true` | Kirim notifikasi ke Telegram |
| `dry_run` | — | Override mode DRY_RUN (`true`/`false`) per request |

## Backtesting

Backtester mensimulasikan strategi Blueprint (Pullback Entry EMA 15) terhadap data historis Binance.

### Jalankan Backtest

```bash
python backtester.py --symbol SOLUSDT --timeframe 1h --limit 5000 --balance 10000
```

### Parameter

| Parameter | Default | Deskripsi |
|---|---|---|
| `--symbol` | `SOLUSDT` | Trading pair |
| `--timeframe` | `1h` | Timeframe candle |
| `--limit` | `5000` | Jumlah candle historis |
| `--balance` | `10000` | Modal awal simulasi (USDT) |

### Output Metrik

- Balance comparison (Before vs After)
- Net Profit/Loss ($ dan %)
- Total sinyal, order terisi, order batal
- Win rate, profit factor, max drawdown
- Daftar trade lengkap (entry/exit price, PnL)
