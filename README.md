# EMA5/EMA20 Cross Scanner — Strategy (1h/4h)

Bot perdagangan crypto futures (Binance) berbasis Python yang menerapkan strategi **EMA5/EMA20 Cross** dengan **Macro Filter 4H** secara real-time. Menggunakan arsitektur modular dengan Firebase sebagai state management dan Telegram sebagai notifikasi.

## Fitur Utama
- **Two-Stage Screening**: Macro filter 4H → entry trigger 1H (efisien)
- **EMA5/EMA20 Cross Entry**: EMA5 crossover EMA20 pada timeframe 1H
- **Macro Filter 4H**: Hanya trading saat price > EMA20 dan EMA20 melandai/naik
- **Volume Confirmation**: Entry hanya saat volume > MA 20
- **ATR-Based Stop Loss**: SL dinamis berdasarkan Lowest Low + 1.5×ATR(14)
- **Partial Take Profit**: 50% posisi di TP1 (EMA20 4H), sisa trailing
- **Break Even Protection**: SL otomatis pindah ke BEP setelah TP1 hit
- **Telegram Notification**: Notifikasi real-time via Telegram Bot API
- **Firebase Integration**: State management tahan crash dengan Firestore transaction
- **Macro Filter**: Hanya trading saat BTC bias terkonfirmasi (bullish)
- **Auto-Liquidity Filter**: Hanya memindai koin dengan volume harian > $50,000,000 USDT
- **Auto-Cancel Order**: Monitoring order kadaluarsa dan cancel otomatis
- **Backtesting Engine**: Simulasi historis lengkap dengan metrik performa
- **Concurrent Scanning**: Memproses 10 simbol secara paralel untuk scanning lebih cepat

## Tech Stack
- **Framework**: [FastAPI](https://fastapi.tiangolo.com/)
- **Exchange**: [binance-futures-connector-python](https://github.com/binance/binance-futures-connector-python) (UM-Futures)
- **Database**: [Firebase Admin](https://firebase.google.com/docs/admin/setup) (Firestore)
- **Data**: [Pandas](https://pandas.pydata.org/) + [pandas-ta](https://github.com/twopirllc/pandas-ta)
- **Notifikasi**: [httpx](https://www.python-httpx.org/) → Telegram Bot API
- **Runtime**: FastAPI + Uvicorn

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

## Strategi: EMA5/EMA20 Cross (1h/4h)

### Tahap 1 — Macro Filter 4H (Watchlist)
| Kondisi | Rumus |
|---|---|
| Price di atas EMA20 | `Close(4H) > EMA20(4H)` |
| EMA20 melandai/naik | `EMA20(4H)[now] >= EMA20(4H)[3 candles ago]` |

Jika kedua kondisi terpenuhi → aset masuk **Watchlist Long**.

### Tahap 2 — Entry Trigger 1H (Golden Cross)
| Kondisi | Rumus |
|---|---|
| Golden Cross | `EMA5(1H) cross above EMA20(1H)` |
| Volume spike | `Volume(1H) > MA_Volume_20(1H)` |

### Manajemen Risiko
| Parameter | Rumus |
|---|---|
| Stop Loss | `Lowest Low(10 candle) - 1.5 × ATR(14)` |
| TP1 (50% posisi) | `EMA20(4H)` saat entry |
| Setelah TP1 hit | SL pindah ke BEP `Entry × (1 + 0.05%)` |
| Sisa posisi | Trailing hingga BEP atau exit manual |

## Optimasi Performa

| Optimasi | Dampak |
|---|---|
| **Concurrent scanning** (10 simbol paralel) | ⚡ 40s → ~5-8s |
| **Caching exchange_info** (TTL 5 menit) | ⚡ Kurangi 1 API call/scan |
| **Caching ticker_24h** (TTL 1 menit) | ⚡ Kurangi 1 API call/scan |
| **Reuse Binance client (singleton)** | ⚡ Hindari setup koneksi berulang |
| **Lazy compute indicators** | ⚡ Macro filter cepat tanpa ATR/RSI/ADX |

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

- **`GET /`** — Status bot (running, version, strategy)
- **`GET /api/scan`** — Memicu pemindaian market dan eksekusi trading

### Parameter `/api/scan`
| Parameter | Default | Deskripsi |
|---|---|---|
| `timeframe` | `1h` | Entry timeframe (EMA5/EMA20 cross detection) |
| `htf` | `4h` | Macro timeframe (watchlist filter) |
| `limit` | `500` | Jumlah candle entry yang di-fetch |
| `macro_limit` | `200` | Jumlah candle macro yang di-fetch |
| `volume_m` | `50` | Threshold volume (dalam juta USDT) |
| `send_telegram` | `true` | Kirim notifikasi ke Telegram |
| `dry_run` | — | Override mode DRY_RUN per request |

### Contoh Response
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

Backtester mensimulasikan strategi EMA5/EMA20 Cross (macro 4H + entry 1H) terhadap data historis Binance.

### Jalankan Backtest

```bash
python backtester.py --symbol SOLUSDT --timeframe 1h --htf 4h --limit 5000 --balance 100
```

### Parameter

| Parameter | Default | Deskripsi |
|---|---|---|
| `--symbol` | `SOLUSDT` | Trading pair |
| `--timeframe` | `1h` | Entry timeframe (EMA5/EMA20 cross) |
| `--htf` | `4h` | Macro timeframe (filter) |
| `--limit` | `2000` | Jumlah candle entry historis |
| `--balance` | `100` | Modal awal simulasi (USDT) |

### Batch Backtest

```bash
python backtest_run.py
```
Menjalankan backtest pada 20 token berbeda dan menghasilkan laporan markdown.

### Output Metrik

- Balance comparison (Before vs After)
- Net Profit/Loss ($ dan %)
- Total sinyal, order terisi
- Win rate, profit factor, max drawdown
- Partial TP1 hit tracking
- Daftar trade lengkap (entry/exit price, PnL)
