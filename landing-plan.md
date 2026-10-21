# Rencana: Landing page, Login/Register, Users & Dashboard

> Branch `feat/rsi-rewrite`. Stack tetap: FastAPI + `src/api/` (envelope, `AppException`, middleware CSP, rate limit), Postgres via SQLAlchemy async (`create_all`, tanpa Alembic), Redis, dan vanilla JS tanpa build step.
> **Tidak ada dependency baru.** Hash password memakai `hashlib.scrypt`, token memakai `secrets`, dan hash token memakai `hashlib.sha256`. Semuanya stdlib Python 3.12 (`Dockerfile`: `python:3.12-slim`). Body form dikirim sebagai JSON, jadi tidak perlu `python-multipart`. Email divalidasi dengan regex, jadi tidak perlu `email-validator`/`EmailStr`.

---

## 1. Ringkasan & tujuan

| Siapa | Sekarang | Sesudah |
|---|---|---|
| Pengunjung | `/` langsung membuka dashboard scanner (`main.py:169-172`). Semua `GET /api/*` terbuka. | `/` menjadi landing page statis. Data scanner hanya bisa dilihat setelah login. |
| `user` | Belum ada | Daftar sendiri di `/register`, lalu melihat dashboard **read-only**: sinyal aktif/riwayat, chart, watchlist, statistik, dan status cron. Tidak bisa melihat journal dan tidak bisa menulis apa pun. |
| `administrator` | Saat aksi tulis, `prompt()` meminta `API_SECRET_KEY` (`app.js:259`) lalu menyimpannya di localStorage | Login biasa. Semua aksi tulis tersedia: watchlist, journal, dan Run now. |
| Automasi (curl, CI, deploy) | `Authorization: Bearer <API_SECRET_KEY>` | Tetap sama dan diperlakukan sebagai administrator. |

**Keputusan kunci**

| Topik | Keputusan | Alasan |
|---|---|---|
| Transport sesi | Token opaque `secrets.token_urlsafe(32)` dalam cookie `st_session` dengan atribut `HttpOnly; Secure; SameSite=Lax; Path=/`. Tidak memakai JWT maupun localStorage. | JS tidak bisa membaca cookie ini, jadi XSS tidak bisa mencurinya. Browser mengirimnya saat navigasi, sehingga `/dashboard` bisa dijaga dari server. |
| Penyimpanan sesi | Tabel Postgres `user_sessions` yang hanya menyimpan **sha256** dari token | Helper Redis diam-diam tidak melakukan apa pun saat Redis mati (`redis_client.py:76-95`), sehingga sesi bisa hilang tanpa ketahuan. Kalau DB bocor, sesi aktif tidak ikut bocor. |
| Hash password | scrypt `n=2**14, r=8, p=1, dklen=64`, salt 16 byte, dijalankan lewat `asyncio.to_thread`, dibandingkan dengan `secrets.compare_digest` | Stdlib dan memory-hard. Butuh sekitar 16 MiB per hash, masih di bawah batas default OpenSSL 32 MiB. Parameter disimpan di setiap hash, jadi biayanya bisa dinaikkan nanti. |
| CSRF | SameSite=Lax ditambah cek `Origin` | Lihat §6. |
| `API_SECRET_KEY` | Tetap ada sebagai kredensial mesin dengan hak administrator. `verify_api_key` dihapus dan digantikan `get_principal`/`require_admin`. | Automasi tidak rusak, dan tidak ada dua jalur auth yang berjalan paralel. |
| Administrator pertama | Dibuat lewat CLI `scripts/create_admin.py`. Password dimasukkan dengan `getpass`. | Register publik **tidak pernah** memberi role admin. Password admin tidak tersimpan di `.env`. Skrip yang sama juga dipakai untuk reset password dan ubah role, karena tidak ada SMTP maupun UI manajemen user. |
| Identifier login | Email saja | Kodenya lebih sedikit dan hanya ada satu jalur yang bisa dipakai untuk enumerasi. Username dipakai sebagai nama tampilan. |
| Journal | Tetap global dan **khusus administrator**, baik baca maupun tulis | Journal berisi catatan trade milik admin (fill, catatan). `signal_journal.signal_id` bersifat UNIQUE, dan `create_all` tidak pernah menjalankan ALTER (lihat §15). |
| Lokasi file HTML | `src/pages/`, folder yang **tidak** di-mount | `app.mount("/static", ...)` (`main.py:112`) akan melayani `/static/dashboard.html` tanpa melewati guard. |
| Parameter `next` | Tidak dipakai. Login selalu menuju `/dashboard`. | Hanya ada satu halaman terlindungi, dan tanpa `next` tidak ada risiko open redirect. |

---

## 2. Peta halaman & routing

