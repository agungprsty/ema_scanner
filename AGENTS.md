# AGENTS.md — Panduan AI Agent untuk Refactoring Crypto Trading Bot

## Tujuan Proyek
Refactoring sistem auto-trading cryptocurrency dari arsitektur monolitik (`app/`) menuju modular blueprint (`src/`). Sistem mengimplementasikan strategi kuantitatif, menyimpan state di Firebase, notifikasi Telegram, dan auto-cancel order.

## Tech Stack
- **Exchange:** `binance-futures-connector-python` (UM-Futures)
- **Database:** `firebase-admin` (Firestore)
- **Notifikasi:** `httpx` → Telegram Bot API
- **Data:** `pandas`, `pandas-ta`
- **Runtime:** `asyncio`, FastAPI, APScheduler

---

## Struktur Target (`src/`)
```
src/
├── config/             # Kredensial & parameter
├── data_feed/          # Fetch OHLCV Binance
├── strategy/           # Indikator & Blueprint logic
├── risk_manager/       # Position sizing, SL, TP
├── execution/          # Order routing, auto-cancel
├── services/           # Firebase & Telegram integration
└── main.py             # Orchestrator
```

---

## Dokumen Refactoring Plan

| File | Isi |
|---|---|
| `refactor-plan/01_Architecture_and_Workflow.md` | Arsitektur, struktur direktori, pipeline E2E |
| `refactor-plan/02_Data_and_Alpha_Strategy.md` | Data feed, macro filter BTC, alpha logic |
| `refactor-plan/03_Risk_and_Position_Sizing.md` | ATR-based risk, entry/SL/TP, position sizing |
| `refactor-plan/04_Execution_and_Monitoring.md` | Order LIMIT, notifikasi, auto-cancel |
| `refactor-plan/05_Database_Schema_Firebase.md` | Firestore schema, state management |
| `refactor-plan/06_Backtesting_Strategy_and_Metrics.md` | Backtester, performa metrik |

---

## Skills (.opencode/skills)

Setiap skill berisi instruksi detail untuk AI Agent. **Load skill sebelum mengerjakan modul terkait.**

| Skill | Modul | Gunakan Saat |
|---|---|---|
| `skill-architecture-workflow` | Semua modul | Memahami struktur & pipeline proyek |
| `skill-data-alpha` | `src/data_feed/`, `src/strategy/` | Fetch OHLCV, macro filter, sinyal alpha |
| `skill-risk-manager` | `src/risk_manager/` | Kalkulasi position sizing, SL, TP |
| `skill-execution-monitor` | `src/execution/` | Order placement, auto-cancel, monitoring |
| `skill-firebase-db` | `src/services/` | Schema Firestore, state management |
| `skill-backtester` | `backtester.py` | Simulasi historis, metrik performa |
| `skill-best-practices` | Semua modul | FastAPI, Firebase, Telegram, Binance patterns |

### Cara Load Skill
```
Load skill: skill-<nama-skill>
```

---

## Aturan Umum AI Agent

### Code Convention
- Seluruh fungsi I/O bound wajib `async def`
- Validasi request/response menggunakan **Pydantic V2**
- **Hanya commit jika diminta** — jangan pernah commit tanpa perintah eksplisit
- Jangan tambahkan komentar explicatif pada kode

### Verifikasi
- Jalankan `npm run lint` / `ruff` setelah perubahan
- Jalankan `npm run typecheck` / `mypy` jika tersedia
- Pastikan tidak ada error sintaks sebelum pull request

### Firebase
- `firebase_admin.initialize_app()` hanya sekali (singleton)
- Update status wajib pakai **Transaction**
- Gunakan **WriteBatch** untuk update massal

### Binance
- Aktifkan `enable_server_time=True` saat init client
- Gunakan `decimal.Decimal` untuk presisi harga/kuantitas
- Ambil `exchange_info()` untuk tickSize & stepSize

### Telegram
- Kirim pesan via `asyncio.create_task()` / `BackgroundTasks` (non-blocking)
- Escape karakter spesial jika pakai MarkdownV2
