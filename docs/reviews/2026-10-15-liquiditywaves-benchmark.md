# Crossline vs Liquidity Waves: benchmark & roadmap

**Untuk:** Owner Crossline
**Dari:** Trader crypto-futures (desk institusional) yang juga memahami produk
**Tanggal:** 2026-10-15
**Objek:** liquiditywaves.com (LW, publik, tanpa login) dibandingkan Crossline production (`https://macd-scan.bantubuat.web.id/`, source `prod_src/`)
**Catatan lanjutan dari:** review institusional 2026-10-14 (`/home/farghani/Project/ema_scanner/docs/reviews/2026-10-14-institutional-review.md`, sudah tercatat di repo, status untracked)

---

## 1. Ringkasan

1. LW pada dasarnya bisnis distribusi dan monetisasi, bukan mesin quant yang edge-nya sudah terbukti. Bisnisnya terdiri dari grup VIP Telegram berisi 4–6 analis SMC, bot scalper M1 "Liquidity AI", copy-execution ke Bitunix, funnel referral Bitunix, program affiliate/KOL, white-label API, dan ratusan halaman SEO.
2. Orang percaya dan mau membayar karena presentasinya sangat rapi. Ada permalink per sinyal dengan judul berbasis R ("PIEVERSE LONG · +4R"), slider chart before/after, status lifecycle yang jelas, toggle 7/30/90/365, status page, changelog, dan klaim "100% Win Rate · +1.28R · 70 setup".
3. Klaim 100% itu berasal dari showcase kurasi. Seluruh 70 entry `/signals/` di sitemap positif (min +0,51R, mean 1,2843 → "+1.28R"), dan semuanya diunggah dalam satu batch tanggal 6–11 Mei 2026, lalu tidak pernah diperbarui selama lima bulan.
4. Di halaman `/stats` milik LW sendiri, 30 hari terakhir tercatat 11W/10L. Angka "Hit Rate 74,4%" di sana adalah jumlah *event* TP dibagi (TP+SL) = 29/39, bukan per trade. "Net +19R" = 29 − 10, artinya setiap level TP yang tersentuh dikreditkan +1R penuh tanpa fee.
5. Ledger 365 hari LW, kalau dihitung jujur, memberi win rate per trade 52,4–54,6%. Untuk subscriber yang keluar penuh di TP1, expectancy-nya sekitar +0,09R per trade sebelum fee (CI 0,01–0,18), atau +0,17R kalau TP1 = 1,14R. Itu wilayah yang sama dengan Crossline: tipis, dan belum tentu positif setelah biaya.
6. Jawaban jujur atas "saya ingin seperti ini": **shell produknya bisa dan layak ditiru**, yaitu halaman, lifecycle, permalink, status, changelog, dan bot. **Klaim dan cara menghitungnya jangan ditiru.** Mendahulukan sisi distribusi dan monetisasi sebelum ada edge sama saja menjual strategi ~0R ke orang lain.
7. Win rate 44% bukan masalahnya. Pada target 1:1,5 dengan fee, break-even WR sekitar 42%. Yang jadi masalah adalah expectancy setelah biaya: −0,014R di fee produksi dan −0,043 sampai −0,057R di biaya realistis (n=7.576).
8. Angka 44,4% / +0,63R di dashboard juga terlalu bagus. Tiga dari empat winner adalah baris legacy dengan fee 0. Dengan aturan v2.2 saja hasilnya 1W/5L, mean −0,647R, 95% CI [−1,72; +0,43], Σ −3,88R.
9. Backtest cepat di data Crossline membuktikan bahwa win rate bisa direkayasa. Memperkecil TP menaikkan WR 12–13 poin dan justru memperburuk expectancy. Pembukuan ala LW mengubah hasil realized −0,271R/trade menjadi "+0,092R".
10. Rekomendasi: jadikan Crossline **research ledger publik yang jujur dengan presentasi selevel LW**. Produk berbayar dan auto-trading baru dibuka setelah edge lolos holdout OOS dan ada legal sign-off.

---

## 2. Anatomi Liquidity Waves

Legenda: **EDGE** = bisa mengubah expectancy. **COST** = menurunkan biaya/varians eksekusi. **RISK** = kontrol survival, bukan alpha. **PRES** = presentasi/trust. **DIST** = distribusi/monetisasi.