| Path | Akses | File | Keterangan |
|---|---|---|---|
| `GET/HEAD /` | Publik | `src/pages/landing.html` | Landing page baru, tanpa JS. |
| `GET/HEAD /login` | Publik | `src/pages/login.html` | Kalau sesi masih valid, redirect **303 ke `/dashboard`**. |
| `GET/HEAD /register` | Publik | `src/pages/register.html` | Kalau sesi masih valid, redirect **303 ke `/dashboard`**. |
| `GET/HEAD /dashboard` | Login, semua role | `src/pages/dashboard.html` (`git mv src/static/index.html`) | Tanpa sesi valid: **303 ke `/login`**. Respons diberi `Cache-Control: no-store`. |
| `/static/*` | Publik | Hanya CSS dan JS | `/static/index.html` dan `/static/dashboard.html` menghasilkan 404. |
| `/docs`, `/redoc`, `/openapi.json` | Publik | | Tidak berubah. |

- Satu helper di `main.py`, `_page(request, file, *, require_session: bool)`. Helper ini membaca cookie, memanggil `get_session_user()`, lalu mengembalikan `FileResponse` atau `RedirectResponse(..., status_code=303)`.
- `/dashboard` hanya bisa dibuka dengan cookie. Bearer API key tidak bisa dipakai karena navigasi browser tidak membawa header Authorization.
- nginx tidak perlu diubah: `location /` sudah mem-proxy semua path dengan `Host $host`.

---

## 3. Landing page (`/`)

**Prinsip:**
- Statis dan **tanpa JS**.
- Memakai token gelap yang sudah ada (`--bg`, `--surface`, `--long`, `--short`, `--entry`, IBM Plex) lewat `base.css`.
- Tetap dark-only seperti dashboard.
- Tanpa gambar raster atau eksternal. Visual dibuat dari HTML/CSS atau inline SVG (CSP `img-src 'self' data:`).
- Bahasa UI: English, mengikuti dashboard (lihat §15).

| # | Section | Isi |
|---|---|---|
| 1 | Topbar | Brand `simple-trade`, tombol **Log in** (ghost) dan **Sign up** (primary). Tanpa hamburger. |
| 2 | Hero | Judul "RSI 28/72 limit-entry signals for Binance USDⓈ-M futures", subjudul satu baris (limit entry, TP/SL otomatis, chart per trade), CTA **Create account** dan **Log in**. Visual berupa mock kartu sinyal dari HTML/CSS (baris LONG/SHORT dengan `--long`/`--short`), diberi `role="img"` dan `aria-label`. |
| 3 | Cara kerja | 3 langkah: (1) setiap candle yang sudah close discan; (2) RSI menyentuh ≤28 atau ≥72, lalu limit entry dipasang di low/high candle sinyal; (3) TP, SL dan max hold dilacak otomatis, dengan alert Telegram. |
| 4 | Fitur | Grid 4 sampai 6 kartu: watchlist, tab aktif/riwayat, chart per jendela trade, ringkasan win rate, scheduler per candle dengan status cron, dan jam WIB. |
| 5 | Statistik / parameter | Kartu parameter **statis**: RSI 14, 28/72, TP 2%, SL 1%, fee 0.1% (default di `settings.py:86-97`). Tidak ada statistik live (lihat §15). |
| 6 | CTA penutup | "Start watching signals" dengan tombol **Sign up**. |
| 7 | Disclaimer | "Not financial advice. Futures trading is high risk." |
| 8 | Footer | Brand, link Log in dan Sign up, serta tahun. |

**Responsif:** mobile-first, memakai breakpoint yang sudah ada di `index.html`.

| Lebar | Landing | Login/Register |
|---|---|---|
| ≤599px | 1 kolom, CTA full-width, tinggi kontrol `--control-h` 40px. Topbar hanya berisi brand dan 2 tombol kecil. | Kartu full-width dengan gutter 16px. |
| 600–999px | Fitur 2 kolom, hero 1 kolom (mock di bawah teks) | Kartu 400px di tengah |
| ≥1000px | Hero 2 kolom (teks dan mock), fitur 3 kolom, kontainer maks 1120px | Sama |

`--gutter` = `max(16px, safe-area)`. Tidak boleh ada scroll horizontal di lebar 320px.

**Aksesibilitas:**
- Satu `<h1>` dan landmark `<header>`, `<main>`, `<footer>`.
- Kontras teks minimal 4.5:1.
- `:focus-visible` dipertahankan (sudah ada di `index.html:66`).
- `prefers-reduced-motion` dihormati.
- Tombol berupa `<a>` dengan teks yang jelas.

**Performa:**
- Tanpa JS dan tanpa gambar.
- Satu `base.css` yang di-cache dan dipakai bersama.
- Font IBM Plex dari Google Fonts yang sama dengan `display=swap`.

---

## 4. Login & Register

