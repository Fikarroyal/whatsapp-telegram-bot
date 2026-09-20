# Asisten Harian Bot WhatsApp & Telegram

| Fitur | Isi |
|---|---|
| Sholat | Jadwal Kemenag RI, pengingat tiap waktu sholat, pengingat *N menit sebelum* waktu sholat, info sholat berikutnya + hitung mundur |
| Cuaca | Kondisi saat ini + penilaian **baik / kurang baik / buruk** beserta saran (payung, panas, angin, badai), kabar cuaca otomatis tiap pagi |
| Keuangan | Pemasukan & pengeluaran tercatat otomatis dari chat biasa, kategori otomatis, saldo, rekap harian/bulanan, riwayat |
| Tugas | Tugas sekali jalan atau harian, pengingat pada jam tertentu, ringkasan agenda tiap pagi |
| Tidur | Pengingat tidur + hitungan durasi tidur sampai alarm |
| Alarm | Alarm bangun pagi yang mengulang sampai kamu membalas `bangun`, lalu langsung memberi ringkasan pagi |


## 1. Isi folder

| File | Fungsi |
|---|---|
| `core.py` | Seluruh logika: perintah, database SQLite, penjadwal pengingat |
| `wa_bot.py` | Adapter **WhatsApp Cloud API** (server webhook Flask) |
| `bot.py` | Adapter **Telegram** |
| `test_bot.py` | 43 uji otomatis (tanpa internet dan tanpa token) |
| `requirements.txt` | Dependensi untuk WhatsApp |
| `requirements-telegram.txt` | Dependensi untuk Telegram |

Butuh **Python 3.9+**. Data tersimpan di `botdata.db` (SQLite) di folder tempat bot dijalankan.


## 2. Perintah

Di WhatsApp ketik langsung (tanpa `/`). Di Telegram, `/` di depan boleh dipakai maupun tidak.

**Sholat**
| Ketik | Hasil |
|---|---|
| `sholat` | Jadwal hari ini, sholat berikutnya + hitung mundur |
| `sholat on` / `sholat off` | Nyalakan/matikan pengingat tiap waktu sholat |
| `sholat menit 15` | Ingatkan 15 menit sebelum waktu sholat (`0` = mati, boleh 5–60) |
| `kota Bandung` | Ganti kota (zona WIB/WITA/WIT ikut menyesuaikan). Default: Yogyakarta |

**Cuaca**
| Ketik | Hasil |
|---|---|
| `cuaca` / `cuaca Surabaya` | Kondisi + penilaian baik/buruk + saran |
| `cuaca on 06:30` | Kabar cuaca otomatis tiap hari jam 06:30 (`cuaca off` untuk mematikan) |

**Keuangan**
| Ketik | Hasil |
|---|---|
| `25rb makan siang` | Pengeluaran Rp25.000 (kategori otomatis) |
| `+5jt gaji` | Pemasukan Rp5.000.000 |
| `masuk 150k bonus` / `keluar 1,5jt bayar kos` | Cara eksplisit. Format angka: `25000`, `25rb`, `25k`, `1,5jt`, `Rp12.500` |
| `saldo` | Saldo total + ringkasan bulan ini |
| `rekap` | Hari ini, bulan ini, selisih, saldo, 5 kategori pengeluaran terbesar |
| `riwayat` / `hapus 12` | 10 catatan terakhir / hapus catatan nomor 12 |

Untuk memulai dengan saldo awal, catat sebagai pemasukan: `masuk 2jt saldo awal`.

**Tugas**
| Ketik | Hasil |
|---|---|
| `tugas kirim laporan @14:00` | Tugas + pengingat jam 14:00 (jika jam sudah lewat, otomatis untuk besok) |
| `tugas beli beras` | Tugas tanpa pengingat |
| `tugas rapat besok @09:00` | Tugas untuk besok |
| `tugas harian minum obat @08:00` | Diingatkan **setiap hari** jam 08:00 |
| `tugas` | Daftar tugas |
| `selesai 3` / `tugas hapus 3` | Tandai selesai / hapus |
| `agenda on 07:00` | Ringkasan tugas tiap pagi jam 07:00 (`agenda off`) |

**Tidur & bangun**
| Ketik | Hasil |
|---|---|
| `tidur on 22:00` / `tidur off` | Pengingat tidur harian |
| `alarm on 04:30` / `alarm off` | Alarm bangun pagi. Diulang 3× (tiap 3 menit) sampai kamu balas `bangun` |
| `bangun` | Matikan alarm + ringkasan pagi (sholat berikutnya, cuaca, agenda) |

