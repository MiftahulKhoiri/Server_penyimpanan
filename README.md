# Server_penyimpanan

Backend **Personal Cloud Storage** untuk Raspberry Pi 3 Model B+ (RAM 1 GB), dipakai oleh aplikasi Android
untuk menyimpan foto, video, dan dokumen. Dibangun dengan FastAPI + SQLite, tanpa Docker, tanpa layanan cloud.

> **Status: dalam pengembangan.** Tahap yang sudah selesai: kerangka proyek, database + migrasi,
> autentikasi, admin pengguna, file & folder management, upload besar (chunked/resumable),
> media (streaming, thumbnail), status SSD/HDD, dashboard sistem, backup ke HDD, trash, dan
> activity log. Deployment Raspberry Pi (script instalasi, systemd) menyusul.

## Fitur yang sudah tersedia

| Area | Keterangan |
|---|---|
| Autentikasi | Registrasi (opsional), login, logout, logout semua perangkat, refresh token, ganti password, profil |
| Sesi perangkat | Daftar sesi aktif per perangkat, cabut sesi tertentu |
| Keamanan | Argon2id, JWT berumur pendek, refresh token dirotasi & disimpan sebagai hash, deteksi pemakaian ulang refresh token, pembatasan percobaan login, CORS daftar putih, error tanpa bocoran info |
| Admin | CRUD pengguna, ubah role, nonaktifkan akun, reset password, perlindungan admin terakhir |
| Database | SQLite (WAL, foreign key aktif), 10 tabel, migrasi Alembic otomatis + backup DB sebelum migrasi |
| Storage guard | Menolak operasi bila SSD tidak ter-mount; tidak pernah menulis ke direktori mount kosong |
| Health | `GET /api/v1/system/health` |

## Struktur

```
app/
  main.py              application factory
  core/                config, security, dependencies, errors, rate limit, logging
  api/v1/              router: auth, admin, system
  models/              model SQLAlchemy (10 tabel)
  schemas/             skema request/response Pydantic
  services/            logika bisnis (auth, user, activity log, storage guard)
  database/            engine/sesi, tipe UTC, migrasi programatik
  utils/
migrations/            Alembic (versions/0001_initial_schema.py)
scripts/               init_db.py, create_admin.py
tests/                 pytest
```

File pengguna **tidak** pernah disimpan di direktori kode; lokasinya diatur lewat `.env`.

## Menjalankan untuk pengembangan

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env && chmod 600 .env
# Edit .env: isi SECRET_KEY, dan untuk dev tanpa SSD set REQUIRE_MOUNTED_STORAGE=false
# serta arahkan semua path ke folder lokal di luar direktori proyek.
.venv/bin/python scripts/init_db.py
.venv/bin/python scripts/create_admin.py --username admin
.venv/bin/python run.py
```

Dokumentasi interaktif: `http://<ip-pi>:8000/docs` (matikan dengan `ENABLE_DOCS=false`).

## Testing

```bash
.venv/bin/python -m pytest
```

## Endpoint autentikasi

| Method | Path | Keterangan |
|---|---|---|
| POST | `/api/v1/auth/register` | Hanya bila `ALLOW_REGISTRATION=true` |
| POST | `/api/v1/auth/login` | `{username, password, device_name?}` → access + refresh token |
| POST | `/api/v1/auth/refresh` | `{refresh_token}` → pasangan token baru (yang lama langsung tidak berlaku) |
| POST | `/api/v1/auth/logout` | Akhiri sesi perangkat ini |
| POST | `/api/v1/auth/logout-all` | Akhiri semua sesi |
| GET | `/api/v1/auth/me` | Profil pengguna |
| POST | `/api/v1/auth/change-password` | Mencabut semua sesi, mengembalikan token baru untuk perangkat ini |
| GET | `/api/v1/auth/sessions` | Sesi perangkat aktif |
| DELETE | `/api/v1/auth/sessions/{session_id}` | Cabut satu sesi |
| GET/POST | `/api/v1/admin/users` | Daftar (pagination, pencarian) / buat pengguna |
| GET/PATCH/DELETE | `/api/v1/admin/users/{user_id}` | Detail / ubah / hapus pengguna |

## Endpoint file, folder, dan upload