`<form method="post" novalidate>` **tanpa `action`**. JS mencegat submit lalu mengirim JSON lewat `fetch`. Kalau JS gagal, browser melakukan POST ke `/login` atau `/register` dan mendapat 405, sehingga password tidak pernah masuk ke URL atau access log.

**Register (`/register`)**

| Field | Atribut | Aturan (sama di klien dan server) |
|---|---|---|
| `email` | `type=email`, `autocomplete=email` | Di-trim lalu diubah ke huruf kecil, maks 254 karakter, regex `^[^@\s]+@[^@\s]+\.[^@\s]+$` |
| `username` | `autocomplete=username` | Regex `^[A-Za-z0-9_]{3,32}$`, disimpan dalam huruf kecil |
| `password` | `autocomplete=new-password` | 10 sampai 128 karakter, tidak boleh sama dengan email atau username (case-insensitive), tanpa aturan komposisi |
| konfirmasi password | Hanya dicek di klien | Harus sama dengan password |

- Form tidak punya field role. Server selalu menyimpan `user`, dan field `role` di body diabaikan karena Pydantic mengabaikan field ekstra.
- Kalau sukses: respons 201 beserta cookie (auto-login), lalu klien menjalankan `location.assign('/dashboard')`.

**Login (`/login`):**
- Field `email` (`type=email`, `autocomplete=username`) dan `password` (`autocomplete=current-password`).
- Kalau sukses, klien diarahkan ke `/dashboard`.

**Pesan error** dibaca dari envelope error `{code, status, message, data, errors[], meta}` dengan pola `responseError` (`app.js:270-288`, sekitar 15 baris disalin ke `auth.js`).

| Kasus | HTTP | Pesan |
|---|---|---|
| Validasi gagal | 422 | Error ditampilkan per field dari `errors[].field`. |
| Email atau username sudah dipakai | 409 | "Email or username is already registered" |
| Password salah **atau** akun tidak ada | 401 | "Invalid email or password". Pesannya identik untuk kedua kasus. |
| Origin asing | 403 | "Request rejected" |
| Rate limit | 429 | "Too many attempts, try again in N s" (dari `Retry-After`) |
| DB mati atau 5xx | 503/500 | "Service unavailable, try again shortly" |

**UX:**
- Satu kartu di tengah layar.
- `<label>` eksplisit untuk setiap input.
- Error per field ditautkan lewat `aria-describedby`.
- Banner error umum memakai `role="alert"`.
- Fokus pindah ke field invalid pertama.
- Checkbox "Show password".
- Tombol submit dinonaktifkan selama request berjalan.
- Ada link silang ("No account? Sign up" dan sebaliknya) dan link kembali ke `/`.

Tidak ada fitur lupa password karena tidak ada SMTP. Admin mereset password lewat CLI (§5).

---

## 5. Data

Kedua tabel ini baru, jadi `create_all` (`db.py:301`) langsung membuatnya saat boot. **Tidak ada ALTER** pada tabel yang sudah ada.

```sql
CREATE TABLE users (
  id            SERIAL PRIMARY KEY,
  email         VARCHAR(254) NOT NULL UNIQUE,   -- lowercased
  username      VARCHAR(32)  NOT NULL UNIQUE,   -- lowercased
  password_hash VARCHAR(255) NOT NULL,          -- scrypt$16384$8$1$<salt_b64>$<hash_b64>
  role          VARCHAR(16)  NOT NULL DEFAULT 'user',
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT ck_users_role CHECK (role IN ('administrator', 'user'))
);

CREATE TABLE user_sessions (
  token_hash  CHAR(64)    PRIMARY KEY,          -- sha256 hex of the cookie token
  user_id     INTEGER     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at  TIMESTAMPTZ NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_user_sessions_user_id ON user_sessions(user_id);
```

- `UserModel` dan `SessionModel` ditaruh di `src/services/db.py` dan mengikuti gaya `WatchlistModel`/`JournalModel`: `Integer` autoincrement, `DateTime(timezone=True)` dengan `server_default=func.now()`, dan prefix constraint `ck_`.
- `UserModel.to_dict()` **tidak pernah** menyertakan `password_hash`.
- Constraint UNIQUE pada `email` dan `username` sudah membuat index. Duplikat ditangkap dari `IntegrityError` lalu diubah ke `Conflict`, jadi aman terhadap race dan tidak perlu pre-check.
- Tidak ada kolom `is_active` atau `updated_at` karena tidak ada yang memakainya. Untuk mengunci seseorang, hapus user-nya, dan sesinya ikut terhapus lewat CASCADE.
- Fungsi baru di `db.py`: `create_user`, `get_user_by_email`, `create_session`, `get_session_user(token_hash)` (JOIN `users` dengan `expires_at > now()`, sehingga role dibaca live), `delete_session`, dan `delete_user_sessions(user_id)`.
- Sesi kedaluwarsa dihapus dengan `DELETE ... WHERE expires_at < now()` setiap kali login. Tambahkan komentar `shortcut: cleanup only on login, move to the cron job if the table grows`.
- **`signal_journal`: tidak ada perubahan** di fase ini (lihat §15 untuk journal per user).

