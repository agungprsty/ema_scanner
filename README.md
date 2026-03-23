# 🚀 Crypto Crossover Bot (Asymmetric Strategy)

Bot pemindai market Crypto Futures (Binance) berbasis Python yang mendeteksi **Golden Cross** dan **Death Cross** pada EMA 7 & 25 secara real-time. Dirancang khusus untuk berjalan efisien di lingkungan *Serverless* seperti Vercel.

## ✨ Fitur Utama
- **⚡ High-Performance Scanning**: Menggunakan `asyncio` untuk memproses ratusan token dalam hitungan detik.
- **📈 Asymmetric Bets Strategy**: Setiap sinyal dilengkapi dengan Risk/Reward ratio minimal 1:3 (Risk 1.5%, TP1 4.5%, TP2 10%).
- **🤖 Telegram Integration**: Notifikasi rapi, ringkas, dan bebas redundansi dalam satu pesan tunggal.
- **☁️ Serverless Ready**: Dioptimalkan untuk Vercel Cron Jobs tanpa perlu server yang standby 24/7.
- **📊 Auto-Liquidity Filter**: Hanya memindai koin dengan volume harian > $10,000,000 USDT.

## 🛠️ Tech Stack
- **Framework**: [FastAPI](https://fastapi.tiangolo.com/)
- **Exchange Library**: [CCXT (Async)](https://github.com/ccxt/ccxt)
- **Data Analysis**: [Pandas](https://pandas.pydata.org/)
- **Deployment**: [Vercel](https://vercel.com/)
- **Scheduler**: [Fastcron](https://fastcron.com)

## 🚀 Jalankan Aplikasi

```bash
uvicorn main:app --reload
```

## 📍 Endpoint API
- GET /: Mengecek status bot.
- GET /api/manual-scan: Memicu pemindaian market secara manual dan mengirimkan notifikasi ke Telegram.