| Method | Path | Keterangan |
|---|---|---|
| GET | `/api/v1/files` | Daftar file: `folder_id`, `q` (cari), `media_type`, `sort` (name/size/created_at/updated_at), `order`, `page`, `page_size` |
| POST | `/api/v1/files?filename=..&folder_id=..` | Upload sekali kirim (body mentah, ≤ `MAX_SIMPLE_UPLOAD_BYTES`), header opsional `X-Content-SHA256` |
| GET | `/api/v1/files/{id}` | Detail file |
| GET | `/api/v1/files/{id}/download` | Unduh (streaming, mendukung `Range`) |
| PATCH | `/api/v1/files/{id}/rename` | `{name}` |
| POST | `/api/v1/files/{id}/move` | `{folder_id}` (null = root) |
| POST | `/api/v1/files/{id}/copy` | `{folder_id?, name?}` |
| DELETE | `/api/v1/files/{id}` | Pindah ke trash (data fisik tidak dihapus) |
| POST/GET | `/api/v1/folders` | Buat folder / daftar subfolder (`parent_id`) |
| GET/DELETE | `/api/v1/folders/{id}` | Detail (breadcrumb, jumlah file) / pindah ke trash beserta isinya |
| PATCH | `/api/v1/folders/{id}/rename` | `{name}` |
| POST | `/api/v1/folders/{id}/move` | `{parent_id}` (anti-siklus) |
| POST | `/api/v1/uploads/init` | `{filename, total_size, chunk_size?, folder_id?, sha256?}` |
| PUT | `/api/v1/uploads/{id}/chunks/{index}` | Body mentah satu chunk, header opsional `X-Chunk-SHA256` |
| GET | `/api/v1/uploads/{id}/status` | Chunk yang sudah diterima (untuk resume) |
| POST | `/api/v1/uploads/{id}/complete` | Verifikasi SHA-256 lalu simpan file |
| POST | `/api/v1/uploads/{id}/cancel` | Batalkan dan hapus data sementara |
| GET | `/api/v1/uploads` | Upload yang masih bisa dilanjutkan |

**Alur upload besar dari Android:** `init` → kirim tiap chunk (boleh diulang bila gagal) →
bila koneksi putus, panggil `status` dan kirim chunk yang belum ada → `complete`.
Upload yang tidak disentuh selama `UPLOAD_EXPIRE_HOURS` dihapus otomatis.

Nama file hanya disimpan sebagai label; file fisik disimpan dengan nama UUID di
`PRIMARY_STORAGE_PATH/files/<user_id>/`, sehingga nama file tidak bisa dipakai untuk path traversal.
MIME ditentukan dari isi file; file yang isinya tidak cocok dengan ekstensinya ditolak (415).

## Endpoint media, storage, dan sistem

| Method | Path | Akses | Keterangan |
|---|---|---|---|
| GET | `/api/v1/media/photos` | user | Galeri foto (semua folder atau `folder_id`), sort & pagination |
| GET | `/api/v1/media/videos` | user | Daftar video |
| GET | `/api/v1/media/{id}` | user | Info media; durasi & resolusi video dibaca ffprobe sekali lalu disimpan |
| GET | `/api/v1/media/{id}/stream` | user | Streaming inline dengan HTTP Range (seek di pemutar video) |
| GET | `/api/v1/media/{id}/thumbnail` | user | Thumbnail JPEG, dibuat saat pertama diminta lalu di-cache (`ETag`) |
| GET | `/api/v1/storage/status` | user | Kapasitas SSD/HDD (ringkas) + pemakaian & kuota Anda |
| GET | `/api/v1/storage/ssd` | admin | Detail SSD: mount, filesystem, device, kapasitas |
| GET | `/api/v1/storage/hdd` | admin | Detail HDD; bila tidak ter-mount `available=false`, server tetap jalan |
| GET | `/api/v1/system/health` | publik | Status database & penyimpanan |
| GET | `/api/v1/system/status` | admin | CPU, RAM, suhu, uptime, database, SSD/HDD, ketersediaan ffmpeg |
| GET | `/api/v1/system/statistics` | admin | Jumlah pengguna, file, ukuran total, per jenis media, trash, upload aktif |