| Kategori | Fitur LW | Bukti (file: kutipan) | Klasifikasi | Penilaian |
|---|---|---|---|---|
| Engine | Analis VIP (SMC/ICT, M15–4H) posting ke Telegram, lalu auto-captured | home.txt: "Analyst publish setup ke channel premium · auto-captured radar"; services.txt: "Sinyal SMC/ICT real-time dari 6 analis" | Klaim EDGE, tidak terbukti | Ledger publik `/stats`: 365d WR per trade 52–55% |
| Engine | Liquidity AI / ScalpAuto M1, 5 strategi: rsiBollinger, meanReversion, sweepReversal, breakout, vwapReversion | 56/56 kartu AI = M1 (`work_lw_method/tr.py`); home "Lima strategi" vs services "Sembilan strategi" | Klaim EDGE, tidak terbukti | Tidak ada agregat AI yang dipublikasikan; yang tampil hanya winner. Di M1, fee 0,1% RT terhadap stop 0,3% ≈ 0,33R |
| Engine | Tier S/A+/A/B/C | docs.txt: "A — high conviction… RR > 1:3; B ~1:2"; S dan A+ tidak didefinisikan | PRES | Rata-rata RR kartu AI per tier: S 0,97 (n=21), A+ 1,26 (16), B 1,33 (19), jadi tier tidak mengurutkan hasil |
| Engine | Lifecycle PENDING (MENUNGGU) → ENTERED (BERJALAN) → COMPLETED/SL HIT, atau CANCELLED (BATAL, 0R); SL ke BE setelah TP1 | api-docs.txt "Daftar Event"; docs.txt: "setelah TP1 hit, SL otomatis dipindah ke breakeven" | COST (limit entry) + PRES | Polanya layak ditiru. CANCELLED dihitung 0R dan tidak masuk WR |
| Data | "5 sumber intel" (Nansen, Arkham, Glassnode, DefiLlama, TradingView), "auto-reject low-quality" | Arkham/Glassnode/DefiLlama hanya muncul di logo home; daftar ini editable lewat CMS (changelog: "/me/admin/landing CMS — partners, dataSources") | PRES | Yang tampak benar-benar dipakai hanya Nansen (`/me/onchain`) dan data derivatif ala Coinglass. Tidak ada ablation |
| Data | Wave Radar `/radar/<COIN>`: 12 modul, AI Eval Claude Sonnet 4.6 | radar.txt: "Sonnet 4.6 baca chart lo… verdict probabilistic" | DIST (lead magnet) | Tidak ada evaluasi akurasi |
| Execution | Autopilot: limit order di akun Bitunix member <3 dtk, multi-TP, mode Combo/Ride/Exit | services_autopilot.txt: "limit order di akun Bitunix Anda" | COST | Satu-satunya venue yang terdokumentasi adalah Bitunix, padahal diklaim "5 Bursa Native" (daftar bursanya pun beda antar-section; Bybit `"active":false` di CMS) |
| Execution | Atomic claim dengan deterministic order ID, phantom-position guard, DailyLoss claim-at-fill, idempotent recovery | services_autopilot.txt: "Atomic claim anti double-order… Phantom position guard" | RISK/COST | Engineering yang layak ditiru kalau suatu saat ada eksekusi |
| Execution | "<200 ms smart routing" | home.txt vs services_autopilot "<3s Waktu Eksekusi" | PRES | Kontradiktif |
| Risk | Risk/trade 0,5–3% (docs) vs 1–10% (services), leverage 3–20×, daily DD auto-pause sampai 00:00 WIB, skip log dengan alasan | docs.txt "Max Drawdown (1%–10%)… auto-pause" | RISK | Konsepnya benar, tetapi batas 10% risk per trade adalah jalan menuju ruin |
| Content | 184 outlook publik (177 slug "neutral"), news RSS, kalender makro | sitemap; outlook terbaru 5 Jul 2026, sementara home menulis "186 outlook · last 30d" | PRES/DIST (SEO) | Tidak bisa difalsifikasi dan tidak di-grade |
| Monetization | VIP Rp450rb/bln, ScalpAuto Rp450rb, Autopilot Rp200rb, Bundle Rp650rb (lifetime Rp8,95jt), API Rp250rb–2,7jt | pricing RSC `finalPrice`, `bitunixDiscount:true` | DIST | Diskon "−50%/−71%" berlaku permanen; harga beda di 4 halaman lain ($29/$99/$299; "Rp 299rb"; "Mulai Rp 173rb") |
| Monetization | Funnel referral Bitunix (`/r/bitunix`, verifikasi UID `/vipuid`), affiliate/KOL 30–50 "pts", lifetime plan | docs.txt, affiliate.txt; Terms: 30%/20% | DIST | Kemungkinan sumber revenue besar dari rebate (inferensi). Ada konflik kepentingan dengan ScalpAuto yang membuka "minimum 1 posisi/jam" |
| Community | Channel Telegram publik, grup VIP dengan auto-invite/auto-kick, testimoni screenshot PnL | home: "31.289 subscribers" vs "29.187+"; VIP Lounge 415 vs status 270 (bisa jadi populasi berbeda) | DIST | Distribusi adalah moat LW yang sebenarnya |
| API | REST `/v1/signals`, `/v1/leaderboard`, SSE `/v1/stream`, webhook Enterprise; kuota 100/1k/10k/100k per hari | api-docs.txt; key boleh lewat `?api_key=` | DIST | Taksonomi event SSE bagus. Key di query string adalah praktik buruk |
| Trust/UX | Track record gallery, `/signals/<id>` dengan OG meta, slider before/after, toggle 7/30/90/365, cumulative-R chart, status page, changelog | track-record.txt; stats.txt "sumber: koleksi sinyal NeDB" | PRES | Strukturnya layak ditiru, metrik dan kurasinya tidak (lihat §3) |
| Trust/UX | Floating "+R" chips di hero | rsc home: 14 chip opacity 0,4, animasi drift; XPL "+3.2R" padahal live −1,00R | PRES (menyesatkan) | Kemungkinan besar hard-coded (belum terbukti) |

**Kesimpulan anatomi.** Sekitar 80% permukaan LW adalah PRES/DIST. Komponen yang secara teoretis bisa menggerakkan expectancy hanya mekanika eksekusi: limit entry, ladder dengan BE, sizing, DD cap, dan idempotensi. Itu pun efeknya ke bentuk distribusi dan biaya, bukan ke tanda (sign) edge. Lihat §3.4.

---

## 3. Forensik track record LW

### 3.1 Angka yang dipublikasikan dan artinya