**Bootstrap administrator** (`scripts/create_admin.py`, sekitar 40 baris):

```
docker compose -f docker-compose.prod.yml exec app python -m scripts.create_admin \
  --email you@example.com --username admin [--role administrator|user]
```

- Password dimasukkan dua kali lewat `getpass` dan divalidasi dengan aturan yang sama seperti register.
- Kalau email belum ada, skrip melakukan INSERT dengan role default `administrator`.
- Kalau email sudah ada, skrip memperbarui password dan/atau role, lalu menghapus semua sesi user tersebut. Ini sekaligus menangani promosi role dan reset password.
- Skrip memakai `init_db` dan session dari `db.py`, serta `hash_password` dari `src/api/passwords.py`.
- `Dockerfile` menjalankan `COPY . .`, dan `.dockerignore` hanya mengecualikan `tests/` dan `*.md`, jadi `scripts/` ikut masuk ke image.

---

## 6. Autentikasi & sesi

| Alur | Langkah |
|---|---|
| Register `POST /api/auth/register` | Cek Origin, lalu bucket `auth`, lalu validasi Pydantic. Password di-hash di thread. INSERT dengan role `'user'` yang di-hard-code. `IntegrityError` dikembalikan sebagai 409. Kalau berhasil, buat sesi dan kirim 201 + `Set-Cookie`. |
| Login `POST /api/auth/login` | Cek Origin, lalu bucket `auth`, lalu cari user lewat email lowercase. Kalau user tidak ada, server **tetap memverifikasi terhadap `DUMMY_HASH`** supaya waktu respons sama. Kalau gagal, kirim 401. Kalau sukses, hapus sesi kedaluwarsa, buat **token baru** (mencegah session fixation), lalu kirim `Set-Cookie`. |
| Request berikutnya | `get_principal` (lihat di bawah). |
| Logout `POST /api/auth/logout` | Cek Origin, hapus baris sesi, kirim `Set-Cookie: st_session=; Max-Age=0`. Tetap 200 walau tidak ada sesi (idempoten). |
| Kedaluwarsa | Absolut: `SESSION_TTL_HOURS` dengan default 168 (7 hari), tanpa perpanjangan otomatis. Ganti password atau role lewat CLI langsung mencabut semua sesi user tersebut. |

**Cookie:** `st_session=<token>; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=<SESSION_TTL_HOURS*3600>`.
- Body respons **tidak pernah** berisi token.
- `Secure` diatur oleh `SESSION_COOKIE_SECURE` dengan default `true`. Browser tetap menerima cookie Secure di `http://localhost`, jadi setting ini hanya perlu `false` untuk HTTP biasa di host selain localhost.

**`src/api/passwords.py`** (sekitar 25 baris) berisi:
- `hash_password(pw) -> str`
- `verify_password(pw, stored) -> bool`

Keduanya async lewat `to_thread` dan membaca parameter scrypt dari string hash. `DUMMY_HASH` dibuat sekali secara lazy.

**`src/api/security.py`:**
- `get_principal(request, credentials=Depends(bearer_scheme))` bekerja berurutan:
  1. Kalau ada Bearer, token harus lolos `api_key_configured()` dan `compare_digest` dengan `API_SECRET_KEY`. Hasilnya `Principal(id=None, username="api-key", role="administrator", via="api_key")`. Kalau tidak cocok, kirim 401 "Invalid or missing API key".
  2. Kalau tidak ada Bearer tetapi ada cookie `st_session` (dibaca lewat `request.cookies`), server mencari user lewat `get_session_user(sha256(token))`. Untuk method `POST`, `PUT`, `PATCH` dan `DELETE`, server juga menjalankan `check_same_origin`.
  3. Kalau keduanya tidak ada, kirim 401 "Authentication required". Header `WWW-Authenticate: Bearer` sudah ditambahkan otomatis oleh `responses.py:76`.
- `require_admin`: kalau role bukan `administrator`, lempar `Forbidden("Administrator only")` (`exceptions.py:40`, sudah ada tetapi belum terpakai).
- `check_same_origin(request)`: kalau header `Origin` ada dan `urlsplit(origin).hostname != request.url.hostname`, kirim 403. Kalau `Origin` tidak ada, request diizinkan (klien non-browser).
  - Yang dibandingkan hanya hostname, karena nginx mengirim `Host $host` tanpa port.
  - Fungsi ini juga dipasang sebagai dependency di login, register dan logout untuk mencegah login CSRF.