**Lainnya:** `menu` (bantuan), `status` (semua pengaturanmu).


## 3. Pasang di WhatsApp (Meta Cloud API)

### 3.1 Siapkan di Meta
1. Buka <https://developers.facebook.com>, login, **Create App** → tipe **Business** → tambahkan produk **WhatsApp**.
2. Di *WhatsApp → API Setup* catat **Phone Number ID** dan **Temporary access token**. Meta menyediakan nomor tes gratis.
3. Di bagian *To*, tambahkan nomor WhatsAppmu sebagai penerima tes (akan diminta kode verifikasi).
4. Untuk memakai app-secret verifikasi (disarankan): *App settings → Basic → App secret*.

### 3.2 Jalankan bot
```bash
pip install -r requirements.txt

# Linux/Mac                         # Windows (CMD)
export WA_TOKEN="token"             set WA_TOKEN=token
export WA_PHONE_ID="phone_id"       set WA_PHONE_ID=phone_id
export WA_VERIFY_TOKEN="bebas_isi"  set WA_VERIFY_TOKEN=bebas_isi
export WA_APP_SECRET="app_secret"   set WA_APP_SECRET=app_secret

python wa_bot.py
```
Jalankan **satu proses saja** (penjadwal pengingat ada di dalam proses yang sama).

### 3.3 Buka ke internet & daftarkan webhook
1. Meta butuh alamat **HTTPS publik**. Untuk uji coba: `ngrok http 5000` → salin URL `https://xxxx.ngrok-free.app`.
2. *WhatsApp → Configuration → Webhook → Edit*:
   - **Callback URL**: `https://xxxx.ngrok-free.app/webhook`
   - **Verify token**: sama persis dengan `WA_VERIFY_TOKEN`
3. Klik **Verify and save**, lalu **Manage** → centang **messages** → Subscribe.
4. Dari WhatsApp-mu, kirim `menu` ke nomor tes.

### 3.4 Pengingat di WhatsApp punya batas 24 jam
WhatsApp hanya mengizinkan bot mengirim pesan bebas **dalam 24 jam setelah kamu terakhir membalas**. Selama kamu chat dengan bot setidaknya sekali sehari, semua pengingat berjalan normal. Kalau lebih dari 24 jam kamu diam, Meta menolak pesan bebas (kode error 131047).

Solusinya: **template pesan**. Bot otomatis beralih ke template saat kena batas itu, kalau kamu mengisi `WA_TEMPLATE_NAME`.

Cara membuat template:
1. *WhatsApp Manager → Message templates → Create template*.
2. Kategori **Utility**, bahasa **Indonesian**, nama mis. `pengingat_bot`.
3. Isi body persis seperti ini (variabel tidak boleh di awal atau akhir):
   ```
   Halo! Ini pengingat dari asisten harianmu: {{1}} Balas pesan ini supaya pengingat berikutnya bisa dikirim langsung.
   ```
   Contoh isi variabel: `Waktunya sholat Dzuhur (11:46)`.
4. Tunggu disetujui Meta, lalu jalankan bot dengan `WA_TEMPLATE_NAME=pengingat_bot`.

Catatan: pesan template yang dikirim bisnis bisa **dikenai biaya** menurut tarif Meta. Kalau kebutuhan utamamu adalah pengingat (alarm, sholat, tugas), **Telegram lebih cocok** karena tanpa batasan 24 jam dan gratis.

### 3.5 Token permanen & nomor sungguhan
- Token sementara habis dalam ±24 jam. Buat **System User** di *Business Settings* lalu generate token permanen dengan izin `whatsapp_business_messaging`.
- Untuk dipakai orang lain, daftarkan nomor telepon khusus untuk bot dan lengkapi verifikasi bisnis di Meta.
- Versi Graph API bisa diganti dengan `WA_GRAPH_VERSION` (default `v23.0`) kalau Meta mempensiunkan versi lama.


## 4. Pasang di Telegram
1. Chat **@BotFather** → `/newbot` → salin **token**.
2. ```bash
   pip install -r requirements-telegram.txt
   export TELEGRAM_TOKEN="token"      # Windows: set TELEGRAM_TOKEN=token
   python bot.py
   ```
3. Buka botmu di Telegram, kirim `/start`. Tidak butuh domain, HTTPS, atau webhook.