| Lokasi | Angka | Arti sebenarnya | Status |
|---|---|---|---|
| `/track-record`, `/stats`, home | "100% Win Rate/Hit Rate · +1.28R Avg RR · 70 Setup Tervalidasi" | Mean Outcome dari 70 halaman `/signals/` di sitemap = 89,9/70 = 1,2843. Semuanya positif (min +0,51R; 24/70 < 1R). Isinya 4 analis (+3,25R rata-rata) + 66 AI M1 (+1,17R). Seluruh lastmod jatuh di 2026-05-06…11 | Terbukti, bisa direproduksi persis |
| Sama | Headline identik di `?source=analyst` dan `?source=scalpauto`; tab analyst merender 0 kartu sinyal (hanya 60 testimoni PnL) | Headline tidak ikut filter | Terbukti |
| `/stats` 30d | 46 sinyal · 22 ditutup / 24 aktif · Hit Rate 74,4% (29 TP · 10 SL) · +19,00R · 11W/10L | Hit rate dihitung per event TP. WR per trade = 11/21 = 52,4% | Terbukti |
| `/stats` 7d / 90d / 365d | 37,5% (3/5) −2R · 70,4% (114/48) +66R · 74,5% (701/240) +461R | Net R = TP − SL tepat di keempat window | Terbukti (identitas) |
| Header Net vs curve | Header +19 / −2 / +66 / +461, ujung curve 17 / −4 / 64 / 460 | Header ikut menghitung R unrealized dari trade yang masih jalan (JTO BERJALAN +2,00R) | Terbukti |
| Wins 7d | W2 + L5 = 7 > 6 closed | Trade terbuka (JTO, TP1+TP2) sudah dihitung sebagai win | Terbukti (eliminasi) |
| "aktif" | 365d: 229 aktif | "aktif" ikut mencakup BATAL dan pending. Klaim awal "171 sinyal stale tanpa expiry" **dibatalkan** | Koreksi verifikator |
| Bucket netral | closed − (W+L) = 23 (365d), 9 (90d), 2 (30d) | Kemungkinan BE/manual close. Harus dihitung 0R | Terbukti (aritmetika) |
| Kartu "Risk/Reward 1:X" | LAYER 1:1,8 = Outcome +1,79R; DYM 1:0,5 = +0,53R | Angka RR di kartu sama dengan angka Outcome, jadi tidak memberi informasi tambahan. Apakah itu planned atau realized tidak bisa dibuktikan | Sebagian |
| Slider before/after | Gambar before/after diunggah berselisih 11–710 ms (BTC: hold 13D, selisih 423 ms) | Gambar "before" tidak diambil saat entry | Terbukti dari nama aset (analisis UX, epoch dicek ulang oleh verifikator forensik) |
| Permalink sinyal rugi | `/signals/sig_1791639367350_oroqyr` (XPL SL HIT) mengembalikan "Signal not found" | Hanya winner yang punya halaman | Diamati satu kali, belum diverifikasi independen |
| Hero chips | INJ +2,4R, XPL +3,2R, WLD LONG +2,1R vs live 0,00R / −1,00R / SHORT −1,00R | Dekoratif, nilainya kemungkinan hard-coded | Kemungkinan |
| Duplikat AI | LAYER +1,79/+1,80 dan DYM +0,53/+0,54, hold dan strategi sama | Lebih cocok sebagai eksekusi per akun daripada copy-paste | Kemungkinan |
| FAQ | "tidak ada sinyal yang 100% profit" | Disclaimer tanpa jaminan. Kontradiksi yang lebih kuat ada di stats.txt: "menampilkan signal yang berhasil dan yang gagal untuk transparansi penuh" | Terbukti |

### 3.2 Hitung ulang ledger analis LW (`work_lw_forensics/honest_stats.py`, direproduksi di `work_lw_verify_forensics/recompute.py`)

| Window | n decided | WR per trade (95% Wilson) | "Hit Rate" publik | E[R] cara hitung LW | E[R] mode Exit (TP1≈1R), pre-fee |
|---|---|---|---|---|---|
| 30d | 20 | 50,0% (29,9–70,1) | 74,4% | +0,85 | 0,00 (±0,40) |
| 90d | 98 | 51,0% (41,3–60,7) | 70,4% | +0,65 | +0,02 (−0,18–0,22) |
| 365d | 529 | 54,6% (50,4–58,8) | 74,5% | +0,87 | +0,09 (0,01–0,18) |
| 365d + 23 netral sebagai 0R | 552 | 52,4% | — | — | +0,089 |

- **Sensitivitas TP1.** Kalau TP1 = 1,14R (contoh di docs), mode Exit menjadi +0,17R (1y) dan +0,09R (90d) sebelum fee. LW tidak mempublikasikan jarak TP maupun SL, jadi tidak ada angka pasti.
- **Fee drag.** Diperkirakan 0,025–0,05R per trade (spekulatif). Hasil bersih mode Exit setelah fee ada di kisaran ~0 sampai +0,12R.
- **Arah bias pembukuan.** Pembukuan +1R per level TP melebih-lebihkan hasil untuk mode Exit/Combo (sekitar 2,4 kredit TP per win), tetapi justru mengecilkan hasil Ride yang mencapai TP3 (3 vs 4,71R).
- **Decay.** Jan–Apr 2026: 200 sinyal, 304R (1,52R/sinyal). Mei–14 Okt: 581 sinyal, 156R (0,27R/sinyal). Max DD −10,9R (2026-05-13). Caveat: penyebutnya memakai semua sinyal yang dibuat (termasuk BATAL/pending), jadi sebagian penurunan adalah artefak penyebut.

### 3.3 Pelajaran: win rate bisa direkayasa (backtest cepat di data Crossline)

Script: `scratchpad/work_lw_method/bt_lw.py`, direproduksi independen di `work_lw_verify_methodology/rebt.py`.
- **Data:** 12 simbol, 2H, Okt-2024 → Sep-2026. IS < 2026-01-01, OOS 2026-01 → 09.
- **Biaya:** fee 0,1% RT, slippage 2 bps (majors) / 10 bps (alts), SL-first.
- **Mekanika A (sweepReversal):** wick menembus high/low 20 bar lalu close kembali ke dalam → market entry di open berikutnya.
- **Mekanika B (BOS + limit retest):** limit di level yang ditembus, CANCELLED kalau tidak terisi dalam 6 bar.

