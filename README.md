# Server_penyimpanan

Backend **Personal Cloud Storage** untuk Raspberry Pi 3 Model B+ (RAM 1 GB), dipakai oleh aplikasi Android
untuk menyimpan foto, video, dan dokumen. Dibangun dengan FastAPI + SQLite, tanpa Docker, tanpa layanan cloud.

> **Status: dalam pengembangan.** Tahap yang sudah selesai: kerangka proyek, database + migrasi,
> autentikasi, admin pengguna, file & folder management, dan upload besar (chunked/resumable).
> Media streaming, storage/backup, endpoint trash, dan deployment systemd menyusul.

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

Format error seragam: `{"detail": "<pesan>", "code": "<kode>"}`.

**Catatan untuk aplikasi Android:** simpan refresh token di penyimpanan terenkripsi, dan jangan kirim
dua request refresh bersamaan dengan token yang sama. Pemakaian ulang refresh token dianggap
pencurian dan seluruh sesi akan dicabut.

## Lisensi

GPL-3.0, lihat [LICENSE](LICENSE).
