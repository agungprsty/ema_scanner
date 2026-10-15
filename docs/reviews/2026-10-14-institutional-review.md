# Crossline v2.2.0: Kritik dari Meja Institusi

**Untuk:** Developer/owner Crossline
**Dari:** Senior trader/PM, desk crypto-futures institusional ($50M+ notional book)
**Objek:** Production `https://macd-scan.bantubuat.web.id/` (DRY_RUN) dan source `origin/master`
**Snapshot live:** 2026-10-14 15:52 UTC (22:52 WIB), di-GET ulang 16:06–16:08 UTC

---

## 1. Verdict singkat

Untuk ukuran institusi, aturan yang sedang jalan di production tidak punya edge yang bisa ditradingkan, berapa pun size-nya. Logika produksi saya replikasi dengan tiga engine independen. Hasilnya cocok trade-per-trade (0 outcome mismatch di 7.420 trade bersama), dan level SL/TP cocok dengan production sampai 6 desimal. Di 12 simbol selama Okt-2024 sampai Sep-2026 (n=7.576), edge gross hanya **+0,025R/trade (t 1,87)**, setara ±7–8 bps notional. Fee produksi 0,1% saja sudah membuatnya **−0,014R (t −1,0)**. Dengan biaya realistis (taker, slippage 2/10 bps, funding riil) hasilnya **−0,043 s.d. −0,057R/trade (t −3,2 s.d. −4,2)**, PF 0,92, max DD ~393–450R, daily Sharpe −1,7. Angka ini negatif di setiap half-year dan di in-sample maupun out-of-sample.

Track record live di dashboard (9 closed, win rate 44,4%, +0,63R, "+9,33% after fees") menyesatkan. Seluruh hasil positifnya berasal dari 3 baris legacy yang dientry di harga cross terinterpolasi, dengan fee 0%, dan dicatat 4–6 jam setelah cross. Saat itu harga sudah bergerak 0,63–0,82R ke arah sinyal. Enam trade yang berjalan dengan aturan v2.2 hasilnya **1W/5L, −3,88R**.

Satu-satunya varian yang menjanjikan adalah filter tren 1D EMA50 dengan exit di opposite cross (+0,054R, t 2,11). Varian ini tidak lolos koreksi multiple testing (sekitar 40 konfigurasi diuji, ambang Bonferroni t≈3,2). Edge-nya hilang kalau ada tambahan impact 10 bps (+0,015R, t 0,61) atau kalau 0,7% trade terbaiknya dibuang (−0,012R). Kesimpulan: ini signal logger yang dibangun dengan rapi, bukan strategi. Belum layak dapat modal, dan belum layak dipasarkan sebagai strategi.

---

## 2. Yang sudah bagus

- **Engine deterministik dan bebas look-ahead di jalur cron.** `include_current=False` (src/services/cron_scheduler.py:198-200). Candle yang masih terbentuk tidak pernah disimpan, diklaim, atau di-alert (src/main.py:801-804). Ids 5–15 bisa saya rekonstruksi dari kline mentah: entry, SL, TP, outcome, dan R cocok sampai 0,01R.
- **Entry v2.2 realistis**, yaitu open candle setelah cross (src/strategy/macd.py:152). Harga cross terinterpolasi yang lama memberi bias rata-rata **+0,128R/trade** (0,535% harga) di 7.576 trade. Perbaikan ini benar.
- **Aturan outcome konservatif.** Kalau SL dan TP tersentuh di candle yang sama, dihitung SL (src/strategy/outcome.py:39-42). Ini hanya berpengaruh pada 12 dari 7.576 trade (+0,004R kalau dibalik). Tidak ada gap-through stop (0 dari ~4.000 stop hit). Fallback stop 1,5% tidak pernah terpicu.
- **Unit yang benar.** Hasil dicatat dalam R dengan fee sudah dikurangkan (src/strategy/outcome.py:52-62).
- **Dedup idempoten** di level DB: unique index (symbol, side, timeframe, cross_candle_ms) ditambah `ON CONFLICT DO NOTHING` (src/services/db.py:215, 260-261, 311), dan tetap aman saat Redis gagal.
- **Capture lengkap dan latensi rendah.** Sejak deploy 10-13 ~10:05 UTC, cron menangkap 12/12 cross. `total_runs` cocok dengan setiap slot terjadwal, jadi tidak ada run yang terlewat. Sinyal masuk DB 15,7–21,3 detik setelah close.
- **Proxy Binance dilindungi** (rate-limit 60/menit, request coalescing, validasi exchangeInfo; src/main.py:350-366). Ada juga kejujuran intelektual: plan.md:7 sendiri mencatat edge turun dari +0,23R ke −0,05R. Jarang ada di tool retail.

---

## 3. Kritik utama

### (a) Edge & strategi

**A1. [Critical] Edge gross lebih kecil dari biaya eksekusi yang tidak bisa dihindari.**
Bukti: replika src/strategy/macd.py:81-107, 143-152 dan src/strategy/outcome.py:29-62, setiap cross ditradingkan seperti di production.

| Skenario biaya (12 simbol) | Window A: n=7.576 (sejak 2024-10-26) | Window B: n=7.450 (sejak 2024-11-11) |
|---|---|---|
| Gross (tanpa fee) | +0,025R, t 1,87, PF 1,05 | +0,023R, t 1,70 |
| **Prod 0,10% RT** | **−0,014R, t −1,0, PF 0,976, max DD 180R** | −0,016R, t −1,17, max DD 182R |
| Taker + slippage (2 bps major / 10 bps alt) di entry/SL/expiry, TP di harga limit + fee taker, funding riil | **−0,047R, t −3,5, PF 0,92, max DD ~393R** | −0,049R, t −3,6 |
| Sama, tapi TP terisi sebagai market (dengan slippage) | −0,055R, t −4,1, max DD ~450R | **−0,057R, t −4,24, max DD 445R** |
| Taker entry, maker TP 0,02% | −0,043R, t −3,2 | −0,045R |
| Fee VIP (0,03% taker / 0% maker), taker entry | −0,027R, t −2,0 | −0,030R |
| VIP + maker entry penuh di open (**tidak mungkin dicapai**) | +0,005R, t 0,3 | +0,003R |

