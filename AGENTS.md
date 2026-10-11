# AGENTS.md — Panduan AI Agent untuk Crypto MACD Scanner

## Tujuan Proyek
Sistem automated scanner sinyal cryptocurrency kuantitatif berbasis **MACD Overcross (Golden Cross & Death Cross)** untuk Binance USDⓈ-M Futures. Sistem terintegrasi dengan PostgreSQL untuk histori sinyal, Redis untuk deduplikasi dan caching sinyal, notifikasi Telegram, web dashboard, dan full containerized via Docker Compose.

## Tech Stack
- **Exchange:** `binance-futures-connector-python` (UM-Futures)
- **Database:** `asyncpg` + `SQLAlchemy 2.0` (PostgreSQL)
- **Cache & Dedup:** `redis.asyncio` (Redis 7)
- **Notifikasi:** `httpx` → Telegram Bot API
- **Data:** `pandas`, `pandas-ta`
- **Runtime:** `asyncio`, FastAPI, Uvicorn
- **Containerization:** Docker, Docker Compose

---

## Struktur Proyek (`src/`)
```
src/
├── config/             # Kredensial & parameter (settings.py)
├── data_feed/          # Fetch OHLCV Binance (binance_client.py, ohlcv.py)
├── strategy/           # Indikator MACD & deteksi crossover (macd.py)
├── services/           # Integrasi PostgreSQL, Redis, dan Telegram
├── static/             # Web dashboard UI (index.html, app.js)
└── main.py             # FastAPI entrypoint & scanner endpoints
```

---

## Aturan Umum AI Agent

### Code Convention
- Seluruh fungsi I/O bound wajib `async def`
- Validasi request/response menggunakan **Pydantic V2**
- **Hanya commit jika diminta** — jangan pernah commit tanpa perintah eksplisit
- Jangan tambahkan komentar explicatif pada kode

### Verifikasi
- Jalankan `ruff check src` setelah perubahan
- Jalankan `PYTHONPATH=. pytest tests/` untuk validasi unit dan integrasi
- Pastikan tidak ada error sintaks sebelum pull request

### Database (PostgreSQL)
- Gunakan `AsyncSession` dan `create_async_engine`
- Schema migration / `Base.metadata.create_all` otomatis saat startup di lifespan
- Query pagination dengan `limit` dan `offset`

### Redis
- Gunakan `redis.asyncio` (non-blocking)
- Format deduplikasi key: `macd:<timeframe>:<side>:<symbol>:<candle_ms>`

### Binance
- Aktifkan `enable_server_time=True` saat init client
- Support `BINANCE_PROXY` untuk koneksi via proxy jika diatur
- Gunakan `decimal.Decimal` jika diperlukan presisi tingkat tinggi

### Telegram
- Kirim pesan via `asyncio.create_task()` / `BackgroundTasks` (non-blocking)
