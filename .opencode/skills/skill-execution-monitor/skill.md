# Skill: Execution & Order Monitoring

Terapkan order routing dan mekanisme auto-cancel untuk posisi trading.

## 1. Order Placement
- Tipe: `LIMIT`
- Time-in-Force: `GTC` (Good Till Cancelled)
- Setel leverage via API sebelum eksekusi

### Fase 1: Setup Call (Sebelum API Binance)
- Kirim Telegram: `[SETUP CALL] Sinyal {Side} {Symbol} di {Entry}. SL: {SL}, TP: {TP}`
- Simpan ke Firebase `trades`:
  ```json
  { "id": "uuid", "symbol": "...", "side": "...", "target_entry": ..., "status": "PENDING", "timestamp": "..." }
  ```

### Fase 2: Limit Order Terpasang
- Jika response Binance sukses → ambil `orderId`
- Update Firebase: `status: 'LIMIT_PLACED'`, `binance_order_id: orderId`
- Kirim Telegram: `[ORDER PLACED] Limit {Side} {Symbol} terpasang di {Entry}`

## 2. Auto-Cancel (Monitoring Loop)
- Background task cek order status `LIMIT_PLACED`
- Catat `created_at` saat order dibuat
- Cek status via Binance API berkala
- **Batal jika:** order belum `FILLED` setelah 5× periode timeframe (misal: TF 1h → 5 jam)
- **Aksi:** Cancel order Binance → update Firebase ke `EXPIRED`

## 3. Fill Handling
- Jika Binance return `FILLED`:
  - Kirim Stop-Market + Limit Maker untuk SL dan TP di bursa
  - Update Firebase ke `FILLED`