**Thumbnail** memakai Pillow (mode *draft*: JPEG didekode langsung pada skala kecil, sehingga foto
24 MP hanya menambah RAM ±6 MB) dan ffmpeg untuk video. Hanya satu thumbnail dibuat dalam satu waktu,
gambar di atas `THUMBNAIL_MAX_PIXELS` dilewati. ffmpeg bersifat opsional: tanpa ffmpeg, streaming
tetap berjalan, hanya thumbnail video & info durasi yang tidak tersedia
(`sudo apt install ffmpeg` untuk mengaktifkan).

Pemutar video Android (ExoPlayer/Media3) perlu mengirim header `Authorization: Bearer <token>`
pada request stream (lewat `DefaultHttpDataSource.Factory().setDefaultRequestProperties(...)`).

## Endpoint backup, trash, dan activity log

| Method | Path | Akses | Keterangan |
|---|---|---|---|
| POST | `/api/v1/storage/backup` | admin | Mulai backup SSD → HDD di latar belakang (202). Body opsional `{"verify_existing": true}` |
| GET | `/api/v1/storage/backup` | admin | Riwayat job backup |
| GET | `/api/v1/storage/backup/{job_id}` | admin | Status + laporan (file disalin/dilewati/gagal & alasannya, backup database) |
| GET | `/api/v1/trash` | user | Isi trash, beserta `purge_after` |
| POST | `/api/v1/trash/{id}/restore` | user | Kembalikan file/folder (`id` = ID file atau folder) |
| POST/DELETE | `/api/v1/trash/{id}/delete` | user | Hapus permanen satu item beserta data fisik & thumbnail |
| DELETE | `/api/v1/trash` | user | Kosongkan trash |
| GET | `/api/v1/activity` | user | Riwayat aktivitas sendiri (`action` persis atau awalan seperti `file.`, `since`, `until`) |
| GET | `/api/v1/admin/activity` | admin | Seluruh activity log, filter `user_id` |

**Cara kerja backup**
- Bersifat inkremental: file yang sudah tercatat ter-backup dengan ukuran sama dilewati.
  `verify_existing` menghitung ulang SHA-256 salinan di HDD dan memperbaiki salinan yang rusak.
- Setiap file disalin sambil dihitung SHA-256-nya, lalu dibandingkan dengan SHA-256 saat upload.
  Bila data di SSD ternyata rusak, salinan lama di HDD **tidak** ditimpa dan file dicatat di laporan
  (`source_checksum_mismatch`).
- Database ikut di-backup ke `BACKUP_STORAGE_PATH/database/` memakai SQLite online backup lalu dicek `integrity_check`.
- **File asli tidak pernah dihapus atau diubah.** Salinan di HDD juga tidak dihapus walau file asalnya
  dihapus (bukan mirror). Rotasi salinan database hanya aktif bila `BACKUP_DB_KEEP` > 0.
- Backup otomatis **mati secara default**. Aktifkan dengan `BACKUP_AUTO_INTERVAL_HOURS`; pengecekannya
  berjalan pada siklus pemeliharaan (`UPLOAD_CLEANUP_INTERVAL_SECONDS`).
- Bila HDD tidak tersedia, backup ditolak (503) dan server utama tetap berjalan normal.

**Trash:** hapus biasa hanya memindahkan ke trash. Item dihapus permanen otomatis setelah
`TRASH_RETENTION_DAYS` (default 30 hari), atau manual lewat endpoint trash. Folder dipulihkan sebagai
satu kesatuan; file yang sebelumnya dihapus sendiri-sendiri tetap di trash.

**Activity log** mencatat login, logout, upload, unduh, streaming, rename, pindah, salin, hapus,
restore, hapus permanen, ganti password, aksi admin, backup, dan start server (beserta ringkasan
konfigurasi). Password, token, dan secret tidak pernah dicatat.

Format error seragam: `{"detail": "<pesan>", "code": "<kode>"}`.

**Catatan untuk aplikasi Android:** simpan refresh token di penyimpanan terenkripsi, dan jangan kirim
dua request refresh bersamaan dengan token yang sama. Pemakaian ulang refresh token dianggap
pencurian dan seluruh sesi akan dicabut.

## Lisensi

GPL-3.0, lihat [LICENSE](LICENSE).