Baris terakhir tidak realistis. Production baru tahu ada cross di close+15s (`CRON_SAFETY_DELAY_SECONDS=15`), jadi order maker tidak mungkin sudah menunggu di open.

| Metrik pendukung | Nilai |
|---|---|
| Bootstrap 95% CI mean R (prod / C), weekly-block | [−0,054, +0,030] / [−0,089, −0,003] |
| Daily R-sum Sharpe | −0,5 (prod), −1,7 (C) |
| Outcome mix | SL 52,7%, TP 35,6%, EXPIRED 11,8% (expired rata-rata +0,127R) |
| Median jarak stop / median hold | 3,14% / 12 bar (24 jam) |
| Biaya per trade dalam R | 0,039R (prod), 0,072–0,080R (realistis) |
| Half-year (prod / C) | 2024H2 −0,014/−0,038; 2025H1 −0,021/−0,052; 2025H2 −0,020/−0,052; 2026H1 −0,017/−0,055; 2026H2 (parsial) +0,025/−0,017 |
| Watchlist vs kontrol (C) | −0,031R (t −1,61) vs −0,063R (t −3,32) |
| Per simbol (prod) | ZEC +0,074, DOGE +0,073, XRP +0,018, AVAX +0,017, NEAR −0,003, ADA −0,021, LTC −0,032, ETH −0,036, BTC −0,044, SOL −0,047, LINK −0,055, BNB −0,101. Tidak ada \|t\| > 2,38. Kalau tidak ada edge sama sekali, t terbaik dari 12 simbol akan melampaui 2,64 dalam 5% kasus, jadi ZEC (t 1,71) masih noise. |

Kenapa penting: desk tidak memberi alokasi ke sinyal yang edge gross-nya di bawah fee floor. Size hanya menambah slippage, sehingga kerugian membesar seiring size. Dengan sizing compounding fixed-fractional pada biaya prod: risk 0,25% per trade menghasilkan MaxDD 37% (equity ×0,72), 0,5% menghasilkan 61% (×0,49), 1% menghasilkan 88% (×0,19). Periode underwater terpanjang 327 hari. Dengan biaya realistis (C), risk 0,25% menghasilkan DD sekitar 62%.

**A2. [High] Fixed target 1,5R memotong right tail. Satu-satunya perbaikan yang punya mekanisme masuk akal adalah trend-aligned entry dengan opposite-cross exit, dan itu pun belum signifikan.**

| Varian (skenario C) | In-sample avg R (t) | Out-of-sample avg R (t) | Catatan |
|---|---|---|---|
| Baseline production | −0,050 (−2,90) | −0,043 (−1,96) | |
| 2H EMA200 trend | −0,078 (−3,2) | +0,003 | tanda berbalik IS/OOS; engine B juga: −0,096 / −0,008 |
| 12H EMA50 / 1D EMA50 / BTC 1D regime | −0,076 / −0,060 / −0,039 | −0,003 / −0,022 / −0,021 | |
| Zero-line mean-reversion | −0,038 | −0,057 (−2,07) | lebih buruk |
| Volume > rata-rata 20 bar | −0,077 (−3,21) | −0,074 (−2,35) | konsisten memilih trade yang lebih buruk |
| ATR stop 1,5 / 2,0 / 2,5× | −0,060 / −0,016 / −0,002 | −0,050 / −0,044 / −0,044 | |
| Swing stop + floor 1,0 / 1,5 ATR | −0,047 / −0,041 | −0,043 / −0,042 | |
| RR 1,0 / 2,0 / 3,0 | −0,048 / −0,016 / +0,034 | −0,042 / −0,048 / −0,017 | pilihan walk-forward jujur (RR 3) gagal OOS |
| 50% di 1R + trail 3 ATR | −0,012 | −0,004 | |
| Opposite-cross exit | −0,003 | +0,057 (1,78) | |
| **1D EMA50 + opposite-cross exit** | **+0,030 (1,01)** | **+0,093 (2,02)** | full: +0,054R, t 2,11, PF 1,125, DD 110R, Sharpe ~0,8 |

Uji fragilitas varian terbaik:

| Uji | Hasil |
|---|---|
| Weekly-block CI OOS | [−0,081, +0,305], mencakup nol |
| Buang 2 trade terbaik per simbol (24 trade, 0,7%) | −0,012R |
| Tambahan impact 10 bps round-trip | +0,015R (t 0,61) |
| Top 2% trade | menyumbang 261% total R |
| In-sample tanpa 2024H2 (periode warm-up EMA50 harian, n 356, +0,128R) | ~+0,011R (2025H1 −0,033, 2025H2 +0,054) |
| LONG vs SHORT | +0,109 (t 2,48) vs +0,014 |
| Simbol negatif | NEAR −0,074, LTC −0,105, LINK −0,060 |
| Win rate | 31,4% |
| Power: σ(R)=1,50 | butuh ~3.600 trade untuk membuktikan +0,05R di t=2, ±4 tahun pada ~75 sinyal watchlist/bulan |

Varian ini juga dipilih setelah melihat hasil OOS, jadi t OOS-nya terkontaminasi. Kesimpulan: kandidat untuk shadow/paper trading dengan aturan yang di-pre-register, bukan kandidat modal. Di ukuran whale, seluruh edge-nya berada di dalam pita error eksekusi.