- `verify_api_key` **dihapus**. Akibatnya, kalau `API_SECRET_KEY` kosong, akses mesin cukup mati (Bearer mendapat 401) dan tidak ada lagi 503. Teks warning di lifespan (`main.py:86-90`) diganti menjadi "machine access via Bearer is disabled".

---

## 7. Otorisasi (matriks role × endpoint)

| Endpoint / aksi | Visitor | `user` | `administrator` / API key |
|---|---|---|---|
| `GET /`, `/login`, `/register`, `/static/*`, `/docs` | ✓ | ✓ | ✓ |
| `GET /dashboard` | 303 ke `/login` | ✓ | ✓ (hanya lewat cookie) |
| `GET /api/health`, `GET /api/info` | ✓ | ✓ | ✓ |
| `POST /api/auth/register`, `POST /api/auth/login` | ✓ (bucket `auth`) | ✓ | ✓ |
| `POST /api/auth/logout` | 200 tanpa efek | ✓ | ✓ |
| `GET /api/auth/me` | 401 | ✓ | ✓ |
| `GET /api/watchlist`, `/api/summary`, `/api/cron/status` | 401 | ✓ | ✓ |
| `GET /api/signals`, `GET /api/signals/{id}` | 401 | ✓ tanpa field journal | ✓ |
| `GET /api/signals/{id}/chart` (memakai budget Binance) | 401 | ✓ | ✓ |
| `GET /api/journal`, `GET /api/journal/summary` | 401 | 403 | ✓ |
| `PUT/DELETE /api/signals/{id}/journal` | 401 | 403 | ✓ |
| `POST /api/watchlist`, `DELETE /api/watchlist/{symbol}`, `PATCH /api/watchlist/{symbol}/toggle` | 401 | 403 | ✓ |
| `POST /api/cron/trigger` | 401 | 403 | ✓ |

- Untuk role `user`, handler di `main.py` menghapus `journal_status` dari setiap item `/api/signals` (`db.py:626`) dan `journal` dari `/api/signals/{id}` (`db.py:647`). Cukup 2 baris `pop`, dan `db.py` tidak perlu diubah.
- `/api/health` tetap publik untuk deploy gate (`deploy.yml:107`) dan healthcheck Docker. `/api/info` tetap publik karena tidak berisi data trade.
- **Nasib `API_SECRET_KEY`:** opsional, dan hanya dipakai untuk akses mesin dengan hak administrator lewat `Authorization: Bearer`. Dashboard tidak lagi memakainya.

---

## 8. API baru/berubah

Semua endpoint memakai `success()`/`Envelope` dan `AppException` yang sudah ada. Model request memakai `Field(pattern=..., min_length=..., max_length=...)` seperti `WatchlistAddRequest` (`main.py:116`).

| Method dan path | Auth | Body | `data` saat sukses | Error |
|---|---|---|---|---|
| `POST /api/auth/register` | Publik + Origin | `{email, username, password}` | `{id, email, username, role, created_at}` (201), dengan Set-Cookie | 422, 409, 403, 429 |
| `POST /api/auth/login` | Publik + Origin | `{email, password}` | Objek user yang sama (200), dengan Set-Cookie | 401, 422, 403, 429 |
| `POST /api/auth/logout` | Origin | Tidak ada | `null`, cookie dihapus | 403 |
| `GET /api/auth/me` | Sesi atau API key | Tidak ada | Objek user. Untuk API key: `{id: null, username: "api-key", role: "administrator"}` | 401 |
| Semua `GET` data (`/api/watchlist`, `/signals*`, `/summary`, `/cron/status`) | **Berubah**: wajib `get_principal` | | Sama. Field journal dihapus untuk `user`. | 401 |
| `GET /api/journal*` | **Berubah**: `require_admin` | | Sama | 401, 403 |
| Endpoint tulis yang sudah ada | `verify_api_key` diganti `require_admin` | | Sama | 401, 403 |

---

## 9. Dashboard

**Yang pindah:**
- `src/static/index.html` dipindah dengan `git mv` ke `src/pages/dashboard.html`. Markup dan CSS khusus dashboard tetap inline.
- Token `:root` dan reset (`index.html:18-70`) dipindah ke `src/static/base.css` lalu di-`<link>`.
- Script tag dinaikkan dari `app.js?v=4` ke `?v=5`.
- `app.js` tetap di `src/static/`.

**Perubahan `app.js`:**
- Hapus `API_KEY_STORAGE`, `LEGACY_API_KEY_STORAGE`, `getApiKey`, `setKeyHeader`, dan `prompt()` di baris 259. `authFetch` cukup jadi `fetch` biasa, karena request same-origin otomatis membawa cookie.
- Satu titik penanganan status untuk `authFetch` dan `getJson` (`app.js:291`):
  - **401** menjalankan `location.assign('/login')`.
  - **403** menampilkan "Administrator only".