| Config | n | WR | Net R/trade | t | Catatan |
|---|---|---|---|---|---|
| A1 sweep, TP 1R | 2.767 | 46,8% | −0,270 | −13,6 | IS −0,305 / OOS −0,215; break-even WR ≈60% |
| A2 sweep, TP 0,5R | 2.767 | **59,5%** | **−0,305** | −20,0 | WR +12,7 poin, expectancy lebih buruk; break-even ≈80% |
| A3 sweep, ladder 1/2/3R per sepertiga, SL→BE | 2.767 | 46,8% | −0,271 | −12,3 | **Kalau dihitung cara LW: +0,306R gross / +0,092R net; event hit rate 61,2%** |
| B1 BOS + limit retest, TP 1R | 2.202 | 47,9% | −0,141 | −6,5 | Fill rate 75,5% (715 CANCELLED) |
| B2 sama, TP 0,5R | 2.202 | **60,6%** | **−0,185** | −11,5 | WR +12,7 poin, expectancy lebih buruk |
| B3 = B1 + filter 1D EMA50 | 1.466 | 49,2% | −0,111 | −4,1 | OOS −0,097 (t −2,25); dengan maker fee OOS −0,057 (t −1,31, tidak signifikan) |

Robustness: dengan asumsi TP-first yang optimis, semua tetap negatif (A1 −0,140; B1 −0,115; B3 −0,090; A3 −0,184 setelah koreksi). Semua juga tetap negatif dengan lock berbasis exit dan dengan maker fee.

**Tiga cara win rate direkayasa, semuanya terbukti di data Crossline sendiri:**
1. **TP dekat.** WR naik 12–13 poin, expectancy turun, karena fee dan slippage stop tidak ikut mengecil dalam satuan R.
2. **Cara menghitung.** Mengkreditkan posisi penuh di level TP terjauh mengubah −0,271R menjadi +0,092R. Selisih 0,36R/trade itu murni hasil akuntansi.
3. **Limit entry + cancel.** Sinyal yang dibatalkan adalah move yang lari. Kalau dieksekusi di market, kelompok ini akan mencatat WR 82% dan +0,59R. Angka itu bergantung pada masa depan dan tidak bisa ditradingkan. Akibatnya trade yang terisi ter-*adverse-select*, sedangkan LW mencatat BATAL sebagai 0R di luar WR. Catatan: perbandingan biaya A vs B (0,205 vs 0,099R) tercampur faktor lain (set sinyal dan lebar stop berbeda), jadi belum membuktikan limit entry lebih murah. Butuh uji overlay pada sinyal yang sama.

**Cermin untuk Crossline.** Kalau Crossline hanya menampilkan 4 winner-nya, headline ala LW akan berbunyi "100% Win Rate · +1,49R · 4 setup". Tiga di antaranya baris legacy dengan fee 0. Kartu R dan cumulative-R chart di atas ledger yang belum dibersihkan hanya akan menyebarkan angka yang membengkak. Jadi pembersihan ledger (P0) adalah syarat sebelum presentasi apa pun.

---

## 4. Tiru / Adaptasi / Hindari

| Tiru (apa adanya) | Adaptasi secara jujur | Hindari |
|---|---|---|
| Permalink per sinyal `/s/{id}` dengan OG meta dan judul R, **untuk semua sinyal termasuk yang rugi** | Slider before/after → toggle "At publish / After close" yang dirender dari candle Binance (≤ `detected_at`), untuk semua sinyal closed, dengan footnote sumber kline | "100% Win Rate" atau showcase kurasi; filter yang menyembunyikan rugi |
| Chip status lifecycle teks + ikon + warna; state terminal spesifik (TARGET HIT / STOPPED OUT / EXPIRED, bukan satu "COMPLETED") | Ticker → strip statis "10 event terakhir" dengan timestamp asli | Hit rate per event TP; Net R = jumlah TP − SL; R unrealized dicampur ke header |
| Unit R di mana-mana | Toggle 7/30/90/All + cumulative-R, ditambah n, CI, selector versi, dan marker pergantian versi | Win yang ikut menghitung trade terbuka; BATAL di luar penyebut |
| Status page + changelog | Status dari SLI nyata (cron last success, missed slots, evaluator lag); changelog terikat `strategy_version` | Chip "+R" dekoratif; counter sosial yang tidak bisa diverifikasi; testimoni screenshot |
| Footer sumber data ("updated every 2H cron · Postgres ledger + Binance klines") | Tier atau klasifikasi: chip netral (Trend-aligned 1D EMA50 / Counter-trend) dengan statistik per kelas; tier baru boleh dipakai kalau sudah pre-registered dan per tier n ≥ 100 | Tier S/A+/B tanpa definisi atau kalibrasi |
| Skip log dengan alasan, idempotensi, deterministic order ID (kalau suatu saat ada eksekusi) | Ladder TP + BE dan limit entry hanya sebagai **strategy version terpisah** yang di-backtest, dengan akuntansi realized-R dan pelaporan fill rate | Diskon coret permanen, lifetime plan, harga yang beda antar-halaman |
| Taksonomi event SSE (signal:new, entry_hit, tp_hit, sl_hit…) | Bot Telegram: alert berisi permalink, valid_until, dan peringatan "jangan kejar"; perintah `/performance` menampilkan n dan CI | Risk 10%/trade; ScalpAuto dengan kuota "≥1 posisi/jam" + rebate exchange |
| Docs/tutorial langkah demi langkah | Outlook (kalau ada) dengan field terstruktur (bias, zona, invalidasi, horizon) yang di-grade otomatis | Outlook "neutral" yang tidak bisa difalsifikasi; verdict AI pada strategi ~0R |
| — | API publik: mulai dari JSON/CSV ledger read-only (menambah verifiability) | Key di query string; klaim "5 bursa" / "<200 ms" tanpa bukti; Next.js (halaman LW 0,5–0,7 MB) |

---

## 5. Desain target Crossline

### 5.1 IA / halaman (tetap FastAPI + Jinja2 shell + vanilla ES modules, tanpa build step)