**A3. [Medium] Tuning stop/target di sekitar entry yang mendekati nol hanya memindahkan biaya.** Tabel di atas menunjukkan bahwa ATR stop, floor, RR, partial TP, ATR% filter, zero-line, dan volume tidak mengubah tanda hasil. Satu-satunya yang mengubah hasil adalah struktur exit. Kesimpulannya, sinyal entry MACD 12/26/9 di 2H hampir tidak membawa informasi.

**A4. [Low] Spesifikasi berbeda dari kode.** Swing window `df.iloc[idx-5 : idx+1]` berisi 6 candle termasuk candle cross (src/strategy/macd.py:89-90), sedangkan `SWING_LOOKBACK_CANDLES=5` (src/config/settings.py:59). Contoh live: stop ZEC #15 = 1272,22 = 1269,68 × 1,002 memakai high 04:00, yang tidak akan masuk kalau window-nya benar-benar 5 bar. Stop di bawah 1% (n=241, 3,2%) menghasilkan rata-rata −0,129R (prod) dan −0,198R (C). Fee 0,1% pada stop 0,93% (ETH #8) saja sudah 0,108R.

### (b) Eksekusi & market structure

**B1. [Critical] Tidak ada ruang untuk slippage.** Untuk follower yang mengeksekusi level SL/TP yang di-alert, setiap 10 bps slippage entry memakan ~0,037R: watchlist −0,040R di 10 bps dan −0,073R di 20 bps. 10 bps di entry ditambah 10 bps di stop menghasilkan −0,060R. Stop dibukukan tepat di harga stop (src/strategy/outcome.py:39-40), dan biaya hanya berupa fee flat (src/strategy/outcome.py:59). Spread, impact, dan slippage stop dianggap nol.

**B2. [Medium; bagi $50M+ bisa dianggap High] Kapasitas.** Median quote volume per bar 2H (12 bulan terakhir, hanya Binance):

| Simbol | Median vol 2H | Clip $8,33M (0,5% risk / stop 3%) sebagai % bar | Impact √-model untuk $20M (one-way) |
|---|---|---|---|
| BTC | $748,5M | 1,1% | 6 bps (0,03R) |
| ETH | $644,3M | 1,3% | 9 bps (0,03R) |
| SOL | $147,9M | 5,6% | 22 bps (0,07R) |
| XRP | $58,9M | 14% | 35 bps (0,12R) |
| ZEC | $56,3M | 15% | 60 bps (0,13R) |
| NEAR | $9,0M | **92%** | 112 bps (0,27R) |

Volume BTC berbeda 3,5× antar jam ($421M di candle 20:00 UTC vs $1.464M di 14:00 UTC). Untuk NEAR, bar p10 hanya $3,5M. `VOLUME_THRESHOLD_USD` (src/config/settings.py:66) hanya dipakai di fallback scan manual (src/main.py:745-757), tidak di watchlist cron. Catatan: kapasitas riil BTC/ETH lebih besar kalau di-route ke Bybit/OKX/CME. Untuk alt seperti NEAR, kapasitas ini tetap tidak memadai.

**B3. [Medium] Entry di open dan penempatan stop.** Buffer stop 0,2% hanya setara 0,08 (ZEC) sampai 0,23 (BTC) ATR14. Ada efek likuiditas yang robust: 49% stop berada dalam 0,15% dari equal low/high sebelumnya, dan stop seperti itu kena +3,9pp lebih sering setelah dikontrol jarak stop (t=3,6). Narasi "53% stop-out adalah wick sweep" **tidak lolos uji placebo**: level acak dengan jarak yang sama juga close kembali ke dalam 55,3% dari waktu. Jadi perbaiki penempatan stop sebagai hygiene, bukan sebagai sumber alpha. Buffer 0,5/1,0 ATR hanya menggeser watchlist ke +0,002/+0,014R, dan kontrol tetap negatif.

**B4. [Medium] Bias fill akibat latensi belum dikuantifikasi.** Cron berjalan di HH:00:15 UTC jam genap (src/services/cron_scheduler.py:41, 50-57), lalu fetch 500 kline per simbol, tetapi entry dibukukan di open yang persis. Estimasi random-walk pergerakan selama delay manusia 5/15 menit: BTC 11/19 bps, ZEC 38/65 bps. Pada edge gross ±7 bps, bias fill beberapa bps saja sudah menentukan tanda hasil. Narasi front-running/crowding bersifat spekulatif: drift rata-rata di candle entry +0,10 ± 1,45 bps, tidak terdeteksi. Lagi pula MACD 2H dihitung oleh semua platform charting.

**B5. [Low] Alert tidak punya pita harga.** Format alert (src/services/alerts.py:17-29) tidak memuat `valid_until`, harga maksimum yang masih layak di-fill, atau harga live. Alert catch-up (bars_ago>0) mencetak entry yang bisa sampai ±4 jam basi. Untuk 2H, detik tidak penting, tetapi alert tanpa pita mengundang chasing.

**B6. [Low] Trigger stop memakai last price, bukan mark price** (src/services/db.py:767-773), dan tidak ada `workingType` di seluruh src/. Ini catatan fidelity backtest, belum risiko live karena belum ada OMS. Funding tidak material: rata-rata −0,0004 s.d. −0,0008R/trade, dengan ekstrem ZEC −0,399%/8h dan SOL −0,303%/8h.

### (c) Risk & portfolio