- Saat init, panggil `GET /api/auth/me` lalu set `document.body.dataset.role`. Username dirender lewat `textContent` saja, tidak pernah lewat `innerHTML`.
- Pembersihan sekali jalan: `try { localStorage.removeItem('crossline_api_key'); localStorage.removeItem('tokyo_api_key') } catch {}`.
- Tombol **Log out** menjalankan `POST /api/auth/logout` lalu `location.assign('/')`.

**Perubahan UI:**
- **Topbar** (`dashboard.html`, dulu `index.html:725-734`): tambahkan username, badge role, dan tombol Log out.
- Kontrol admin diberi class `admin-only` lalu disembunyikan dengan `body[data-role="user"] .admin-only { display: none }`. Kontrol tersebut:
  - Run now (`index.html:767-771`)
  - Form tambah watchlist, serta toggle dan hapus watchlist
  - Tab Journal (`#tab-journal`) dan filter `#filter-journal`
  - Form journal di `<dialog id="signal-modal">`

  Server tetap menegakkan semuanya dengan 403.

---

## 10. Struktur file frontend & CSP

```
src/pages/                  (baru, TIDAK di-mount)
  landing.html              tanpa JS; CSS khusus halaman di <style>
  login.html                <script src="/static/auth.js?v=1">
  register.html             <script src="/static/auth.js?v=1">
  dashboard.html            (git mv dari src/static/index.html) <script src="/static/app.js?v=5">
src/static/
  base.css                  (baru) token :root, reset, tipografi, .btn, .input, .field-error, .auth-card
  auth.js                   (baru, sekitar 100 baris) dipakai login & register, cabang berdasarkan id form
  app.js                    (diubah)
```

- Keempat halaman me-link `/static/base.css` dan Google Fonts yang sama.
- `auth.js` menangani validasi klien (aturan sama dengan server), fetch JSON, memetakan `errors[]` ke field, menangani 429 dengan `Retry-After`, lalu `location.assign('/dashboard')`.
- **CSP: tidak berubah** (`APP_CSP`, `middleware.py:25-29`). Alasannya:
  - Script hanya dari `'self'`, tanpa inline script, `on*=` atau `javascript:`.
  - Style dari `'self'`, `'unsafe-inline'` dan Google Fonts.
  - Fetch hanya same-origin (`connect-src 'self'`).
  - Form tetap memenuhi `form-action 'self'`.
  - Gambar hanya inline SVG.
- Test CSP di `test_frontend.py:200-204` diperluas ke keempat halaman.

---

## 11. Keamanan & rate limit

| Ancaman | Mitigasi |
|---|---|
| Pencurian sesi lewat XSS | Cookie HttpOnly. Username dirender lewat `textContent`. CSP tanpa inline script. |
| CSRF dan login CSRF | SameSite=Lax ditambah `check_same_origin` di semua request tulis lewat cookie, termasuk login, register dan logout. |
| Bypass halaman lewat `/static/*.html` | HTML dipindah ke `src/pages/` yang tidak di-mount. |
| Kebocoran DB | Password disimpan sebagai scrypt. Token sesi disimpan sebagai sha256. |
| Eskalasi role | Body register tidak bisa menentukan role. Admin hanya dibuat lewat CLI. Role dibaca live di setiap request. |
| Enumerasi akun | Login memakai pesan dan waktu respons yang sama. Register **masih** bisa membocorkan lewat 409, dan ini risiko yang diterima karena tidak ada email verifikasi. Dibatasi oleh bucket `auth`. |
| Credential stuffing | 5 percobaan per menit per IP, ditambah sekitar 50 ms scrypt per percobaan. Sengaja **tanpa** lockout per akun supaya penyerang tidak bisa mengunci admin. Serangan terdistribusi adalah risiko yang diterima. |
| Session fixation | Token baru setiap kali login. |
| Cache halaman privat | `/dashboard` dikirim dengan `no-store`. JSON sudah `no-store` (`middleware.py:73-74`). |
| CORS | Selama `CORS_ORIGINS='*'`, `allow_credentials` tetap mati (`src/api/__init__.py:47`). Kalau origin didaftarkan eksplisit, credentials menyala untuk origin tersebut, jadi **daftarkan hanya origin tepercaya**. Request tulis lewat cookie dari origin itu tetap ditolak cek Origin, jadi origin lain harus memakai Bearer. |

**Rate limit** (`src/api/rate_limit.py`):
- Tambah bucket `"auth"` untuk `{"/api/auth/login", "/api/auth/register"}`, mengikuti pola `TRIGGER_PATH`.
- Setting baru `RATE_LIMIT_AUTH_PER_MINUTE=5`, ikut divalidasi di `min(...) >= 1` (`settings.py:30`), dan disambungkan di `setup_http` (`src/api/__init__.py:36-41`).
- Logout dan me masuk ke bucket yang sudah ada.
- nginx tetap membatasi 10 r/s per IP.