| Route | Audiens | Isi | Endpoint/DB baru |
|---|---|---|---|
| `/` Overview | publik | Badge "Research, belum ada edge tervalidasi"; strip KPI jujur (n, mean net R ± CI); sinyal open sebagai kartu; 10 event terakhir; cron last/next | `GET /api/performance` (menggantikan `/api/summary`, main.py:943) |
| `/signals` | publik | Semua sinyal, filter symbol/side/status/version/source | Kolom `strategy_version`, `source`, `entry_basis`, `detected_at` |
| `/s/{id}` | publik, shareable | §5.3 | Route Jinja dengan OG; candles + `detected_ms/entry_ms/exit_ms`; `GET /api/signals/{id}/events` |
| `/track-record` | publik | §5.4 | `GET /api/performance?group_by=…`, `GET /api/ledger.csv`; kolom `gross_r/cost_r/net_r/mae_r/mfe_r` |
| `/symbols/{sym}` | publik | State MACD, cross terakhir, n/mean R per simbol | `GET /api/scan/latest` (cache scan cron terakhir; menggantikan tab "Latest scan" yang kosong untuk publik) |
| `/status` | publik | Last success, on-schedule ratio, 50 run terakhir, evaluator lag, incident | Tabel `cron_runs` (counter saat ini hanya di memori, cron_scheduler.py:73-78) |
| `/changelog`, `/methodology` | publik | Versi aturan, git SHA, params hash, baris ledger terdampak; definisi R, fee, expiry 48 candle, SL-first, CI | `changelog.json` / `GET /api/versions` |
| `/ops`, `/journal` | API key | Run scan, Evaluate, Watchlist (dipindah dari halaman publik); jurnal pribadi | Scan via POST dengan `save_db/send_telegram=False` default (saat ini True, main.py:726-731) |

`app.js` dipecah menjadi `js/core.js`, `js/chart.js`, `js/cards.js`, dan satu entry per halaman. CSS dipindah ke `static/css/base.css`. Navigasi: Overview · Signals · Track record · Status, dengan Methodology dan Changelog di footer.

### 5.2 Signal card (`js/cards.js`, menggantikan template di app.js:446-487)

```
BTCUSDT  ▼ SHORT  2H  v2.2·next-open   [LATE] [LEGACY]
● STOPPED OUT                                  −1.07R net
Entry 83,829.2   Stop (+1.5%)   Target (−2.3%)
[R-progress bar −1R ... +1.5R, posisi exit/MTM]
Detected 14 Okt 23:00 WIB · held 6h · exit 15 Okt 05:00
gross −0.98R · cost −0.09R                      Detail →
```

- **Status:** OPEN (dengan countdown expiry), TARGET HIT, STOPPED OUT, EXPIRED, LATE (dikeluarkan dari statistik), LEGACY (garis putus, dikeluarkan), RESTATED. Implementasinya memperluas `outcomeOf()/outcomeHtml()` di app.js:185-196.
- **Tata bahasa angka:** selalu bertanda, minus U+2212, 2 desimal, sufiks R, dan n di samping setiap rate, misalnya `44.4% (4/9)`.
- **Tidak dipakai:** tier, "Featured", avatar, efek sparkle.

### 5.3 Halaman detail `/s/{id}`

1. `<title>`/`og:title` = `{SYMBOL} {SIDE} · {±R|OPEN} · Crossline {version}`. Perlakuannya identik untuk win dan loss.
2. Banner outcome netral, misalnya "Stopped out −1.07R (gross −0.98R, cost −0.09R)".
3. Chart memakai ulang `renderChart()` (app.js:1151-1275) dengan segmented control *At publish | After close*. Tambahkan pita risk/reward dan footnote "Candles: Binance USDⓈ-M fapi/v1/klines…". Window candle final dipersist ke `signal_candles` saat resolusi (`_evaluate_pending_signals_unlocked`, db.py:747) supaya immutable.
4. Facts grid: version + params hash, source, entry basis, cross/confirm/detected/alert_sent, level, hold, MAE/MFE (dari `resolve_outcome`, outcome.py:14), gross/cost/net R. Nilai MACD mentah dilipat.
5. Timeline event (`signal_events`), 5 sinyal terakhir di simbol yang sama dengan semua outcome, link JSON mentah, dan entry changelog terkait.
6. `format_signal_alert` (alerts.py:17) menambahkan permalink.

### 5.4 Halaman track record yang jujur

Urutan:
1. **Header.** Badge research, selector versi (default v2.2), keterangan "3 legacy rows excluded", periode.
2. **Kartu baseline backtest dulu:** "Okt-2024→Sep-2026, 12 simbol, n=7.576: gross +0,025R; fee produksi −0,014R (t −1,0); biaya realistis −0,043 s/d −0,057R".
3. **Strip KPI dengan n.** Contoh untuk v2.2: `Mean net R −0.65R [−1.72, +0.43] n=6`, `Win rate 17% (1/6) · break-even ≈42%`, `Net −3.88R`, max DD (diurutkan dengan benar berdasarkan exit_time), profit factor, expired. Tile "% after fees" (db.py:1024-1029, app.js:409) dihapus. Kalau n < 30, tampilkan caption "insufficient sample"; total disembunyikan sampai n ≥ 100 per versi.
4. **Equity curve dalam R** (baseline series lightweight-charts) + pane underwater + marker versi.
5. **Histogram R.**
6. **Breakdown** per versi/side/simbol/bulan/varian. Baris dengan n < 20 diberi abu-abu dan label "not meaningful".
7. **Ledger lengkap** (open, closed, LATE, legacy, restated) dengan unduhan CSV/JSON.
8. **Tab "All charts":** grid chart after-close untuk semua sinyal closed, termasuk yang rugi.
9. **Footer integritas:** ledger terakhir berubah, jumlah baris restated, hash-chain head (setelah `signal_events` ada).