**C1. [High] Tidak ada position model.** Long dan short di simbol yang sama bisa terbuka bersamaan. Dedup hanya pada (symbol, side, tf, cross_candle_ms) (src/services/db.py:215, 311), dan `save_macd_signal` (src/services/db.py:548-572) hanya melakukan insert. Di backtest, **69,2%** sinyal terbuka saat sinyal lain di simbol yang sama masih hidup, **60,4%** terbuka melawan sinyal berlawanan yang masih terbuka, dengan puncak 46 posisi bersamaan. Contoh live: BTC #4 LONG (83.691,98) dan #11 SHORT (82.282,30) sama-sama PENDING. ETH #8 LONG di-stop di dalam candle 10:00 tepat ketika ETH #9 SHORT masuk di open-nya. Kalau akun one-way, #8 seharusnya tertutup di 2.495,86 = −0,82R, bukan −1,11R seperti yang dibukukan. Ledger ini tidak bisa direkonsiliasi dengan akun mana pun.
Catatan penting: MACD cross selalu berselang-seling arah, jadi stop-and-reverse sama dengan close di opposite cross. Hasil uji: SAR −0,0008R (watchlist), one-per-symbol 0,000R (n=1.715). Netting adalah kontrol exposure dan margin, bukan sumber edge.

**C2. [High] Korelasi tinggi: 6 simbol ≈ 2 taruhan.** Korelasi return 2H: BTC-ETH 0,82 (2 tahun) / 0,89 (6 bulan terakhir), ETH-SOL 0,80, rata-rata off-diagonal 0,63. PC1 menjelaskan 69,6% variance, N_eff ≈ 1,9–1,97. Beta terhadap BTC: ETH 1,22, SOL 1,34, XRP 1,28, NEAR 1,46, ZEC 1,32. 21,5% sinyal watchlist muncul di bar dan sisi yang sama dengan ≥2 simbol lain, dan 49% dengan ≥1 simbol lain. Contoh live: tiga win legacy adalah short ETH/SOL/ZEC yang muncul bersamaan, lalu lima long (ETH/SOL/NEAR/XRP/ZEC) kena stop di candle 10:00–14:00 UTC 14-Okt dengan total −5,32R. Angka itu persis max DD dashboard.

**C3. [High] Tidak ada sizing dan tidak ada cap portfolio.** Isi src/config/settings.py:58-63 hanya RR, swing lookback, expiry, dan fee. Simulasi MTM watchlist pada risk 1% tanpa cap menghasilkan CAGR −22,7%, MaxDD −68,3%, gross leverage p95 5,2× dan maks 19,1×, dengan 27 posisi bersamaan. Stop BTC p5 0,74% berarti notional $67,6M (1,35× NAV) untuk satu trade. Konfigurasi one-per-symbol + maks 3 posisi + maks 2 searah, dengan tie-break acak (4 seed), menghasilkan CAGR +10,5% s.d. +35,9%, MaxDD −18,3% s.d. −27,2%. Konfigurasi yang sama di basket kontrol menghasilkan **−7,5% s.d. +2,7%**. Cap ini adalah risk control, bukan alpha, dan jangan dikutip sebagai bukti edge.

**C4. [Medium] Tail.** Pada 1R per trade tanpa cap (watchlist): bar 2H terburuk −6,51R, hari terburuk −10,25R (43 dari 726 hari ≤ −5R), minggu terburuk −18,5R, losing streak 20 (23–24 kalau 12 simbol). Tidak ada kill switch, daily loss limit, atau halt saat data basi (grep: 0 hit). Error evaluasi per simbol ditelan begitu saja (src/services/db.py:820-821). Range 2H terbesar: XRP 56,7%, NEAR 48,7%, BTC 13,4% (2025-10-10 20:00 UTC). Overshoot ekstrem bar di luar stop (p90 0,82R, p99 2,13R) adalah batas atas yang longgar, bukan slippage riil.

**C5. [Low] Expiry 48 candle.** Trade dengan stop 3–4 ATR expire 27,1% dari waktu, dan stop >4 ATR expire 41,9%. Trade expired rata-rata justru +0,135R, jadi time stop yang lebih pendek harus diuji walk-forward, bukan langsung diterapkan.

### (d) Integritas pengukuran & track record live

**D1. [Critical] Headline P&L sepenuhnya dibangun dari baris legacy.**

| | Dashboard | Faktanya |
|---|---|---|
| Ids 1–3 (ETH/SOL/ZEC SHORT) | +1,50R × 3 = +4,50R, +17,55% | entry = cross_price terinterpolasi (2.526,24 / 110,065 / 1.261,16) vs open riil (2.510,85 / 108,85 / 1.237,65); `fee_percent` 0,0; sinyal dibuat 22:16:41 UTC (bukan slot cron, bars_ago 2/3/2) |
| R yang sudah lewat sebelum publikasi (open 22:00) | — | +0,63 / +0,82 / +0,69R = **2,14R** |
| Entry di open candle publikasi, SL/TP tetap | — | +0,505 / +0,357 / +0,470 = +1,33R |
| Aturan v2.2 diturunkan ulang dari entry 22:00 | — | ketiganya masih OPEN (MTM +0,60/+0,51/+0,46R) |
| Trade v2.2 (ids 5–10) | — | **1W/5L, −3,88R, rata-rata −0,647R** |
| Total 9 trade | +0,63R, 44,4%, +9,33% | **≈ −2,55R** point-in-time (atau −3,88R kalau legacy dianggap masih terbuka) |

Asal-usul: baris P/L 1–3 ditulis oleh evaluator pra-v2.2 (format exit_time `'2026-10-13 00:00:00'`, git `1a2c024^`). Evaluator itu memeriksa TP sebelum SL, perbedaan aturan kedua yang tidak terdokumentasi. `fee_percent=0.0` berasal dari default ADD COLUMN (src/services/db.py:272), dan R diturunkan dari pnl% bebas fee (src/services/db.py:270-279). Jalur backfill juga meng-hard-code fee 0,0 (src/services/db.py:346) dan fallback `entry_price or cross_price` (src/services/db.py:345). plan.md:30 sengaja mempertahankan nilai lama. Label "after fees" di src/static/app.js:409 salah.