**Logging:** `_SENSITIVE_KEY` (`log.py:10`) sudah mencakup `password`, `cookie`, `token` dan `authorization`, dan body request tidak pernah di-log. Tidak ada perubahan kode, cukup tambah test bahwa header `cookie` disanitasi.

---

## 12. Tests

**Diubah:**

| File | Perubahan |
|---|---|
| `tests/test_frontend.py` | `INDEX_HTML` diganti ke `src/pages/dashboard.html` (:12-13). Script tag `?v=5` (:142, :204). Assertion `` `Bearer ${key}` `` (:190) diganti "tidak ada `prompt(`" dan "tidak membaca `crossline_api_key`". Cek CSP dijalankan untuk 4 halaman dan `auth.js`. Fixture POST dengan Bearer API key (:32) tetap jalan. |
| `tests/test_auth.py` | Hapus test `verify_api_key` (:28-63) dan ganti dengan test `get_principal`: Bearer valid menjadi admin, key salah 401, key kosong atau placeholder 401 (sebelumnya 503). Test :115 menjadi 401. Teks warning lifespan disesuaikan. |
| `tests/test_api.py` | Test 503 saat key kosong (:327-332) menjadi 401. Test GET mengirim `AUTH_HEADERS`. `securitySchemes`/`security` di :353-354 **tetap** `HTTPBearer`, karena cookie dibaca lewat `request.cookies` dan tidak menambah scheme OpenAPI. |
| `tests/test_http_foundation.py` | :82 memakai `security.require_admin`, dan :243 menjadi 401. :382 dan :499 (`GET /` dengan `APP_CSP`) tetap lolos. |
| `tests/test_cron_lock.py` | :243 dan :249 override `main.require_admin`, bukan `main.verify_api_key`. |
| `tests/test_journal.py` | API key tetap admin, jadi fixture tidak berubah. |

**Baru (`tests/test_users.py`):**
- Hash scrypt bisa diverifikasi ulang, password salah gagal, salt berbeda untuk password yang sama, dan format hash bisa di-parse.
- Register mengembalikan 201 dengan cookie `HttpOnly`, `SameSite=Lax` dan `Secure`, serta role `user`. Kalau body mengirim `role: "administrator"`, nilainya diabaikan.
- Email atau username duplikat menghasilkan 409. Input tidak valid menghasilkan 422 per field.
- Login dengan email tak dikenal dan dengan password salah menghasilkan 401 dengan pesan identik.
- Alur register, me, logout, lalu me lagi menghasilkan 401 di akhir. Sesi kedaluwarsa menghasilkan 401.
- Role `user` mendapat 403 di `POST /api/watchlist`, `POST /api/cron/trigger`, `PUT /api/signals/{id}/journal` dan `GET /api/journal`. `/api/signals` untuk `user` tidak memuat `journal_status`.
- Tanpa sesi, `GET /api/signals` menghasilkan 401, sedangkan `/api/health` dan `/api/info` tetap 200.
- `GET /dashboard` tanpa sesi menghasilkan 303 ke `/login`. Dengan sesi, 200 dengan `no-store`. `/login` dengan sesi menghasilkan 303 ke `/dashboard`.
- `Origin` asing pada login dan pada PUT lewat cookie menghasilkan 403. Bearer tanpa Origin tetap lolos.
- Bucket `auth` mengembalikan 429 setelah 5 percobaan (rate limit dinyalakan khusus di test ini).
- `/static/dashboard.html` dan `/static/index.html` menghasilkan 404.
- `create_admin` membuat admin, memperbarui user yang sudah ada, dan menghapus sesinya.

---

## 13. Urutan implementasi (seukuran commit)

1. **`settings.py` dan `.env.example`:** tambahkan `SESSION_TTL_HOURS=168`, `SESSION_COOKIE_SECURE=true` dan `RATE_LIMIT_AUTH_PER_MINUTE=5` beserta validasinya. Tulis ulang komentar `API_SECRET_KEY` menjadi "optional, machine/CI admin access via Bearer".
2. **`src/api/passwords.py`** beserta unit test-nya.
3. **`db.py`:** `UserModel`, `SessionModel`, dan fungsi user/sesi.
4. **`security.py`:** `get_principal`, `require_admin` dan `check_same_origin`. Hapus `verify_api_key`. Sesuaikan `test_auth.py`, `test_http_foundation.py` dan `test_cron_lock.py`.
5. **`main.py`:**
   - Endpoint `/api/auth/*`.
   - Pergantian dependency di semua route sesuai §7, termasuk penghapusan field journal untuk `user`.
   - Teks warning lifespan.
   - `APP_VERSION` dinaikkan ke `3.1.0` (`main.py:101`).
   - Bucket `auth` di `rate_limit.py` dan `setup_http`.
