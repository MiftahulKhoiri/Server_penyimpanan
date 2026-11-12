# Server_penyimpanan

Backend **Personal Cloud Storage** untuk Raspberry Pi 3 Model B+ (RAM 1 GB) yang dipakai aplikasi
Android untuk menyimpan, melihat, mengunggah, dan mengunduh foto, video, serta dokumen.
Dibangun dengan FastAPI, Uvicorn, SQLite, dan SQLAlchemy, tanpa Docker dan tanpa layanan cloud.
Data disimpan di SSD dan di-backup ke HDD.

## Dokumentasi

| Dokumen | Isi |
|---|---|
| **[docs/API.md](docs/API.md)** | Panduan API untuk developer aplikasi: autentikasi & refresh token, upload chunked & resume, streaming video (ExoPlayer), kode error, contoh `curl` & Kotlin |
| **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** | Instalasi di Raspberry Pi: SSD/HDD & fstab, systemd, konfigurasi, akses dari luar rumah, update, backup, restore, pemecahan masalah |
| **[docs/TESTING.md](docs/TESTING.md)** | Cara menjalankan tes, pemetaan tes wajib, daftar bug yang ditemukan lewat pengujian |
| **[android/README.md](android/README.md)** | Aplikasi Android: fitur, cara instal APK (rilis `android-latest`), build & tes |
| [docs/openapi.json](docs/openapi.json) | Spesifikasi OpenAPI untuk generator client (Kotlin dsb.) |
| `http://<server>:8000/docs` | Swagger interaktif dari server yang sedang berjalan |

## Fitur

| Area | Fitur |
|---|---|
| Akun & keamanan | Login, logout per perangkat / semua perangkat, refresh token yang dirotasi (deteksi pencurian), ganti password, sesi perangkat, role admin/user, Argon2id, pembatasan login, CORS daftar putih |
| File & folder | Folder bertingkat, upload, download dengan `Range`, rename, pindah, salin, cari, sort, pagination, deteksi MIME dari isi file |
| Upload besar | Chunked & resumable hingga 64 GiB, di-stream ke disk (RAM tidak naik), SHA-256 per chunk & total, deteksi duplikat, kuota, cek ruang SSD |
| Media | Galeri foto/video, streaming video dengan `Range` (seek), thumbnail (Pillow hemat RAM + ffmpeg), durasi & resolusi video |
| Trash | Soft delete, restore (folder sebagai satu kesatuan), hapus permanen, purge otomatis setelah 30 hari |
| Storage & backup | Status SSD/HDD, backup inkremental ke HDD dengan verifikasi SHA-256 + backup database, restore dari HDD; file asli tidak pernah dihapus |
| Ketahanan | Mode degraded bila SSD tidak ter-mount (tidak pernah menulis ke kartu SD), pulih otomatis saat disk terpasang; HDD opsional |
| Administrasi | Kelola pengguna, dashboard (CPU, RAM, suhu, uptime, storage), statistik, activity log |
| Deployment | Script instalasi idempoten, systemd dengan user non-root & hardening, update, restore |

## Instalasi di Raspberry Pi

```bash
git clone https://github.com/MiftahulKhoiri/Server_penyimpanan.git
cd Server_penyimpanan
python3 scripts/check_storage.py            # cek SSD & HDD (read-only)
sudo ./scripts/install_pi.sh --admin admin  # pasang, buat admin, jalankan otomatis saat boot
```

Siapkan SSD/HDD lebih dulu; langkahnya ada di [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#3-siapkan-ssd-dan-hdd).
Perintah sehari-hari:

```bash
M=/opt/server_penyimpanan/scripts/manage.sh
sudo $M status          # status + health
sudo $M logs            # log realtime
sudo $M backup-now      # backup ke HDD
sudo ./scripts/update.sh   # update ke versi terbaru (dari folder clone)
```

| Script | Fungsi |
|---|---|
| `scripts/install_pi.sh` | Instalasi/pemasangan ulang (idempoten; konfigurasi & data tidak ditimpa) |
| `scripts/update.sh` | Backup DB → `git pull` → pasang ulang → restart → cek health |
| `scripts/manage.sh` | `status`, `logs`, `check-storage`, `create-admin`, `backup-now`, `restore`, ... |
| `scripts/check_storage.py` | Periksa mount, filesystem, UUID, fstab (`nofail`), izin tulis SSD/HDD |
| `scripts/prepare_storage.sh` | Buat direktori data di disk yang **sudah** ter-mount (tidak pernah memformat) |
| `scripts/restore_backup.py` | Restore database/file dari HDD (simulasi kecuali `--apply`, verifikasi SHA-256) |
| `scripts/export_openapi.py` | Perbarui `docs/openapi.json` |
| `scripts/uninstall.sh` | Lepas layanan & kode; data tidak dihapus |

## Menjalankan untuk pengembangan

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env && chmod 600 .env
# Isi SECRET_KEY. Tanpa SSD: REQUIRE_MOUNTED_STORAGE=false dan arahkan semua path
# ke folder di luar direktori proyek.
.venv/bin/python scripts/init_db.py
.venv/bin/python scripts/create_admin.py --username admin
scripts/run_server.sh
```

Semua pengaturan beserta penjelasannya ada di [`.env.example`](.env.example).

## Testing

```bash
.venv/bin/python -m pytest          # 197 tes otomatis (±40 detik)
tests/deploy/run_e2e.sh             # uji deployment end-to-end (Docker + systemd + Python 3.11)
```

Tes juga memeriksa bahwa dokumentasi tidak melenceng dari kode: semua endpoint dan kode error
ada di `docs/API.md`, dan `docs/openapi.json` selalu terbaru. Detail ada di [docs/TESTING.md](docs/TESTING.md).

## Struktur

```
app/
  main.py          application factory (lifespan, middleware, pemeliharaan berkala)
  core/            konfigurasi, keamanan (Argon2/JWT), dependency, error, batas body, rate limit
  api/v1/          router: auth, files, folders, uploads, media, storage, trash, activity, admin, system
  models/          model SQLAlchemy (10 tabel)
  schemas/         skema request/response Pydantic
  services/        logika bisnis: file, folder, upload, media, backup, trash, storage, auth, ...
  database/        engine SQLite (WAL), tipe UTC, migrasi programatik
  utils/           validasi nama file, deteksi MIME, info gambar
migrations/        Alembic
scripts/           instalasi, update, manajemen, storage, backup & restore
deploy/            unit systemd
docs/              API, deployment, testing, openapi.json
tests/             pytest, uji deployment (Docker), uji beban
```

File pengguna **tidak pernah** disimpan di direktori kode; lokasinya diatur lewat konfigurasi.

## Lisensi

GPL-3.0, lihat [LICENSE](LICENSE).