**Bom waktu:** BTC #4 (PENDING, dibuat cron 2026-10-13 08:00:15 sebelum deploy) masih memakai entry interpolasi 83.691,98, sedangkan open riil 83.829,20 (±0,05R lebih bagus dari yang bisa dicapai). Saat closed, src/services/db.py:792 (`entry_val = rec.entry_price`) akan membukukannya dengan entry fiktif tetapi dengan fee 0,1%, jadi varian aturan ketiga.

Kenapa penting: allocator merekonsiliasi headline terhadap fill yang bisa ditradingkan. Mencampur versi aturan dalam satu headline cukup untuk membuat seluruh track record dibuang.

**D2. [High] Hindsight booking di jalur catch-up dan manual scan.** Entry selalu `df.iloc[i+1].open` berapa pun bars_ago-nya (src/strategy/macd.py:152, 178). Evaluator mengambil kline dari `cross_candle_ms` dan me-resolve mulai i+1 (src/services/db.py:771, 784). Untuk bars_ago=k, ada k candle yang sudah close sebelum sinyal terdeteksi tetapi ikut dihitung. Manual scan menerima lookback sampai 20 (src/main.py:717-719), default UI 5 bar (src/static/index.html:762), `save_db`/`send_telegram` default True (src/main.py:726-731), dan langsung menjadwalkan `_evaluate_and_notify` (src/main.py:877). Akibatnya satu klik bisa menyimpan cross berumur 10 jam dan menutupnya dalam hitungan detik dengan data dari sebelum publikasi. Bias ini tidak acak karena drift pasca-cross searah sinyal. Di jalur cron (`CRON_LOOKBACK_CANDLES=3`, src/config/settings.py:73) masalah ini laten, karena semua 11 baris cron pasca-deploy punya bars_ago=0. Ada pula channel survivorship: `detect_macd_cross` hanya mengembalikan cross terbaru di window lookback (src/strategy/macd.py:131-181), sehingga setelah downtime, cross yang diikuti reversal hilang permanen.

**D3. [Medium] Sampel live tidak bermakna, tetapi tampil seolah bermakna.** Data 9 trade: mean +0,069R, SD 1,34, t 0,15, 95% CI [−0,96, +1,10]. v2.2 saja: CI [−1,72, +0,43]. Break-even win rate dengan fee ±42%, dan P(≥3/6 win | p=0,40) = 0,46. σ(R)=1,17 berarti membedakan +0,10R dari nol butuh ~1.074 trade (~2.040 efektif dengan design effect 1,9 dari clustering), dan +0,05R butuh ~4.300. Tile dashboard (src/static/app.js:388-418) tidak menampilkan n, CI, maupun versi aturan. `avg_rr: 1.5` di-hard-code (src/services/db.py:987, 1087). Itu target, bukan hasil realisasi.

**D4. [Medium] KPI bukan metrik portfolio.**
- `total_net_pnl = SUM(pnl_percent)` (src/services/db.py:1024-1029) menjumlahkan trade dengan stop 0,93% sampai 11,24% (spread 12×). Di arsip, cara ini menghasilkan +45,7% padahal −13,2R, jadi tandanya berlawanan.
- Win rate menghitung expired sebagai kalah (src/services/db.py:1019-1020), padahal expired rata-rata +0,135R.
- `_max_drawdown` (src/services/db.py:897-904) hanya memakai trade closed dan diurutkan berdasarkan string `exit_time` dengan format campuran (src/services/db.py:1036-1038). Tiga exit di candle 14:00 memberi DD 5,31R dalam urutan tersimpan, atau 3,88R kalau win diurutkan lebih dulu.
- Win rate diambil dari `macd_signals.win_status`, sedangkan R dan % diambil dari `profitloss_history`. Dua sumber ini bisa diam-diam tidak sinkron.

**D5. [Medium] Ledger bisa diedit dan tanpa provenance.** POST `/api/profitloss-history` (src/main.py:683-702 → src/services/db.py:840-881) menerima `result`/`pnl_percent` bebas. Baris manual yang ditulis sebelum evaluator mendahului hasil evaluator (unique signal_id + `ON CONFLICT DO NOTHING`), sementara `win_status` tetap ditulis dari outcome riil. R baris manual awalnya NULL, sehingga keluar dari total R. Pada restart berikutnya, `PROFITLOSS_R_MIGRATIONS` (src/services/db.py:270-279) menurunkan R dari pnl% kiriman klien dengan fee 0. Tidak ada kolom versi, params, sumber (cron/manual), atau git SHA. `created_at` memakai `NOW()` Postgres, yaitu waktu mulai transaksi (src/services/db.py:63, 110, 163), sehingga audit "kapan ini diketahui" tidak bisa diandalkan. Counter cron hanya di memori (src/services/cron_scheduler.py:73-78). Endpoint POST butuh API key, jadi ini celah governance, bukan pemalsuan yang terbukti.

**D6. [Low] Bug filter tanggal.** `end_date` membuang seluruh hari terakhir karena perbandingan `'T'` > `' '` (src/services/db.py:649, 937; hanya via API). Filter prefix memakai tanggal UTC sementara UI menampilkan WIB (src/static/app.js:435 vs 129-134). Waktu "Crossed" adalah open candle cross (src/strategy/macd.py:168), padahal sinyal baru tradable 2 jam kemudian.

### (e) Produk, ops & keamanan

**E1. [Medium] Sinyal tidak actionable untuk PM.** Tidak ada size, stop dalam kelipatan ATR, harga live, jarak dari entry dalam R, waktu sampai expiry, kapasitas, atau konteks portfolio. Nilai MACD mentah tidak berguna bagi trader. Catatan: menambah OI, L/S ratio, dan "quality score" di atas sinyal ber-expectancy ~0R hanya kosmetik. Prioritas ada di edge dulu.

