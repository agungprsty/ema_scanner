# Skill: Backtesting Strategy & Performance Metrics

Membangun simulator (`backtester.py`) untuk menguji performa strategi Blueprint pada data historis.

## 1. Data Historis
- Sumber: Binance Futures API (`klines`) atau file CSV
- Minimal 1-3 tahun untuk mencakup bull, bear, sideways
- Timeframe sesuai target live bot (15m, 1h, 4h)

## 2. Aturan Simulasi (Anti Look-Ahead Bias)

1. **No Look-Ahead:** Sinyal dievaluasi hanya dari harga Close candle N-1
2. **Simulasi Pullback Entry:** Order LONG hanya filled jika candle berikutnya `Low` ≤ EMA15
3. **Expiration:** Jika 5 candle setelah sinyal harga tidak menyentuh EMA15 → order `EXPIRED`
4. **SL/TP Eksekusi:** Cek candle berikutnya. `Low` ≤ SL → loss. `High` ≥ TP → win

## 3. Fungsi Utama

```python
def run_backtest(df, initial_balance, risk_pct):
    trade_log = []
    # looping baris per baris, simulasikan entry/exit
    return trade_log, metrics
```

## 4. Output Laporan (3 Bagian)

### A. Konfigurasi Setup
```
Simbol Koin        : SOLUSDT
Timeframe          : 1h
Periode            : 2025-01-01 s/d 2026-06-01
Strategi           : Blueprint (Pullback Entry EMA 15)
Modal Awal         : $10,000.00
Risiko per Trade   : 1.0%
Leverage           : 10x
```

### B. Perubahan Saldo
```
Initial Balance  : $10,000.00
Final Balance    : $14,250.50
Net P/L ($)      : +$4,250.50
Net P/L (%)      : +42.51%
```

### C. Metrik Performa
```
Total Sinyal         : 120
Filled               : 85
Expired              : 35
Win Rate             : 47.06%
Profit Factor        : 1.85
R:R Ratio            : 1:2.0
Max Drawdown         : 5.2%
```

## 5. Visualisasi
- Plot equity curve menggunakan `matplotlib`
- Setiap transaksi dicatat di `trade_log` sebagai dictionary: `{trade_id, type, entry_time, exit_time, pnl, balance_after}`