Definisi win rate harus eksplisit. Saat ini TP/(TP+SL+EXPIRED) (db.py:1019-1020). Net R ditampilkan lebih dulu daripada WR, supaya tidak ada lagi jangkar ke "44% vs 74,4%".

### 5.5 Arsitektur multi-strategy (skala yang realistis untuk 1 developer)

| Komponen | Crossline sekarang | Target | File diubah/ditambah |
|---|---|---|---|
| Strategy interface | MACD hard-coded (`macd.py:110`), params global env (`settings.py:47-63`), TF konstan "2h" (`settings.py:50`), cron memanggil langsung (`cron_scheduler.py:189-200`) | Protocol `Strategy` (key, version, params pydantic, timeframes, warmup, `on_bar_close(ctx)->list[SignalIntent]`, pure, closed bars). Registry + tabel `strategy_versions` (params_hash, code_sha, status research/shadow/live, frozen). Engine menolak emit kalau versinya tidak frozen | Baru `src/strategy/base.py`, `registry.py`; port `macd_cross@2.2.0`; shadow `macd_cross_ema50d_oppexit@1.0.0` |
| Backtest = live | Tidak ada harness di repo | `src/backtest/` memakai fungsi yang sama; replay-parity test; gate di CI | `.github/workflows/deploy.yml` |
| MTF | `CANDLE_INTERVAL_MS` (outcome.py:3), `CANDLE_HOURS=2` (alerts.py:10) | Context per TF dengan as-of join pada candle daily yang sudah close. Ada unit test anti look-ahead (verifikator menemukan bug ms vs ns di pandas 3 yang diam-diam menimbulkan look-ahead) | `src/strategy/context.py` |
| Data | REST klines per simbol (ohlcv.py:18), satu call per sinyal open (db.py:768) | Fase 0–1: polling inkremental (limit kecil) sudah cukup untuk 6–12 simbol (estimasi 126 sym × 3 TF ≈ 16% budget). WS ingestor baru diperlukan kalau universe > ~50; gunakan routed endpoint `/market` USDⓈ-M. 1m diambil **on-demand** via REST hanya untuk bar ambigu (SL-first hanya memengaruhi 12/7.576 trade) | `src/data_feed/` |
| Lifecycle | `win_status` PENDING/TP1_HIT/SL_HIT/EXPIRED (db.py:102), entry next open (macd.py:152) | State machine eksplisit + `signal_events` append-only. CANCELLED/LATE tetap ada di ledger. Limit entry hanya sebagai versi terpisah dengan model fill trade-through | `src/strategy/lifecycle.py`, `src/services/db.py` |
| Ledger | Mutable; POST P/L manual (main.py:683-702); legacy fee 0 | `signal_outcomes` (gross/cost/net, cost_model_version), hash chain, head harian dipublikasikan; adjustment manual dipindah ke tabel terpisah yang tidak masuk statistik | `db.py`, Alembic |
| Alert | One-way `sendMessage` (telegram.py:48-60), `asyncio.create_task` tanpa referensi (cron_scheduler.py:244), sehingga task bisa di-GC dan alert hilang diam-diam | Outbox transaksional (`dedup_key UNIQUE`), retry/429 | `src/services/notifier.py` |
| Ops | Health "healthy" walaupun cron mati (main.py:303-315); cron di dalam proses web (main.py:99-113) | `cron_runs`, health `degraded` kalau `now − last_success > 2h05m`, dead-man ping (Healthchecks). Prometheus/Grafana belum perlu | `main.py`, `cron_scheduler.py` |

---

## 6. Roadmap berfase