**E2. [Medium] Keamanan.**
- Semua GET publik tanpa key: `/api/signals`, `/api/journal`, `/api/profitloss-history`, `/api/summary`, `/api/cron/status`, dan lainnya (src/main.py:319, 492, 501, 600, 618, 649, 907, 943).
- `/docs` dan `/openapi.json` mengembalikan 200 (src/main.py:116-121).
- Tidak ada CSP dan HSTS (headers.txt). lightweight-charts dimuat dari unpkg tanpa SRI (src/static/app.js:7; versi di-pin 4.2.1).
- Satu shared admin key disimpan di localStorage lewat `prompt()` (src/static/app.js:221-252) dan sekaligus membuka akses edit P&L, watchlist, cron trigger, dan scan.
- Kunci Binance di-load di proses web (src/services/binance_client.py:18-43) padahal semua panggilannya publik.
- ACAO `*` sendiri tidak menambah exposure karena auth via header dan `allow_credentials=False` (src/main.py:127).

Kalau suatu saat flow desk mengikuti sinyal ini, feed publik SL/TP berarti order flow Anda terbuka untuk umum.

**E3. [Medium] Observability.** Status `/api/health` hanya bergantung pada DB dan Redis (src/main.py:303-315). Cron yang macet tetap "healthy" (HEALTHCHECK di Dockerfile:26-27). Fetch yang gagal di cron hanya mengembalikan None tanpa tracking (src/services/cron_scheduler.py:191-193). Telegram dikirim fire-and-forget (src/services/cron_scheduler.py:244), image `:latest` (docker-compose.prod.yml:3), dan scheduler jalan di dalam proses API (Dockerfile:29). Saat ini tidak ada bukti run yang terlewat, tetapi kalau terjadi, tidak akan ada yang tahu.

**E4. [Low] DRY_RUN hanya label** (src/config/settings.py:10, satu-satunya pemakaian di src/main.py:291). Tidak ada kode order/OMS. Mengubahnya menjadi "PRODUCTION" menyesatkan. Evaluasi SL/TP hanya tiap 2 jam (lag 0–2 jam). Ini tidak memengaruhi P&L bagi siapa pun yang memasang bracket order, tetapi memengaruhi notifikasi dan timestamp ledger.

---

## 4. Rekomendasi perbaikan berprioritas