## 5. Cara kerja pengingat
- Penjadwal berjalan **tiap menit**. Setiap pengingat punya toleransi 5 menit kalau bot sempat telat/mati sebentar.
- Setiap pengingat dicatat di database, jadi **tidak dikirim dobel** walau bot di-restart.
- Zona waktu mengikuti kota yang kamu set (WIB/WITA/WIT), termasuk untuk alarm, tidur, cuaca, dan tugas.
- Jadwal sholat diambil dari Aladhan API dengan metode **Kemenag RI** untuk **tanggal lokalmu**, lalu di-cache seharian.
- Setelah Isya, "sholat berikutnya" memakai jam Subuh hari ini sebagai perkiraan Subuh besok (selisihnya biasanya hanya ±1 menit).
- Penilaian cuaca memakai kondisi saat ini: badai/hujan lebat/angin ≥50 km/jam/terasa ≥42°C = **buruk**; hujan ringan, gerimis, kabut, angin ≥30 km/jam, terasa ≥38°C, atau sedang ada curah hujan = **kurang baik**; selain itu **baik** (plus peringatan bila peluang hujan hari ini ≥50%).


## 6. Menjalankan 24 jam
Bot harus terus menyala agar pengingat jalan. Pilihan umum:
- **VPS kecil** (mis. Ubuntu) dengan `systemd` atau `tmux`/`screen`, plus Caddy/Nginx untuk HTTPS (khusus WhatsApp).
- **Railway/Render/Fly.io**: pastikan file `botdata.db` ada di *persistent volume*, kalau tidak data hilang saat deploy ulang. Jalankan dengan **1 instance** saja.

Cadangkan `botdata.db` secara berkala; isinya seluruh catatan keuanganmu.


## 7. Pengujian
```bash
python test_bot.py
```
43 tes, selesai dalam beberapa detik, tanpa internet dan token. Jam disimulasikan menit demi menit, dan API Aladhan/Open-Meteo/Meta diganti respons palsu yang bentuknya disalin dari respons asli.

Yang diuji: seluruh parser angka dan jam, alur keuangan (saldo, kategori, hapus, saldo negatif, pergantian hari/bulan), jadwal sholat sehari penuh (10 pengingat tepat di menit yang benar), tanpa duplikasi setelah restart, tanggal lokal vs UTC, WITA vs WIB, 9 skenario penilaian cuaca, retry saat API cuaca down, tugas sekali/harian/selesai/hapus, alarm berulang dan `bangun`, pengingat tidur, isolasi antar pengguna, satu fitur error tidak menghalangi fitur lain, verifikasi webhook + tanda tangan WhatsApp, deduplikasi pesan Meta, fallback template 24 jam, adapter Telegram, dan **simulasi 3 hari** dengan semua fitur menyala (tepat 18 pesan per hari, tanpa kembar).

**Belum bisa diuji dari sini:** koneksi langsung ke server Meta/Telegram dan API cuaca/sholat sungguhan, karena butuh token akunmu dan jaringan bebas. Karena itu langkah pertama setelah dipasang adalah tes cepat: kirim `menu`, `sholat`, `cuaca`, `25rb kopi`, `saldo`, lalu `alarm on <jam 2 menit dari sekarang>`.


## 8. Troubleshooting
| Gejala | Penyebab & solusi |
|---|---|
| Verify webhook gagal | `WA_VERIFY_TOKEN` tidak sama dengan yang diisi di Meta, atau URL ngrok berubah (ngrok gratis berganti URL tiap restart) |
| Bot tidak membalas di WhatsApp | Field **messages** belum di-subscribe; nomormu belum ditambahkan sebagai penerima tes; token sementara sudah habis (lihat log: `Kirim WA gagal`) |
| Log `131047` | Sudah >24 jam sejak kamu terakhir chat. Buat template (bagian 3.4) |
| `403 Bad signature` | `WA_APP_SECRET` salah. Hapus variabelnya untuk uji, atau salin ulang dari *App settings → Basic* |
| `Kota tidak ditemukan` | Pakai nama kota/kabupaten yang umum, mis. `Surakarta`, `Makassar`, `Denpasar` |
| Error `ZoneInfoNotFoundError` di Windows | `pip install tzdata` (sudah ada di requirements) |
| Pengingat telat/tidak muncul | Cek bot masih berjalan; cek `status`; pastikan hanya **satu** proses bot yang aktif |
| Jadwal sholat beda 1–2 menit dari masjid | Aladhan memakai perhitungan Kemenag tanpa *ihtiyath*; masjid sering menambah beberapa menit. Sesuaikan kebiasaan lokalmu |