6. **`scripts/create_admin.py`.**
7. **Pemindahan halaman:**
   - `git mv src/static/index.html src/pages/dashboard.html`.
   - Ekstrak `base.css`.
   - Route `/`, `/login`, `/register` dan `/dashboard` lewat helper `_page`.
8. **Login dan register:** `login.html`, `register.html` dan `auth.js`.
9. **`app.js`:** hapus API key, tambahkan penanganan 401/403, `me`, topbar, logout dan `admin-only`. Naikkan ke `?v=5`.
10. **`landing.html`.**
11. **Test:** perbarui `test_frontend.py` dan `test_api.py`, buat `tests/test_users.py`, lalu jalankan `pytest` dan `ruff` hingga hijau.
12. **README:**
    - Layout (:123).
    - Paragraf dashboard dan key (:139).
    - Daftar halaman tanpa envelope (:145), yang sekarang mencakup `/login`, `/register` dan `/dashboard`.
    - Bagian Authentication (:189-191): login, role, CLI admin dan API key mesin.
13. **Uji manual di browser:**
    - Landing di 320, 768 dan 1280 px.
    - Register, login dan logout.
    - User non-admin tidak melihat kontrol admin.
    - Console bebas pelanggaran CSP.

---

## 14. Deploy & migrasi

1. Tambahkan ke `.env` server: `SESSION_COOKIE_SECURE=true` (situs sudah HTTPS lewat nginx), serta opsional `SESSION_TTL_HOURS` dan `RATE_LIMIT_AUTH_PER_MINUTE`.
2. Deploy seperti biasa. `create_all` membuat tabel `users` dan `user_sessions`, dan tabel lama tidak disentuh.
3. Buat administrator pertama:
   ```
   docker compose -f docker-compose.prod.yml exec app python -m scripts.create_admin --email ... --username ...
   ```
   Sampai langkah ini selesai, tidak ada yang bisa memakai aksi admin di UI. Cron terjadwal tetap berjalan, dan curl dengan API key tetap bisa dipakai.
4. Login di `/login`, lalu cek `/dashboard`. Browser lama akan menghapus API key tersimpannya saat pertama kali membuka dashboard.
5. **Perubahan yang bisa merusak:** klien eksternal yang membaca `GET /api/*` tanpa auth akan mendapat 401, jadi tambahkan Bearer API key. Kalau `API_SECRET_KEY` kosong, respons sekarang 401, bukan lagi 503.
6. Rollback cukup dengan image lama. Kode lama mengabaikan tabel baru.

---

## 15. Keputusan terbuka (default yang direkomendasikan **tebal**)

| # | Pertanyaan | Default |
|---|---|---|
| 1 | Apakah registrasi dibuka untuk umum? | **Ya, dengan user baru sebagai viewer read-only.** Tambahkan flag `REGISTRATION_ENABLED` hanya kalau muncul spam. |
| 2 | Apakah journal per user, sehingga `user` juga bisa menulis journal? | **Belum.** Kalau dibutuhkan, perlu migrasi manual: `ALTER TABLE signal_journal ADD user_id INTEGER REFERENCES users(id) ON DELETE CASCADE`, lalu backfill ke admin pertama, `SET NOT NULL`, drop `signal_journal_signal_id_key`, dan `UNIQUE (user_id, signal_id)`. Di kode: upsert `index_elements=["user_id","signal_id"]` (`db.py:837`) dan filter `user_id` di `db.py:625, 643, 892-930`. |
| 3 | Apakah landing menampilkan statistik live? | **Tidak.** `/api/summary` sekarang wajib login, dan mempublikasikan win rate adalah keputusan bisnis. Opsi murah: fetch parameter strategi dari `/api/info`, yang publik, dengan sekitar 15 baris JS. |
| 4 | Bahasa UI halaman baru? | **English, mengikuti dashboard.** Teks Indonesia bisa diganti belakangan. |
| 5 | Login dengan username juga? | **Tidak, email saja.** |
| 6 | Panjang minimal password? | **10.** NIST 800-63B rev.4 menyarankan 15 untuk password tanpa MFA. Naikkan kalau user tidak keberatan. |
| 7 | Halaman manajemen user (daftar user, ubah role)? | **Tidak.** CLI `create_admin` sudah cukup. |
| 8 | Apakah `/docs` tetap publik? | **Ya.** Skema API bukan rahasia. |
| 9 | Light mode untuk landing? | **Ditunda.** Ikuti tema gelap yang sudah ada. |
| 10 | Hapus `'unsafe-inline'` dari `style-src`? | **Nanti.** Semua `<style>` inline harus dipindah ke file dan atribut `style=` yang dibangun `app.js` diaudit dulu. |
