# Skill: Data Feed & Alpha Strategy (Blueprint)

Terapkan modul pengambilan data dan generasi sinyal trading berdasarkan strategi Blueprint.

## 1. Data Feed
- Gunakan `UMFutures` dari `binance.um_futures`
- Endpoint: `klines` (OHLCV)
- **Aturan Kritis:** Buang baris data terakhir (candle berjalan):
  ```python
  df = df.iloc[:-1].copy()
  ```

## 2. Macro Filter (BTC Bias)
Sebelum analisa altcoin, hitung status BTC:
- Ambil OHLCV `BTCUSDT`, hitung VWAP Mingguan, EMA(15), EMA(100)
- **BULLISH:** `Close BTC > VWAP` AND `EMA15 > EMA100` → hanya izinkan sinyal LONG
- **BEARISH:** `Close BTC < VWAP` AND `EMA15 < EMA100` → hanya izinkan sinyal SHORT

## 3. Alpha Logic per Altcoin
Gunakan data N-1 (candle terakhir yang sudah close).

### Parameter Indikator
- Trend: **ADX(14)**
- Volatilitas: **ATR(14)**
- Trigger: **EMA(15)** dan **EMA(100)**
- Momentum: **RSI(14)**
- Volume: **SMA Volume(20)**

### Logika Sinyal LONG (berkebalikan untuk SHORT)
1. ADX > 25 (Trend kuat)
2. EMA15 menyilang ke atas EMA100 dari N-2 ke N-1 (golden cross)
3. Volume N-1 > SMA Volume(20)
4. RSI N-1 < 65
5. Macro Filter BTC = BULLISH

Jika semua terpenuhi, kembalikan: `Symbol`, `Side`, `Current_Price`, `EMA15_Value`, `ATR_Value`
