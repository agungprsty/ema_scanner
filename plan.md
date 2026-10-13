# Rencana: akurasi hasil, notifikasi outcome, jurnal trade, UI responsif

Branch kerja: `feat/crossline-rebrand`.

Tujuan: angka di dashboard harus mencerminkan hasil yang bisa benar-benar didapat. Setelah itu, tambahkan notifikasi saat trade selesai dan jurnal untuk mencatat trade yang benar-benar diambil.

Dasar keputusan: backtest pada 12 simbol × 1 tahun data 2H. Win rate dan R di aplikasi turun dari +0,23R menjadi −0,05R per trade setelah entry dibuat realistis dan fee dihitung. Hampir seluruh selisihnya berasal dari harga entry hasil interpolasi.

---

## 1. Akurasi pengukuran (Prioritas 1)

### 1.1 Entry di open candle berikutnya
- Entry = harga open candle sesudah candle crossover. Ini harga pertama yang bisa didapat setelah sinyal terkonfirmasi.
- Harga crossover hasil interpolasi tetap disimpan di `cross_price` sebagai informasi.
- Stop loss tetap dari swing low/high. Target dihitung ulang dari entry baru (1,5 × jarak risiko).
- `detect_macd_cross` sekarang menerima data candle lengkap, termasuk candle yang sedang berjalan, supaya open candle berikutnya selalu tersedia.

### 1.2 Sinyal kedaluwarsa (expired)
- Setting baru `SIGNAL_EXPIRY_CANDLES` (default 48 candle = 4 hari).
- Kalau TP maupun SL belum tersentuh sampai batas itu, sinyal ditutup sebagai `EXPIRED` di harga close candle terakhir.
- Ini memperbaiki bug sinyal yang PENDING selamanya: evaluator sekarang hanya mengambil 100 candle pertama sejak crossover.
- Kalau TP dan SL tersentuh di candle yang sama, hitung sebagai SL (konservatif).

### 1.3 Fee dan satuan R
- Setting baru `ROUND_TRIP_FEE_PERCENT` (default 0,1% = taker 0,05% × 2).
- `profitloss_history` mendapat kolom `r_multiple` dan `fee_percent`. PnL yang disimpan sudah dipotong fee.
- Ringkasan dashboard: total R, rata-rata R, dan max drawdown (dalam R), selain total persen.
- Win rate = target tercapai / semua trade tertutup (target + stop + expired).
- Baris lama di `profitloss_history` tetap dengan nilai lamanya; `r_multiple` diisi dari data yang ada.

### 1.4 Sinyal dari candle yang belum tutup tidak disimpan
- Scan dengan "Include forming" tetap menampilkan sinyal itu, tapi tidak disimpan, tidak dikirim ke Telegram, dan tidak di-claim di Redis.
- Bug terkait ikut tertutup: claim Redis untuk candle yang belum tutup saat ini memblokir sinyal yang sama ketika candle tersebut benar-benar tutup.

### 1.5 MACD dari riwayat lebih panjang
- Setting baru `MACD_CANDLES` (default 500, dulu 100) untuk scan manual dan cron.

### 1.6 Cron mengejar candle yang terlewat
- Setting baru `CRON_LOOKBACK_CANDLES` (default 3, dulu 1). Dedup Redis dan database mencegah duplikat.
- Alert Telegram menyebut kalau crossover terjadi beberapa candle lalu.

