# Skill: Arsitektur & Workflow Blueprint Trading

Terapkan arsitektur dan alur kerja end-to-end untuk sistem auto-trading cryptocurrency menggunakan Binance Futures API.

## Tech Stack
- **Exchange API:** `binance-futures-connector-python` (Official U-Margined Futures Binance)
- **Database:** `firebase-admin` (Firestore)
- **Notifikasi:** `python-telegram-bot` / `httpx` ke Telegram Bot API
- **Data Processing:** `pandas`, `pandas-ta`
- **Runtime:** `asyncio` + scheduler (`APScheduler` / cron)

## Struktur Direktori Target

```
src/
├── config/             # Kredensial Binance, Firebase, Telegram, parameter
├── data_feed/          # Fetch OHLCV Binance (UM-Futures)
├── strategy/           # Kalkulasi Indikator & Blueprint Logic
├── risk_manager/       # Position Size, SL, TP (ATR-based)
├── execution/          # Order routing (Limit, OCO, Cancel)
├── services/           # Integrasi Firebase & Telegram API
└── main.py             # Orchestrator / Entry point
```

## End-to-End Pipeline

1. `main.py` memicu `data_feed` untuk mengambil OHLCV BTC/USDT (macro filter) & altcoin target
2. `strategy` mengevaluasi kondisi Macro BTC. Jika valid, evaluasi altcoin
3. Sinyal valid dikirim ke `risk_manager`
4. `risk_manager` menghitung SL, TP, dan kuantitas order
5. Kirim notifikasi Telegram: "Setup/Call Ditemukan"
6. Simpan state awal ke Firebase (Status: `PENDING`)
7. `execution` membuat Limit Order di Binance Futures
8. Jika Limit Order berhasil: Kirim Telegram + update Firebase ke `ORDER_PLACED`
9. Monitoring loop: cek status order. Jika > 5 candle unfilled, cancel + update Firebase ke `CANCELLED`
