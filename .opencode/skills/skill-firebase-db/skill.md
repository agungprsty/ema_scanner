# Skill: Firebase Firestore Schema & State Management

Mengelola state transaksi menggunakan Firebase Firestore.

## 1. Koleksi
- `active_trades` — transaksi aktif
- `trade_history` — riwayat transaksi selesai

## 2. Struktur Dokumen

```json
{
  "trade_id": "auto-generated-uuid",
  "symbol": "SOLUSDT",
  "side": "LONG",
  "tf": "1h",
  "prices": {
    "entry_target": 145.50,
    "stop_loss": 140.00,
    "take_profit": 156.50
  },
  "metrics": {
    "atr_value": 3.66,
    "qty_coins": 18.1
  },
  "status": "LIMIT_PLACED",
  "binance_order_id": 123456789,
  "timestamps": {
    "signal_generated": "2026-06-06T23:55:00Z",
    "order_placed": "2026-06-06T23:55:05Z",
    "filled_at": null,
    "closed_at": null
  }
}
```

### Enum Status
`PENDING` → `LIMIT_PLACED` → `FILLED` → `CLOSED_SL` / `CLOSED_TP`
↘ `EXPIRED` / `FAILED_MARGIN`

## 3. Aturan State Management
- Auto-cancel hanya untuk status `LIMIT_PLACED`
- Jika Binance return `FILLED`: kirim Stop-Market + Limit Maker SL/TP, update Firebase ke `FILLED`

## 4. Best Practices
- **Singleton:** `firebase_admin.initialize_app()` cukup sekali saat startup
- **Transaction:** Update status (`LIMIT_PLACED` → `FILLED`) wajib pakai Firestore Transaction
- **Batch Writes:** Gunakan `WriteBatch` untuk update banyak dokumen sekaligus
- **Indeks:** Buat indeks pada field `status` + `timestamp` untuk query efisien