### Catatan migrasi database
Saya **tidak** memasang Alembic di tahap ini. Database production sudah berjalan dengan migrasi idempotent di `init_db`. Memasang Alembic dengan aman butuh langkah `alembic stamp` di server saat deploy, dan itu tidak bisa saya jalankan atau verifikasi dari sini. Kolom dan tabel baru ditambahkan dengan pola yang sama (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`). Alembic sebaiknya jadi pekerjaan terpisah.

---

## 2. Notifikasi outcome di Telegram

- Setiap kali evaluator menutup trade (target, stop, atau expired), kirim satu pesan ringkas berisi simbol, arah, hasil, entry → exit, R, dan persen setelah fee.
- Hanya trade yang benar-benar baru tertutup di run itu yang dikirim. Ini dijamin oleh index unik `signal_id` di `profitloss_history`.
- Berlaku untuk cron, tombol "Evaluate outcomes", dan evaluasi otomatis setelah scan.
- Setting `TELEGRAM_OUTCOME_ALERTS` (default `true`).

---

## 3. Jurnal trade

Statistik sinyal berbeda dari hasil trading sebenarnya. Jurnal mencatat sinyal mana yang diambil dan berapa harga fill-nya.

- Tabel baru `trade_journal`: `signal_id` (unik), `status` (`TAKEN`/`SKIPPED`), `entry_fill`, `exit_fill`, `notes`, waktu dibuat/diubah.
- Hasil aktual dihitung dari harga fill: persen setelah fee, dan R terhadap stop loss sinyal.
- API:
  - `GET /api/journal`: daftar entri plus ringkasan.
  - `PUT /api/signals/{id}/journal`: simpan atau ubah entri (butuh API key).
  - `DELETE /api/signals/{id}/journal`: hapus entri (butuh API key).
  - `GET /api/signals/{id}` dan `GET /api/signals` menyertakan status jurnal.
- UI:
  - Bagian "Your trade" di dialog detail sinyal.
  - Tab Journal dengan ringkasan, termasuk perbandingan hasil Anda dengan hasil sinyal.
  - Penanda taken/skipped di tabel sinyal.

---

## 4. UI/UX responsif

- Dialog detail tampil layar penuh di ponsel, dengan header yang tetap terlihat saat di-scroll.
- Input berukuran 16px di ponsel supaya iOS tidak zoom otomatis. Target sentuh minimal 40px.
- Tab bisa di-scroll horizontal dengan fade di tepi sebagai petunjuk. Labelnya dipersingkat.
- Kolom simbol tetap terlihat (sticky) saat tabel di-scroll horizontal di tablet.
- Safe-area inset untuk ponsel berponi, dan dukungan `prefers-reduced-motion`.
- Toast konfirmasi untuk aksi yang berhasil (jurnal tersimpan, simbol ditambahkan).
- Kartu statistik: Win rate, Net result (R), Max drawdown, Open signals.

---

## 5. Verifikasi

- Unit test baru:
  - Entry di open berikutnya, sinyal forming tidak disimpan.
  - Evaluator: expired, SL saat ambigu, fee, R.
  - Format notifikasi dan pengiriman hanya untuk trade baru.
  - API jurnal: validasi dan auth.
- Test lama disesuaikan bila perilakunya memang berubah.
- `ruff` dan seluruh unit test lolos.
- Screenshot desktop dan ponsel memakai API tiruan.

## Di luar cakupan
Backtest bawaan, filter strategi, panel MACD di chart, dan Alembic.

---

## Progres

- [x] 1. Akurasi pengukuran
- [x] 2. Notifikasi outcome
- [x] 3. Jurnal trade
- [x] 4. UI/UX responsif
- [x] 5. Verifikasi dan dokumentasi

### Tambahan yang ditemukan saat verifikasi
- Scan kini melaporkan simbol yang candle-nya gagal dimuat (`failed_symbols`). Sebelumnya, kalau koneksi ke Binance gagal, dashboard hanya menampilkan "No crossovers found".
- Waktu exit lama yang tersimpan tanpa zona waktu kini dibaca sebagai UTC. Sebelumnya browser menganggapnya waktu lokal.
- Agregat ringkasan closed trades kini memakai filter `start_date`/`end_date` yang sama dengan daftar.
- Pagination 20 baris per halaman untuk tab Signals, Closed, dan Journal. Sebelumnya UI hanya memuat 100 baris pertama dan sisanya tidak bisa dibuka. Halaman kembali ke 1 setiap filter diubah.