| Prio | Item | Detail konkret / parameter | Lokasi kode | Effort | Dampak |
|---|---|---|---|---|---|
| P0 | Relabel produk | Badge "Research, belum ada edge tervalidasi". Hentikan klaim performa. Ganti mode menjadi `SIGNALS_ONLY`. | src/static/index.html, src/main.py:291, src/config/settings.py:10 | S | Menghentikan keputusan sizing di atas noise |
| P0 | Bersihkan ledger | Kolom `strategy_version`, `source` (cron_live/manual/catchup/legacy), `entry_basis`. Tandai ids 1–4 sebagai legacy. Restate dengan aturan v2.2 (re-derive SL/TP, re-run outcome). **Perbaiki BTC #4 sebelum closed.** Hapus fallback `or rec.cross_price` dan fee 0,0. Tambah test yang gagal kalau ada baris closed dengan fee 0. | src/services/db.py:270-279, 335-364 (345-346), 792, 1021-1041 | S | Headline jadi jujur (≈ −2,55R, bukan +0,63R) |
| P0 | Tutup hindsight booking | Simpan `detected_at_ms` (pakai `clock_timestamp()`/waktu aplikasi). Kalau bars_ago>0, beri status LATE dan keluarkan dari performa, atau entry di harga saat terdeteksi dan tolak kalau harga sudah lewat 0,5R atau SL. Manual scan: jadikan POST, `save_db`/`send_telegram` default False, tanpa auto-evaluate, `limit ≥ 300`, simbol harus watchlist. | src/strategy/macd.py:152, src/main.py:714-731, 877, src/services/db.py:771, 784 | S–M | Menghapus channel look-ahead |
| P0 | Backtest harness + gate | Pakai ulang `compute_macd`, `calculate_trade_levels`, `resolve_outcome`. Laporkan gross R, net R, t, weekly-block CI, IS/OOS, dan basket kontrol untuk tiap release. Blokir deploy yang menurunkan net R. Jumlah konfigurasi yang diuji dicatat (deflated t). | baru: `src/backtest/`, tests/ | M | Setiap perubahan strategi bisa diaudit (<1 menit di arsip) |
| P0 | Model biaya realistis | `SLIPPAGE_BPS` per simbol (BTC/ETH 2–3, SOL 8, XRP 10, ZEC 20, NEAR 25; stop ×1,5). Fee maker/taker terpisah. Funding riil. Simpan `gross_r`, `cost_r`, `net_r`. | src/strategy/outcome.py:39-40, 52-62, src/config/settings.py:63, src/services/db.py:792 | S | Angka dashboard mendekati yang bisa dieksekusi |
| P0 | KPI statistik jujur | Tampilkan n, mean R ± 95% CI, break-even WR ±42%. Sembunyikan total sampai n ≥ 100 per versi. `avg_rr` diganti nama menjadi `target_rr`. Win rate = TP/closed, expired ditampilkan terpisah. Hapus tile "% summed". | src/services/db.py:987, 1019-1029, 1087, src/static/app.js:388-418 | S | Mencegah anchoring pada 6–9 trade |
| P1 | Position model | Satu posisi per simbol (tabel `positions`, one-way). Opposite cross menutup posisi di next open (`REVERSED`), atau skip. Sebutkan bahwa ini kontrol exposure, bukan edge (SAR −0,0008R). | src/services/cron_scheduler.py:207-212, src/services/db.py:548-572 | M | Ledger bisa direkonsiliasi dengan akun riil |
| P1 | Risk layer portfolio | Risk 0,25% NAV (maks 0,5% setelah terbukti). Sizing dari max(stop, 1,0×ATR14). Notional ≤ min(1–2% median vol 2H 30 hari, 0,5×NAV/β). Maks 3 posisi, maks 2 searah di cluster BTC-beta, net β-adjusted ≤1,0× NAV, gross ≤2× NAV, total open risk ≤2% NAV. | modul baru `src/risk/`, src/services/alerts.py:17-29 | M | MaxDD turun drastis (−68% ke ±−18–27%), bukan alpha |
| P1 | Shadow strategy pre-registered | 1D EMA50 (hanya bar harian yang sudah close) searah cross. Tanpa fixed TP. Exit di next open setelah opposite cross 2H, atau swing stop. Maks 96 bar. **Kunci parameter sekarang.** Kriteria lulus dengan power realistis (σ 1,5R): block-bootstrap per hari, minimal 12 bulan, target t ≥ 2 setelah biaya realistis termasuk impact. | src/strategy/macd.py, src/strategy/outcome.py (exit mode) | M | Satu-satunya jalur yang mungkin menuju edge |
| P1 | Event ledger append-only | Tabel `signal_events` (detected/resolved/restated) dengan ts server, params hash, git SHA, SHA-256 candle input, hash-chain, dan head yang dipublikasikan harian. Tabel `cron_runs`. Pindahkan migrasi boot ke Alembic. Hapus POST profitloss atau pindahkan ke tabel `adjustments` yang terpisah dari summary. | src/services/db.py:228-279, 393-425, 840-881, src/main.py:683-702 | M | Track record yang tahan due diligence |
| P1 | Ops | Health "degraded" kalau `now − last_success > 2h+5m`. Dead-man switch (Healthchecks/Cronitor). Outbox Telegram dengan retry. Alert ops saat fetch simbol gagal. Pin image by digest. | src/main.py:303-315, src/services/cron_scheduler.py:73-78, 191-193, 244, docker-compose.prod.yml:3 | M | Kegagalan yang diam-diam jadi terlihat |
| P1 | Keamanan | Auth di semua `/api/*` kecuali `/api/health` minimal. Token per user dengan scope. Allowlist CORS. `docs_url/openapi_url=None`. CSP + HSTS di nginx. SRI atau self-host chart lib. Cookie HttpOnly sebagai pengganti localStorage. Hapus kunci Binance dari proses web. | src/main.py:116-130, src/static/app.js:7, 221-252, src/services/binance_client.py:18-43 | S | Menutup permukaan serangan dan kebocoran flow |
| P1 | Kill switch | Halt entry baru pada daily −2R atau −1% NAV dan weekly −5R. Shadow mode kalau expectancy rolling 50 trade < −0,10R. Data kill kalau kline lebih tua dari 1 bar atau error evaluator > 0. **Hanya setelah cap P1 aktif.** | modul `src/risk/`, src/services/db.py:820-821 | M | Melindungi dari regime break dan bug feed |
| P1 | Isi alert | `valid_until` (+15 menit), "jangan fill lebih buruk dari entry ± 0,15R", harga live saat kirim, signal_id. Suppress alert kalau bars_ago>0. Simpan `alert_sent_ms` dan harga saat kirim. | src/services/alerts.py:17-29, src/services/cron_scheduler.py:241-244 | S | Implementation shortfall bisa diaudit |
| P2 | Hygiene stop | Buffer = max(0,5×ATR14, 0,3%). Perbaiki window ke `idx-4` atau dokumentasikan "6 candle". Skip stop < 1% atau < 0,5 ATR. Perlakukan sebagai hygiene, bukan edge. | src/strategy/macd.py:89-105, src/config/settings.py:59 | S | Kecil (stop <1%: −0,13 s.d. −0,20R) |
| P2 | Resolusi candle ambigu | Kalau SL dan TP tersentuh di candle 2H yang sama, ambil 120 kline 1m dan settle sesuai urutan riil. Fallback SL-first. Tandai `resolved_by=1m`. | src/services/db.py:780-797 | S | Contoh NEAR #10: ayunan 2,5R pada sampel kecil |
| P2 | Pengumpulan data | Kline 1m, aggTrades ±30 menit di sekitar sinyal, snapshot bookDepth, markPriceKlines. Journal otomatis mencatat fill. Implementation shortfall mingguan. | baru: job archiver, `trade_journal` | M | Prasyarat semua klaim eksekusi |
| P2 | Universe | Aturan likuiditas yang di-pre-register (top-N per median vol 30 hari, listing > 90 hari), tier kapasitas, perubahan dicatat sebagai event. Turunkan NEAR ke 0,25× atau keluarkan. Basket kontrol tetap sebagai benchmark. | src/services/cron_scheduler.py:181-182, src/config/settings.py:66 | M | Mengurangi bias pemilihan dan masalah kapasitas |
| P2 | Normalisasi waktu | `timestamptz` atau epoch ms. Filter half-open dengan konversi WIB ke UTC di server. Label "konfirmasi" vs "cross". | src/services/db.py:645-649, 933-937, 1036-1038, src/static/app.js:435 | S | Angka periodik benar |
| P2 | OMS / eksekusi | Service terpisah: signal bus versioned, idempotent `newClientOrderId`, entry pasif lalu TWAP/POV ≤5–10% volume, STOP_MARKET reduceOnly `workingType=MARK_PRICE`, reconciler userData. **Hanya setelah shadow lolos gate.** | baru | L | Saat ini kontraproduktif: mengotomatiskan edge ~0R |

---