| Fase | Deliverables | Perubahan schema | Acceptance criteria | Effort | Gate keluar |
|---|---|---|---|---|---|
| **0. Fondasi kejujuran** (sekarang) | Label "Research, belum ada edge tervalidasi"; `SIGNALS_ONLY` menggantikan `DRY_RUN`/"PRODUCTION" (settings.py:10, main.py:291); restate id 1–4 sebagai legacy; status LATE; scan manual POST dengan default tidak menyimpan; model biaya per simbol (majors hanya BTC/ETH sampai dibuktikan lain); KPI n + CI + break-even WR; hapus tile summed-%; `avg_rr`→`target_rr`; backtest harness + gate CI; auth di semua `/api/*` kecuali health minimal; cookie HttpOnly menggantikan localStorage; `docs_url=None`; CSP/HSTS; dead-man + `cron_runs`; Alembic. **Akuisisi data 2020–2024-09 dan histori funding** (belum ada; klines di scratchpad mulai 2024-10-01) | `macd_signals` + `strategy_version`, `source`, `entry_basis`, `detected_at`; `profitloss_history` + `gross_r`, `cost_r`, `net_r`, `cost_model_version`; tabel `cron_runs` | Test gagal kalau ada baris closed dengan fee 0 atau `cross_interp` di luar `source='legacy'`; headline restated ≈ −2,55R (bukan +0,63R); dashboard menampilkan n dan CI; PR yang menyentuh `src/strategy/**` gagal tanpa laporan backtest baru; `/api/*` 401 tanpa kredensial; health `degraded` dalam ≤5 menit setelah slot terlewat | 2–4 minggu | Semua acceptance hijau |
| **1. Platform riset + track record jujur** | Interface Strategy + registry; port MACD; shadow EMA50/opposite-exit dengan parameter dikunci lewat commit hash + dokumen pre-registrasi; uji sekali di holdout 2020–2024; `research_trials` mencatat setiap konfigurasi; lifecycle + `signal_events` hash chain; halaman `/s/{id}`, `/track-record`, `/status`, `/changelog`, `/methodology`; ekspor CSV | `strategies`, `strategy_versions`, `signals` (ULID, features JSONB, input_candles_sha256, state), `signal_events` (INSERT-only), `signal_outcomes`, `backtest_runs`, `research_trials`, `ledger_heads`, `signal_candles` | Replay parity byte-identik; engine menolak versi yang tidak frozen; verifier hash chain lulus dan langsung gagal kalau ada tampering; halaman per versi menampilkan n, mean net R, CI, jumlah CANCELLED/LATE; shadow berjalan tanpa modal dan tanpa distribusi berbayar | 6–10 minggu | Holdout pass: t ≥ ~3 terdeflasi untuk semua trial, tetap positif dengan +10 bps impact dan setelah top 1% trade dibuang |
| **2. Produk & komunitas** | Channel Telegram publik (gratis) dengan permalink; bot interaktif dengan outbox (`/signals`, `/performance`, `/status`, `/mute`); API `/v1` read-only + CSV; akun/RBAC **hanya kalau** ada fitur per-user; status page dari SLI; copy marketing yang patuh (tidak ada WR tanpa n/CI/net R) | `users`, `auth_identities`, `sessions`, `audit_log`, `api_keys`, `api_usage_daily`, `telegram_links`, `notification_prefs`, `outbox`, `incidents`; billing (`products/orders/payment_webhook_events/subscriptions`) **ditunda** | SSE/`Last-Event-ID` tanpa gap dan tanpa duplikat (soak 1.000 event); alert p95 < 10 dtk setelah bar close tanpa DM duplikat; 429 di batas kuota; key tidak diterima lewat query string; OWASP ASVS L1; privacy notice | 8–12 minggu (bisa overlap dengan akhir Fase 1) | **Berbayar hanya kalau** gate holdout Fase 1 lulus **dan** ada legal sign-off. Sebelum itu yang boleh dijual hanya tooling/data berlabel riset |
| **3. Eksekusi / Autopilot** | Executor terisolasi, key vault KMS (tolak key yang bisa withdraw atau tanpa IP restriction), risk engine dengan hard cap (0,25% NAV/trade, maks 3 posisi, maks 2 searah, gross ≤2×, daily −2R / weekly −5R), user-data stream + reconciler, kill switch, dashboard skip reason. Rollout testnet → house account ≥8 minggu → invite ≤10–20 user → GA | `exchange_accounts`, `risk_profiles`, `exec_intents` UNIQUE(user, signal), `orders` (client_order_id UNIQUE), `fills`, `positions`, `reconciliation_runs`, `kill_switches` | Chaos test: tidak ada order ganda dan tidak ada posisi tanpa stop > N dtk; diff rekonsiliasi 0 selama 30 hari atau akun auto-halt; kill switch global < 5 dtk; setiap decrypt diaudit; realized per user sesuai ledger dalam band shortfall; auto-demote ke shadow kalau rolling-50 < −0,10R | ≥3 bulan **setelah** gate | **Gate masuk:** versi frozen positif net di holdout OOS **dan** live shadow (CI bawah > 0, t ≥ 2 live, terdeflasi); lolos +10 bps impact dan pembuangan top 1% trade; shortfall ≤30% dari edge gross; legal sign-off; review keamanan |

**Realisme kalender untuk gate.** Kandidat EMA50 setelah dihitung ulang: +0,043R (t 1,75) dengan klasifikasi majors versi review; +0,021R (t 0,84) kalau majors hanya BTC/ETH; ≈0 atau negatif dengan +10 bps; −0,020R kalau top-2 trade per simbol dibuang. Dengan sd(R) ≈ 1,49, edge +0,05R butuh sekitar 3.600 trade untuk t ≥ 2. Itu kira-kira 4 tahun di 6 simbol (~75 trade/bulan) atau sekitar 2 tahun di 12 simbol. Artinya Fase 3 kemungkinan **tidak terjadi dalam 12 bulan**, dan itu hasil yang benar, bukan kegagalan rencana. Riset scalper M1 ala LW tidak dianjurkan: prior-nya rendah karena biaya, dan setiap konfigurasi tambahan menaikkan ambang deflasi.

### Catatan legal/regulasi yang perlu dicek konsultan (bukan nasihat hukum)

| Area | Pertanyaan untuk konsultan |
|---|---|
| Regulator | Pengawasan aset kripto berpindah dari Bappebti ke OJK sejak 10 Jan 2025 (UU 4/2023 P2SK, PP 49/2024, POJK 27/2024, SEOJK 20/2024). Apakah sinyal berbayar termasuk aktivitas penasihat (advisory) berizin? Apakah Autopilot (menempatkan order di akun user) termasuk pengelolaan portofolio? |
| Venue | Binance (dan Bitunix) adalah bursa offshore yang tidak terdaftar di Indonesia. Bolehkah mempromosikan, me-refer, atau mengeksekusi derivatif di venue ini untuk residen Indonesia? Ini **blocker** untuk pilihan venue Crossline sendiri, bukan sekadar catatan |
| Pembayaran | Penerimaan USDT vs kewajiban Rupiah (UU 7/2011) dan posisi BI bahwa kripto bukan alat pembayaran; lisensi PJP penyedia QRIS; PPN/PPh layanan digital; AML |
| Data dan key | UU PDP 27/2022 (keamanan, notifikasi breach, retensi); pendaftaran PSE Komdigi; liabilitas menyimpan key yang bisa trading |
| Konsumen | UU 8/1999: klaim performa yang menyesatkan (tipe "100% Win Rate"), klausula baku pembatasan liabilitas, konsistensi refund (LW sendiri kontradiktif: FAQ tidak ada refund, Terms 7 hari) |
| Konflik kepentingan | Rebate exchange atau affiliate yang digabung dengan frekuensi trading otomatis harus diungkap, atau lebih baik dihindari untuk produk eksekusi |

---

## 7. Keputusan yang perlu diambil owner

