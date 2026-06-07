# Skill: Best Practices & Technical Standards

Panduan praktik terbaik untuk pengembangan modul trading cryptocurrency.

## 1. FastAPI

### Asynchronous Code
- Semua endpoint dan I/O bound functions wajib `async def`
- Gunakan `httpx` (bukan `requests`) untuk HTTP async

### Validasi Pydantic V2
```python
from pydantic import BaseModel, Field

class OrderRequest(BaseModel):
    symbol: str = Field(..., example="BTCUSDT")
    side: str = Field(..., pattern="^(LONG|SHORT)$")
    amount_usd: float = Field(..., gt=0)
```

### Dependency Injection
- Gunakan `Depends` untuk inject konfigurasi database/API key
- Mempermudah unit testing

## 2. Firebase/Firestore

### Singleton
- `firebase_admin.initialize_app()` hanya sekali saat startup

### Transaksi & Batch
- Update status order wajib pakai Firestore **Transaction**
- Update massal pakai **WriteBatch**

### Optimasi Query
- Buat indeks pada field `status` + `timestamp`
- Hindari `collection_group` / full scan

## 3. Telegram Bot API

### Non-Blocking
- Bungkus kirim pesan di `BackgroundTasks` / `asyncio.create_task()`
- Kegagalan Telegram tidak boleh menghentikan eksekusi utama

### Escape Special Characters
- Jika `parse_mode="MarkdownV2"`, escape karakter spesial (`.`, `-`, `!`, `_`) dengan `re.escape()`

## 4. Binance Futures API

### Sinkronisasi Waktu
```python
client = UMFutures(key=API_KEY, secret=API_SECRET, enable_server_time=True)
```

### Error Handling
- `-2019 (Margin Insufficient)`: Cancel order, update Firebase `FAILED_MARGIN`, alert Telegram
- `-1013 (Filter Failure)`: Quantity/price tidak sesuai tick size / step size

### Presisi Koin
- Ambil `exchange_info()` untuk tickSize & stepSize
- Gunakan `decimal.Decimal` (bukan `float`) untuk pembulatan

### Rate Limiting
- Jangan spam REST API
- Gunakan WebSocket untuk monitoring harga real-time jika memantau banyak koin
