# Skill: Risk Management & Position Sizing

Mengubah sinyal mentah menjadi paket order siap eksekusi dengan perhitungan risiko berbasis ATR.

## Perhitungan Entry, SL, TP (Pullback Entry)

### LONG
- Entry = `EMA15`
- StopLoss = `Entry - (ATR14 × 1.5)`
- TakeProfit = `Entry + (2.0 × (Entry - StopLoss))`

### SHORT
- Entry = `EMA15`
- StopLoss = `Entry + (ATR14 × 1.5)`
- TakeProfit = `Entry - (2.0 × (StopLoss - Entry))`

## Position Sizing
- `RISK_PER_TRADE_PERCENT` = 1% (0.01)
- Ambil saldo margin dari Binance (`account_information`)
- **Formula (kuantitas dalam unit koin):**

```
RiskAmount = TotalBalance × 0.01
PriceDistance = |Entry - StopLoss|
CoinQuantity = RiskAmount / PriceDistance
```

## Presisi (Wajib!)
- Ambil `exchange_info()` untuk simbol target
- Bulatkan harga sesuai `tickSize` dan kuantitas sesuai `stepSize`
- **Gunakan `decimal.Decimal`, jangan `float`** untuk menghindari error presisi biner