| # | Keputusan | Opsi | Rekomendasi | Alasan |
|---|---|---|---|---|
| 1 | Positioning | (a) Signal service ala LW; (b) research ledger publik yang jujur | **(b)** | Edge belum ada. Diferensiasi yang realistis: satu-satunya ledger di niche ini yang versioned, cost-honest, dan hash-chained |
| 2 | Dashboard publik? | Publik penuh / publik tertunda / privat | **Publik, dengan level live** (strategi ~0R tidak punya alpha yang perlu dilindungi); auth hanya untuk ops/jurnal | Verifiability adalah inti trust. Pertimbangkan delay kalau suatu saat ada edge |
| 3 | Produk berbayar? | Sekarang / setelah gate / tidak | **Setelah gate holdout + legal**. Sebelum itu gratis, atau hanya tooling berlabel riset | Menjual sinyal ~0R memunculkan masalah konsumen yang sama dengan yang dikritik pada LW |
| 4 | Auto-trading? | Bangun sekarang / setelah gate / tidak pernah | **Tidak sebelum gate Fase 3** (realistisnya > 12 bulan) | Mengotomatiskan expectancy ~0R hanya mengotomatiskan fee dan kerugian |
| 5 | Telegram/komunitas | Grup VIP / channel publik | **Channel publik gratis** dengan permalink ke setiap sinyal, termasuk yang rugi | Distribusi adalah moat LW yang nyata; bisa ditiru tanpa klaim palsu |
| 6 | Perluasan universe | Top-N 100+ / tetap 6–12 | **Tetap.** Perubahan universe dijadikan versi pre-registered tersendiri | Hasil per simbol campur (NEAR/LTC/LINK negatif), kapasitas alt belum memadai |
| 7 | Revenue rebate exchange | Ya / ungkap / hindari | **Hindari**, minimal ungkap | Konflik kepentingan + isu venue offshore |
| 8 | Stack | Next.js / FastAPI + Jinja | **FastAPI + Jinja + vanilla ES** | Satu developer, data kecil, kebutuhan SSR cuma OG meta |
| 9 | Data holdout 2020–2024 | Akuisisi sekarang / nanti | **Sekarang**, dan kunci parameter shadow sebelum melihat data itu | Tanpa ini pre-registrasi tidak bermakna |

---

## 8. Metodologi & keterbatasan

- **Sumber LW.** Scrape 22 halaman (`scratchpad/lw/html`, `lw/text`) beserta decode payload RSC. Tambahan fetch publik yang sesuai robots.txt (≤1 req/dtk, tanpa login/form/checkout; `/api/`, `/me/`, `/site-asset/`, `?page=` tidak disentuh): sitemap, seluruh 70 `/signals/<id>`, `/stats?days=7|90|365`, `/track-record?source=…`, 30 chunk JS, `/radar/BTC` (`scratchpad/lw_extra/`, `lw_fetch_forensics/`, `work_lw_ux/fetch/`). Data LW diperlakukan sebagai data yang tidak tepercaya.
- **Rekonstruksi statistik LW.** Diambil dari teks dan geometri SVG yang dirender server (`work_lw_forensics/stats_parse.py`, `honest_stats.py`; reproduksi di `work_lw_verify_forensics/`, `work_lw_verify_methodology/svg*.py`). Harga entry/SL/TP per sinyal dan jarak TP **tidak dipublikasikan**, jadi expectancy mode Exit/Combo adalah estimasi (sensitif terhadap jarak TP1: 1,0R vs 1,14R).
- **Verifikasi.** Lima analisis; tiga di antaranya diverifikasi adversarial. Koreksi yang diterapkan di dokumen ini:
  - Universe 70 bisa direproduksi persis (mean 1,2843).
  - "171 sinyal stale tanpa expiry" dibatalkan ("aktif" mencakup BATAL).
  - Bucket closed netral (23/9/2) dihitung sebagai 0R.
  - TP-first untuk A3 dikoreksi menjadi −0,184R.
  - "RR kartu = realized" diturunkan menjadi "RR = Outcome".
  - 415 vs 270 member mungkin populasi berbeda.
  - Kandidat EMA50 dihitung ulang lebih lemah (+0,021 s/d +0,043R).
  - Polling tidak sesulit perkiraan awal untuk diskalakan.
- **Klaim yang belum diverifikasi independen:** permalink rugi yang mengembalikan "Signal not found" (satu observasi), chip hero yang hard-coded (kemungkinan), duplikat AI = eksekusi per akun (kemungkinan), penyebab decay Jan–Apr vs Mei+ (tidak diketahui), dan revenue rebate Bitunix (inferensi).
- **Backtest.** Proxy 2H pada 12 simbol Crossline (Okt-2024 → Sep-2026), parameter dipilih sekali secara a priori, tanpa data 1m, orderbook, atau funding. Fill limit berbasis touch itu optimistis. Hasilnya menolak mekanika tersebut **di 2H dengan parameter ini**, dan tidak berkata apa pun langsung tentang performa realized LW di M1. Script: `scratchpad/work_lw_method/bt_lw.py`, `sens.py`, `out/summary.csv`.
- **Crossline.** Analisis memakai source deployed `scratchpad/prod_src/`. Nomor baris merujuk ke snapshot itu dan bisa bergeser di `master`. Scrape produksi `scratchpad/site2/`: 15 sinyal, 9 closed. CI untuk n=6–9 sangat lebar, sehingga sampel live tidak informatif dan harus dibaca terhadap baseline backtest n=7.576.
- **Binance.** Batas WS dan REST (stream per koneksi 200 vs 1024, weight ~2.400/menit, routed endpoint) perlu dicek ulang di dokumentasi resmi sebelum ingestor dibangun.
- **Legal.** Bagian legal adalah daftar pertanyaan untuk konsultan, bukan nasihat hukum. Status regulasi OJK diambil dari pemberitaan (Bisnis, detik) dan tidak dicek ulang dalam sesi ini.