## 5. Roadmap 30/60/90 hari

**Hari 0–30: integritas dulu, baru strategi**
- Relabel dashboard. Migrasi ledger (version/source/entry_basis), restate ids 1–4, perbaiki BTC #4 sebelum closed.
- Tutup hindsight booking (LATE flag, manual scan non-persisten secara default, `detected_at_ms`).
- Backtest harness masuk repo, dengan CI yang mereproduksi baseline (−0,014R prod / −0,047 s.d. −0,057R realistis) sebagai regression test.
- Model biaya per simbol dan kolom gross/cost/net R.
- KPI dengan n dan CI. Hapus % summed.
- **Pre-register** shadow strategy (1D EMA50 + opposite-cross exit, parameter terkunci, kriteria lulus/gagal ditulis di repo dengan hash commit).
- Quick wins keamanan: auth GET, matikan docs, CSP/HSTS/SRI, hapus kunci Binance dari web.

**Hari 31–60: book yang bisa direkonsiliasi**
- Position model (one-per-symbol atau SAR) dan risk layer (sizing ATR-floor, cap kapasitas, cap searah dan beta, gross ≤2×).
- Shadow strategy mulai berjalan berdampingan dengan production, dengan ledger terpisah.
- **Holdout yang bersih:** jalankan aturan pre-registered sekali saja di data Binance yang belum pernah disentuh (2020–2024-09) dan di basket kontrol. Ini satu-satunya out-of-sample yang belum terkontaminasi oleh ~40 konfigurasi yang sudah diuji. Kalau gagal di sini, hentikan jalur ini.
- Event ledger append-only, tabel `cron_runs`, dead-man switch, Telegram outbox.
- Archiver data mikro (1m, aggTrades, bookDepth, mark price), dan isi alert dengan `valid_until` serta pita fill.

**Hari 61–90: keputusan go/no-go berbasis bukti**
- Review interim shadow (catatan: 2–3 bulan **tidak cukup** untuk membuktikan +0,05R, karena butuh ~3.600 trade). Yang dinilai: konsistensi tanda dengan holdout, slippage riil versus model, dan tidak ada pelanggaran risk cap.
- Kalibrasi ulang slippage/impact dari aggTrades dan depth. Ulang uji varian dengan model fill TWAP 10–30 menit di open.
- **Gate untuk pilot uang riil (clip kecil, BTC/ETH saja):** (1) holdout 2020–2024 positif setelah biaya realistis, t ≥ 2 dengan block bootstrap; (2) tetap positif dengan impact +10 bps; (3) tidak bergantung pada <1% trade terbaik; (4) shadow live tidak menunjukkan divergensi signifikan. Kalau salah satu gagal, Crossline tetap jadi produk alert/riset, dan OMS tidak dibangun.

---

## 6. Catatan metodologi & keterbatasan

- **Data:** kline Binance USDⓈ-M 2H, 2024-10-01 s.d. 2026-09-30 (8.760 bar per simbol), 6 simbol watchlist dan 6 kontrol (BNB, DOGE, ADA, LINK, AVAX, LTC). Funding 8h riil hanya tersedia untuk 6 simbol watchlist (2025-10 s.d. 2026-09). Di luar periode itu dipakai asumsi 0,01%/8h. Snapshot production: 15:52 UTC (6 closed), re-GET 16:06–16:08 UTC (9 closed). Source code dari `origin/master`, aset statis identik byte-per-byte dengan production. fapi.binance.com tidak bisa dijangkau. Tidak ada endpoint production yang mengubah state yang dipanggil.
- **Replikasi:** tiga engine independen (EMA ber-seed SMA gaya pandas-ta, cross di bar close, entry di open i+1, swing 6 candle ±0,2%, TP 1,5R, SL-first, expiry 48 bar, fee 0,1%). Engine-engine ini cocok trade-per-trade. Selisih headline (n 7.576 vs 7.450) hanya karena window warm-up (300 vs 500 bar) dan perlakuan 30 trade di ujung data. Persentase overlap 54%/45% di salah satu engine adalah bug penghitungan; angka yang benar 69,2%/60,4%.
- **Asumsi biaya:** fee taker 0,05%/sisi; slippage 2 bps (BTC/ETH/SOL/XRP/BNB/DOGE) dan 10 bps (alt lain) per sisi; impact memakai model akar kuadrat (Y=0,7), jadi dimodelkan, bukan diukur. Tier MMR untuk analisis likuidasi adalah asumsi. Volume hanya dari Binance, sehingga kapasitas major yang sebenarnya lebih besar lintas venue.
- **Yang tidak bisa diverifikasi:** tidak ada data 1m, aggTrades, orderbook/depth, atau mark-price kline, sehingga urutan sentuhan intrabar, slippage stop riil, dan crowding di open tidak bisa diukur. Latensi cron (close+15s ditambah fetch) tidak dimodelkan. Tidak diketahui apakah kunci Binance di production punya izin trading. Journal fill kosong, jadi tidak ada implementation shortfall riil.
- **Multiple testing:** ±40 konfigurasi diuji pada arsip yang sama. Semua angka varian harus dibaca dengan ambang t≈3,2. Hasil portfolio ber-cap bergantung pada tie-break sinyal di bar yang sama, jadi disajikan sebagai rentang.
- **Statistik live:** dengan n=6–9 dan σ(R)≈1,2–1,5, track record live tidak bisa memvalidasi atau memfalsifikasi edge apa pun dalam horizon kurang dari 1 tahun. Keputusan harus bertumpu pada arsip dan holdout, bukan pada dashboard.
- **Spekulasi yang diberi label:** penjelasan bahwa filter volume memilih "cross klimaks yang terlambat", serta narasi front-running/stop-hunting, belum terbukti. Uji placebo justru membantah narasi wick-sweep.