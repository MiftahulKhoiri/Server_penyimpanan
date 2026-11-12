# Dokumentasi API Server_penyimpanan

Panduan untuk developer aplikasi Android (atau klien lain). Referensi lengkap setiap field ada di
Swagger `http://<server>:8000/docs` dan di file [`openapi.json`](openapi.json), yang bisa dipakai
untuk membuat client otomatis, misalnya dengan `openapi-generator generate -g kotlin`.

Membuat client Kotlin otomatis (sudah diuji dengan openapi-generator 7.10, 10 kelas API + 49 model):

```bash
docker run --rm -v "$PWD/docs:/spec:ro" -v "$PWD/client:/out" openapitools/openapi-generator-cli:v7.10.0 \
  generate -i /spec/openapi.json -g kotlin -o /out --additional-properties=library=jvm-okhttp4
```

Hasilnya antara lain `AuthApi.login(...)`, `FilesApi.uploadSimple(filename, body: File, ...)`, dan
`UploadsApi.uploadChunk(uploadId, chunkIndex, body: File, ...)`. Fungsi client ini tetap perlu
dilengkapi refresh token otomatis (bagian 2) dan logika resume (bagian 4).

Semua contoh memakai `curl` dan variabel berikut:

```bash
BASE=http://192.168.1.10:8000/api/v1
TOKEN=<access_token dari login>
```

---

## 1. Aturan umum

| Hal | Aturan |
|---|---|
| Prefix | Semua endpoint di bawah `/api/v1/` |
| Format | JSON (UTF-8), kecuali upload & download yang memakai body mentah |
| Autentikasi | Header `Authorization: Bearer <access_token>`; hanya `auth/register`, `auth/login`, `auth/refresh`, dan `system/health` yang publik |
| Waktu | UTC, ISO 8601, contoh `2026-11-12T10:44:48.933345Z` |
| Ukuran | Selalu dalam byte |
| ID | File, folder, upload, dan sesi berupa UUID (string); user dan job backup berupa angka |
| Batas body JSON | 1 MiB (`MAX_REQUEST_BODY_BYTES`); lebih besar dari itu dijawab `413 request_too_large` |
| Hak akses | Setiap pengguna hanya melihat datanya sendiri. Data milik orang lain selalu dijawab `404`, bukan `403`, agar keberadaannya tidak bocor. |

### Pagination

Endpoint daftar menerima `page` (mulai 1) dan `page_size` (maksimal 200, atau 100 untuk endpoint
admin), lalu mengembalikan:

```json
{"items": [...], "total": 137, "page": 1, "page_size": 50}
```

### Format error

Semua error memakai bentuk yang sama:

```json
{"detail": "File tidak ditemukan", "code": "file_not_found"}
```

Gunakan **`code`** untuk logika aplikasi. Isi `detail` bisa berubah dan hanya untuk ditampilkan
atau dicatat. Error validasi input (`422 validation_error`) juga menyertakan daftar field yang salah:

```json
{"detail": "Input tidak valid", "code": "validation_error",
 "errors": [{"loc": ["body", "password"], "msg": "String should have at least 8 characters", "type": "string_too_short"}]}
```

Nilai yang dikirim pengguna (misalnya password) sengaja tidak pernah ikut dikembalikan di pesan error.

---

## 2. Autentikasi

### Login

```bash
curl -X POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"budi","password":"Rahasia123","device_name":"Pixel 7"}'
```

```json
{
  "access_token": "eyJhbGciOi...",
  "refresh_token": "Q2x8...",
  "token_type": "bearer",
  "expires_in": 900,
  "refresh_expires_in": 2592000,
  "session_id": "25936b00-d334-4187-953c-ed8f52d5539d"
}
```

- `access_token` berlaku **15 menit** (`expires_in` dalam detik). Kirim di header `Authorization`.
- `refresh_token` berlaku **30 hari** sejak login. Simpan di penyimpanan terenkripsi
  (Android: `EncryptedSharedPreferences` atau DataStore + Keystore).
- `device_name` opsional; tampil di daftar sesi perangkat.
- Username tidak membedakan huruf besar/kecil.
- Username tidak terdaftar dan password salah sama-sama dijawab `401 invalid_credentials`.

**Pembatasan percobaan:** 5 kali gagal untuk satu username dalam 5 menit akan mengunci login
username itu selama 15 menit. Batas per IP adalah 20 kali. Saat terkunci, server menjawab
`429 too_many_attempts` dengan header `Retry-After` (detik). Tampilkan hitung mundurnya kepada pengguna.

### Memperbarui token (refresh token rotation)

```bash
curl -X POST $BASE/auth/refresh -H 'Content-Type: application/json' -d '{"refresh_token":"Q2x8..."}'
```

Respons berisi pasangan token **baru** dengan bentuk yang sama seperti login. **Refresh token
lama langsung tidak berlaku.** Bila refresh token lama dipakai lagi, server menganggap token itu
dicuri: seluruh sesi perangkat dicabut dan server menjawab `401 refresh_token_reused`.

Masa berlaku sesi tidak diperpanjang oleh refresh. Setelah 30 hari sejak login, pengguna harus login ulang.

> **Penting untuk aplikasi Android:** jangan pernah mengirim dua refresh bersamaan. Serialkan
> lewat satu `Mutex` (lihat contoh di bawah). Dua request yang menerima `401` di saat bersamaan
> harus menunggu satu refresh yang sama, bukan masing-masing melakukan refresh.

Contoh OkHttp `Authenticator` (Kotlin):

```kotlin
class TokenAuthenticator(private val store: TokenStore, private val api: AuthApi) : Authenticator {
    private val lock = Any()
    override fun authenticate(route: Route?, response: Response): Request? {
        val code = response.peekBody(2048).string().let { Regex("\"code\":\"(\\w+)\"").find(it)?.groupValues?.get(1) }
        if (code != "token_expired" && code != "token_invalid") return null          // bukan masalah access token
        synchronized(lock) {
            val sent = response.request.header("Authorization")?.removePrefix("Bearer ")
            if (sent != store.accessToken) {                                         // sudah di-refresh thread lain
                return response.request.newBuilder().header("Authorization", "Bearer ${store.accessToken}").build()
            }
            val fresh = api.refreshBlocking(store.refreshToken) ?: run { store.clear(); return null } // ke layar login
            store.save(fresh.accessToken, fresh.refreshToken)
            return response.request.newBuilder().header("Authorization", "Bearer ${fresh.accessToken}").build()
        }
    }
}
```

### Kode 401 dan tindakan klien

| `code` | Arti | Tindakan aplikasi |
|---|---|---|
| `not_authenticated` | Header Authorization tidak ada | Kirim token |
| `token_expired` | Access token kedaluwarsa | Refresh, lalu ulangi request |
| `token_invalid` | Token rusak/dipalsukan | Refresh; bila gagal, login ulang |
| `session_revoked` | Sesi dicabut (logout, ganti password, dicabut dari perangkat lain, admin) | Login ulang |
| `account_inactive` | Akun dinonaktifkan admin | Tampilkan pesan, login ulang |
| `refresh_token_invalid` / `refresh_token_expired` | Refresh token tidak dikenal / sudah 30 hari | Login ulang |
| `refresh_token_reused` | Refresh token lama dipakai lagi; sesi dicabut demi keamanan | Login ulang |
| `invalid_credentials` | Username/password salah (saat login) | Tampilkan pesan |

### Endpoint akun lainnya

| Method | Path | Body / keterangan |
|---|---|---|
| POST | `/auth/register` | `{username, password, email?}`. Hanya aktif bila `ALLOW_REGISTRATION=true`; bila tidak: `403 registration_disabled` |
| POST | `/auth/logout` | Akhiri sesi perangkat ini; access token & refresh token langsung tidak berlaku |
| POST | `/auth/logout-all` | Akhiri semua sesi di semua perangkat |
| GET | `/auth/me` | Profil: `id, username, email, role, is_active, storage_quota_bytes, created_at, last_login_at` |
| POST | `/auth/change-password` | `{current_password, new_password}`. Semua sesi dicabut; respons berisi **token baru** untuk perangkat ini |
| GET | `/auth/sessions` | Daftar sesi aktif: `id, device_name, ip_address, user_agent, created_at, last_used_at, expires_at, current` |
| DELETE | `/auth/sessions/{session_id}` | Cabut satu sesi (misalnya HP yang hilang) |

Aturan username: 3–32 karakter berupa huruf, angka, `.`, `_`, atau `-`. Aturan password: 8–128
karakter, tidak diawali atau diakhiri spasi, dan tidak sama dengan username.

---

## 3. File dan folder

### Objek file

```json
{
  "id": "ab4edb78-d219-4bd4-96ff-6dee29d756d1",
  "name": "liburan.jpg",
  "folder_id": null,
  "extension": "jpg",
  "mime_type": "image/jpeg",
  "media_type": "image",
  "size_bytes": 2483201,
  "sha256": "9f86d081884c7d65...",
  "width": 4032, "height": 3024, "duration_seconds": null,
  "has_thumbnail": true,
  "created_at": "2026-11-12T10:44:48Z", "updated_at": "2026-11-12T10:44:48Z"
}
```

- `folder_id: null` berarti file ada di root.
- `media_type` bernilai `image`, `video`, `audio`, `document`, atau `other`.
- `mime_type` ditentukan server dari **isi file**, bukan dari klien. File yang isinya jelas tidak
  cocok dengan ekstensinya (misalnya `foto.jpg` yang isinya PDF) ditolak `415 mime_mismatch`.
- Nama file hanya label. Di disk, file disimpan dengan nama UUID.

### Daftar dan pencarian

```bash
curl "$BASE/files?folder_id=<id>&sort=name&order=asc&page=1&page_size=50" -H "Authorization: Bearer $TOKEN"
curl "$BASE/files?q=liburan" -H "Authorization: Bearer $TOKEN"     # tanpa folder_id = cari di semua folder
```

| Parameter | Nilai |
|---|---|
| `folder_id` | Kosong = root (atau semua folder bila `q` diisi) |
| `q` | Cari di nama (tidak membedakan huruf besar/kecil) |
| `media_type` | `image`, `video`, `audio`, `document`, `other` |
| `sort` | `name`, `size`, `created_at` (default), `updated_at` |
| `order` | `asc` atau `desc` (default) |

### Upload file kecil (sekali kirim)

Untuk file hingga **100 MB** (`MAX_SIMPLE_UPLOAD_BYTES`). Body request adalah **isi file mentah**,
bukan multipart. Header `Content-Length` wajib.

```bash
curl -X POST "$BASE/files?filename=liburan.jpg&folder_id=<id-opsional>" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/octet-stream' \
  -H "X-Content-SHA256: $(sha256sum liburan.jpg | cut -d' ' -f1)" \
  --data-binary @liburan.jpg
```

Responsnya `201` berisi objek file.

- Bila nama sudah dipakai di folder tujuan, server memberi nomor otomatis, misalnya `liburan (1).jpg`.
- `X-Content-SHA256` opsional; bila dikirim dan isinya berbeda, server menjawab `422 checksum_mismatch`.
- Kotlin (OkHttp): `file.asRequestBody("application/octet-stream".toMediaType())`. OkHttp mengisi `Content-Length` sendiri.

File di atas 100 MB, atau upload yang perlu bisa dilanjutkan, memakai [upload bertahap](#4-upload-besar-chunked--resumable).

### Download

```bash
curl -OJ "$BASE/files/<id>/download" -H "Authorization: Bearer $TOKEN"
curl "$BASE/files/<id>/download" -H "Authorization: Bearer $TOKEN" -H 'Range: bytes=1048576-' -o sisa.part  # lanjutkan
```

- Header respons: `Content-Disposition: attachment; filename*=utf-8''...`, `X-Content-SHA256`,
  `Accept-Ranges: bytes`.
- Mendukung `Range`: respons `206 Partial Content` untuk melanjutkan unduhan yang terputus, dan
  `416` bila range di luar ukuran file.
- Setelah selesai, cocokkan SHA-256 file hasil unduhan dengan `X-Content-SHA256`.

### Rename, pindah, salin, hapus

| Method | Path | Body | Keterangan |
|---|---|---|---|
| GET | `/files/{id}` | | Detail file |
| PATCH | `/files/{id}/rename` | `{"name": "baru.jpg"}` | `409 name_conflict` bila nama sudah dipakai |
| POST | `/files/{id}/move` | `{"folder_id": "<id>" \| null}` | `null` = pindah ke root |
| POST | `/files/{id}/copy` | `{"folder_id"?: ..., "name"?: ...}` | `201`; tanpa `folder_id` = folder yang sama, tanpa `name` = nama otomatis |
| DELETE | `/files/{id}` | | Pindah ke **trash**; data tidak dihapus |

### Folder

| Method | Path | Body / parameter | Keterangan |
|---|---|---|---|
| POST | `/folders` | `{"name": "Liburan", "parent_id": null}` | `201` |
| GET | `/folders?parent_id=<id>` | | Subfolder (kosong = folder di root), urut nama |
| GET | `/folders/{id}` | | Detail + `path` (breadcrumb dari root), `folders`, `file_count`, `total_size_bytes` |
| PATCH | `/folders/{id}/rename` | `{"name": "..."}` | |
| POST | `/folders/{id}/move` | `{"parent_id": "<id>" \| null}` | `409 folder_cycle` bila dipindah ke dalam turunannya sendiri |
| DELETE | `/folders/{id}` | | Folder **beserta seluruh isinya** pindah ke trash |

**Aturan nama** (file dan folder):
- Panjang 1–255 byte. Tanpa `/`, `\`, atau karakter kontrol, dan bukan `.` atau `..`.
- File dan folder berbagi nama dalam satu folder, tanpa membedakan huruf besar/kecil.
- Kedalaman folder maksimal 32 tingkat.

---

## 4. Upload besar (chunked & resumable)

Dipakai untuk video dan file besar (hingga 64 GiB per file, `MAX_UPLOAD_SIZE_BYTES`). File dikirim
per bagian (chunk). Bila koneksi putus, cukup kirim chunk yang belum diterima.

### Alur

```
1. POST /uploads/init            -> dapat upload_id, chunk_size, total_chunks
2. PUT  /uploads/{id}/chunks/{i}  -> ulangi untuk i = 0 .. total_chunks-1
   (koneksi putus? GET /uploads/{id}/status -> kirim chunk yang belum ada di received_chunks)
3. POST /uploads/{id}/complete   -> server memverifikasi SHA-256 lalu menyimpan file
```

### 1) Mulai

```bash
curl -X POST $BASE/uploads/init -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"filename":"video.mp4","total_size":734003200,"sha256":"<sha256 seluruh file>","folder_id":null}'
```

```json
{
  "id": "0f6d...", "filename": "video.mp4", "folder_id": null,
  "total_size": 734003200, "chunk_size": 8388608, "total_chunks": 88,
  "status": "pending", "received_bytes": 0, "received_chunks": [], "missing_chunks": 88,
  "expected_sha256": "…", "file_id": null, "error_message": null,
  "created_at": "…", "expires_at": "…", "duplicate_file_id": null
}
```

| Field request | Keterangan |
|---|---|
| `filename`, `total_size` | Wajib. `total_size` ≥ 1 byte (file kosong: pakai upload sekali kirim) |
| `chunk_size` | Opsional; 256 KiB–64 MiB, default 8 MiB. **Selalu pakai nilai dari respons** |
| `sha256` | Opsional tetapi **sangat disarankan**: server menolak hasil akhir yang tidak cocok, dan memakainya untuk mendeteksi duplikat |
| `folder_id` | Opsional, folder tujuan |

- Bila Anda sudah punya file yang isinya identik, `duplicate_file_id` berisi ID file tersebut.
  Aplikasi boleh membatalkan upload (`cancel`) dan memakai file yang sudah ada.
- Kuota dan ruang SSD diperiksa di tahap ini (`507 quota_exceeded` / `507 insufficient_storage`),
  bukan setelah gigabyte data terkirim.

### 2) Kirim chunk

```bash
dd if=video.mp4 bs=8388608 skip=$i count=1 2>/dev/null > chunk.bin
curl -X PUT "$BASE/uploads/$UPLOAD_ID/chunks/$i" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/octet-stream' -H "X-Chunk-SHA256: $(sha256sum chunk.bin | cut -d' ' -f1)" \
  --data-binary @chunk.bin
```

```json
{"upload_id": "0f6d...", "chunk_index": 0, "size_bytes": 8388608, "sha256": "…",
 "received_bytes": 8388608, "received_chunks_count": 1, "total_chunks": 88, "all_received": false}
```

Aturan:
- Ukuran tiap chunk **harus tepat** `chunk_size`, kecuali chunk terakhir yang berisi sisa:
  `total_size - chunk_size × (total_chunks - 1)`.
- Offset chunk `i` = `i × chunk_size`.
- Mengirim ulang chunk yang sama aman (idempoten). Chunk boleh dikirim berurutan maupun paralel.
  **Pengiriman berurutan lebih cepat saat `complete`**, karena server bisa menghitung SHA-256 sambil
  menerima data. Bila tidak berurutan, server harus membaca ulang seluruh file dari disk, yang di
  Pi 3 memakan ±30 detik per GB.
- `X-Chunk-SHA256` opsional; bila tidak cocok, chunk ditolak (`422 chunk_checksum_mismatch`) dan
  bisa langsung dikirim ulang.
- `POST` juga diterima sebagai pengganti `PUT`.
- Sesi upload yang tidak menerima chunk selama **24 jam** (`UPLOAD_EXPIRE_HOURS`) kedaluwarsa dan
  datanya dihapus (`410 upload_expired`).
- Maksimal 10 upload aktif per pengguna (`429 too_many_uploads`).

### 3) Melanjutkan setelah koneksi putus

```bash
curl $BASE/uploads/$UPLOAD_ID/status -H "Authorization: Bearer $TOKEN"
curl $BASE/uploads -H "Authorization: Bearer $TOKEN"     # semua upload yang masih bisa dilanjutkan
```

Kirim hanya chunk yang **tidak** ada di `received_chunks`. Simpan `upload_id` di database lokal
aplikasi agar upload bisa dilanjutkan walaupun aplikasi ditutup.

### 4) Selesaikan

```bash
curl -X POST $BASE/uploads/$UPLOAD_ID/complete -H "Authorization: Bearer $TOKEN"
```

Respons berisi `{"upload": {...status "completed"...}, "file": {objek file}}`. Beri timeout yang
panjang untuk request ini (disarankan ≥ 120 detik untuk file sangat besar).

Batalkan dengan `POST /uploads/{id}/cancel`. Data sementara langsung dihapus.

### Kode error upload dan tindakan klien

| Status | `code` | Tindakan |
|---|---|---|
| 400 | `chunk_size_mismatch`, `chunk_incomplete`, `client_disconnected` | Kirim ulang chunk itu dengan ukuran yang benar |
| 413 | `chunk_too_large` | Periksa perhitungan ukuran chunk |
| 422 | `chunk_checksum_mismatch` | Data rusak di jaringan: kirim ulang chunk |
| 409 | `upload_incomplete` | Masih ada chunk yang belum diterima: cek `status`, kirim yang hilang |
| 409 | `upload_not_active` | Upload sudah selesai, dibatalkan, atau gagal: mulai upload baru |
| 409 | `upload_busy` | `complete` sedang berjalan: tunggu, lalu cek `status` |
| 409 | `upload_already_completed` | Sudah selesai; `file_id` ada di `status` |
| 410 | `upload_expired` | Mulai upload baru |
| 415 | `mime_mismatch`, `extension_not_allowed` | File ditolak; upload ditandai gagal |
| 422 | `checksum_mismatch` | SHA-256 akhir tidak cocok; upload gagal, mulai ulang |
| 422 | `upload_corrupted` | Data di disk server rusak; mulai ulang |
| 413 | `file_too_large` | Melebihi batas ukuran file |
| 422 | `invalid_chunk_size`, `too_many_chunks`, `invalid_chunk_index`, `invalid_checksum` | Perbaiki parameter |
| 429 | `too_many_uploads` | Selesaikan atau batalkan upload lain |
| 507 | `quota_exceeded`, `insufficient_storage` | Kuota pengguna atau SSD server penuh |

---

## 5. Media (galeri, streaming, thumbnail)

| Method | Path | Keterangan |
|---|---|---|
| GET | `/media/photos` | Semua foto dari semua folder (atau `folder_id`), terbaru dulu. Parameter `sort`, `order`, `page`, `page_size` sama seperti `/files` |
| GET | `/media/videos` | Semua video |
| GET | `/media/{id}` | Info media. Durasi & resolusi video dibaca saat pertama diminta. `415 not_media` untuk dokumen |
| GET | `/media/{id}/stream` | Isi file untuk diputar/ditampilkan langsung (`Content-Disposition: inline`), mendukung `Range` |
| GET | `/media/{id}/thumbnail` | JPEG, sisi terpanjang 320 px, dikirim dengan `ETag` + `Cache-Control: private, max-age=86400` |

**Thumbnail:**
- Dibuat saat pertama kali diminta, lalu disimpan. Permintaan pertama untuk video atau foto besar
  bisa butuh beberapa detik di Pi 3; permintaan berikutnya instan.
- Kirim `If-None-Match` dengan nilai `ETag` sebelumnya untuk mendapat `304` (tanpa body).
  Coil/Glide menangani ini otomatis lewat cache HTTP OkHttp.
- `404 thumbnail_unavailable` berarti tidak ada thumbnail (dokumen, file rusak, SVG, gambar terlalu
  besar, atau video tanpa ffmpeg di server). Tampilkan ikon jenis file.
- `503 thumbnail_busy` berarti server sedang membuat thumbnail lain; coba lagi setelah `Retry-After`.

**Streaming video dengan ExoPlayer / Media3:** pemutar video wajib mengirim header Authorization.

```kotlin
val dataSource = DefaultHttpDataSource.Factory()
    .setDefaultRequestProperties(mapOf("Authorization" to "Bearer $accessToken"))
val player = ExoPlayer.Builder(context)
    .setMediaSourceFactory(DefaultMediaSourceFactory(dataSource))
    .build()
player.setMediaItem(MediaItem.fromUri("$BASE/media/$fileId/stream"))
player.prepare()
```

ExoPlayer memakai `Range` untuk seek. Server membaca file per blok dari disk, jadi video sebesar
apa pun tidak dimuat utuh ke RAM. Access token berlaku 15 menit: untuk video panjang, buat ulang
`DataSource` dengan token baru setelah refresh, atau gunakan `OkHttpDataSource` dengan
`Authenticator` dari bagian 2.

---

## 6. Trash (recycle bin)

| Method | Path | Keterangan |
|---|---|---|
| GET | `/trash` | Isi trash: `id` (ID file/folder), `type` (`file`/`folder`), `name`, `original_folder_id`, `size_bytes`, `deleted_at`, `purge_after`; plus `retention_days` |
| POST | `/trash/{id}/restore` | Kembalikan ke folder asal, atau ke root bila folder asal sudah tidak ada. Nama yang bentrok diberi nomor. Respons: `{type, id, name, folder_id}` |
| POST | `/trash/{id}/delete` | **Hapus permanen** beserta data fisik dan thumbnail (`DELETE` juga diterima). Tidak bisa dibatalkan |
| DELETE | `/trash` | Kosongkan trash (permanen) |

- `{id}` adalah ID file atau ID folder yang dihapus.
- Folder dipulihkan sebagai satu kesatuan beserta isinya. File yang sebelumnya dihapus sendiri-sendiri
  tetap berada di trash.
- Item dihapus permanen otomatis setelah `purge_after` (default 30 hari, `TRASH_RETENTION_DAYS`).
- File di trash tetap dihitung dalam kuota sampai dihapus permanen.

---

## 7. Penyimpanan & kuota

```bash
curl $BASE/storage/status -H "Authorization: Bearer $TOKEN"
```

```json
{
  "primary": {"available": true, "total_bytes": 960000000000, "free_bytes": 812000000000, "used_percent": 15.4},
  "backup":  {"available": false, "total_bytes": null, "free_bytes": null, "used_percent": null},
  "usage": {"file_count": 1204, "used_bytes": 48200000000, "trash_bytes": 1200000, "quota_bytes": null, "quota_free_bytes": null}
}
```

`quota_bytes: null` berarti tanpa batas kuota. `backup.available: false` berarti HDD backup tidak
terpasang; semua fitur lain tetap berjalan.

---

## 8. Riwayat aktivitas

```bash
curl "$BASE/activity?action=file.&since=2026-11-01T00:00:00Z&page_size=50" -H "Authorization: Bearer $TOKEN"
```

- `action` bisa berupa nama aksi persis (`file.upload`) atau awalan yang diakhiri titik (`file.`, `auth.`).
- Aksi yang dicatat: `auth.login`, `auth.login_failed`, `auth.logout`, `auth.logout_all`,
  `auth.password_change`, `auth.session_revoke`, `auth.register`, `file.upload`, `file.download`,
  `file.stream`, `file.rename`, `file.move`, `file.copy`, `file.delete`, `folder.create`,
  `folder.rename`, `folder.move`, `folder.delete`, `upload.init`, `upload.cancel`, `trash.restore`,
  `trash.purge`, `backup.*`, `admin.*`, `system.start`.
- Password, token, dan secret tidak pernah dicatat.

---

## 9. Endpoint administrator

Semua endpoint di bagian ini membutuhkan akun dengan `role: "admin"`. Pengguna biasa mendapat
`403 admin_required`.

### Pengguna

| Method | Path | Body / parameter | Keterangan |
|---|---|---|---|
| GET | `/admin/users` | `search`, `page`, `page_size` (≤100) | Cari di username/email |
| POST | `/admin/users` | `{username, password, email?, role: "user"\|"admin", storage_quota_bytes?}` | `201` |
| GET | `/admin/users/{id}` | | |
| PATCH | `/admin/users/{id}` | Field yang dikirim saja: `email`, `role`, `is_active`, `password`, `storage_quota_bytes` (null = tanpa batas) | Mengubah role, menonaktifkan, atau reset password akan mencabut semua sesi pengguna itu |
| DELETE | `/admin/users/{id}` | | `409 user_has_data` bila masih punya file/folder (nonaktifkan saja) |

Pengamanan: admin tidak bisa menurunkan, menonaktifkan, atau menghapus dirinya sendiri
(`cannot_demote_self`, `cannot_delete_self`), dan admin aktif terakhir tidak bisa dihapus (`last_admin`).

### Backup SSD → HDD

```bash
curl -X POST $BASE/storage/backup -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"verify_existing": false}'
curl $BASE/storage/backup/1 -H "Authorization: Bearer $TOKEN"
```

- `POST` menjawab `202` dengan job berstatus `pending`; backup berjalan di latar belakang.
- Pantau `GET /storage/backup/{job_id}`: `status` bergerak dari `running` ke `completed`, `partial`, atau `failed`.
  Ada juga `files_total`, `files_copied`, `files_skipped`, `files_failed`, `bytes_copied`, dan
  `report` (berisi `failures[]` dengan `file_id` + `reason`, serta `database_backup`).
- `GET /storage/backup` menampilkan riwayat job.
- `verify_existing: true` menghitung ulang SHA-256 salinan yang sudah ada di HDD (lebih lambat;
  memperbaiki salinan yang rusak).
- Kemungkinan error: `409 backup_running` (backup lain sedang berjalan) dan `503 backup_unavailable`
  (HDD tidak terpasang).

### Sistem & storage

| Method | Path | Keterangan |
|---|---|---|
| GET | `/storage/ssd`, `/storage/hdd` | `mount_point`, `storage_path`, `mounted`, `available`, `reason`, `filesystem`, `device`, kapasitas |
| GET | `/system/status` | `status`, `version`, uptime server & sistem, CPU (`percent`, `load_average`, `temperature_c`), RAM, `database`, `storage`, `media_tools` |
| GET | `/system/statistics` | Jumlah pengguna (total/aktif), file (jumlah, ukuran, per `media_type`), folder, trash, upload aktif, aktivitas 24 jam terakhir |
| GET | `/admin/activity` | Seluruh activity log; filter `user_id`, `action`, `since`, `until` |

### Health check (publik)

```bash
curl $BASE/system/health
# 200 {"status":"ok","database":"ok","primary_storage":"ok","backup_storage":"ok"}
# 503 {"status":"degraded","database":"primary_storage_unavailable","primary_storage":"unavailable",...}
```

Aplikasi bisa memakai endpoint ini untuk menampilkan pesan "Server sedang tidak bisa menyimpan file"
saat menerima `503`.

---

## 10. Daftar semua kode error

| Status | `code` | Arti |
|---|---|---|
| 400 | `wrong_password` | Password saat ini salah (ganti password) |
| 400 | `chunk_size_mismatch`, `chunk_incomplete`, `body_incomplete`, `client_disconnected` | Ukuran body tidak sesuai / koneksi terputus |
| 401 | `not_authenticated`, `token_expired`, `token_invalid`, `session_revoked`, `account_inactive`, `invalid_credentials`, `refresh_token_invalid`, `refresh_token_expired`, `refresh_token_reused` | Lihat [bagian 2](#kode-401-dan-tindakan-klien) |
| 403 | `admin_required` | Butuh akun admin |
| 403 | `registration_disabled` | Registrasi dimatikan |
| 404 | `file_not_found`, `folder_not_found`, `upload_not_found`, `trash_item_not_found`, `session_not_found`, `user_not_found`, `backup_job_not_found`, `not_found` | Tidak ada, atau milik pengguna lain |
| 404 | `file_data_missing` | Catatan file ada tetapi data fisiknya hilang di SSD (hubungi admin; bisa dipulihkan dari backup) |
| 404 | `thumbnail_unavailable` | Tidak ada thumbnail untuk file ini |
| 405 | `method_not_allowed` | Method HTTP salah |
| 409 | `name_conflict`, `folder_cycle` | Nama sudah dipakai / folder ke dalam dirinya sendiri |
| 409 | `username_taken`, `email_taken`, `user_exists` | Akun bentrok |
| 409 | `upload_incomplete`, `upload_not_active`, `upload_busy`, `upload_already_completed` | Lihat [bagian 4](#kode-error-upload-dan-tindakan-klien) |
| 409 | `backup_running` | Backup lain sedang berjalan |
| 409 | `cannot_demote_self`, `cannot_delete_self`, `last_admin`, `user_has_data` | Aturan pengamanan admin |
| 410 | `upload_expired` | Sesi upload kedaluwarsa |
| 411 | `length_required` | Header `Content-Length` wajib (upload sekali kirim) |
| 413 | `request_too_large` | Body JSON > 1 MiB |
| 413 | `file_too_large`, `chunk_too_large`, `body_too_large` | Melebihi batas ukuran |
| 415 | `mime_mismatch`, `extension_not_allowed`, `not_media` | Jenis file ditolak / bukan media |
| 422 | `validation_error` | Field input tidak valid (lihat `errors`) |
| 422 | `invalid_name`, `weak_password`, `same_password`, `folder_too_deep` | Aturan nama/password/kedalaman |
| 422 | `checksum_mismatch`, `chunk_checksum_mismatch`, `invalid_checksum`, `upload_corrupted` | Checksum tidak cocok / format checksum salah |
| 422 | `invalid_chunk_size`, `too_many_chunks`, `invalid_chunk_index` | Parameter upload salah |
| 429 | `too_many_attempts` | Terlalu banyak login gagal (header `Retry-After`) |
| 429 | `too_many_uploads` | Terlalu banyak upload aktif |
| 500 | `internal_error` | Kesalahan tak terduga (detail hanya ada di log server) |
| 503 | `primary_storage_unavailable`, `database_unavailable`, `database_error` | SSD/database tidak tersedia; coba lagi setelah `Retry-After` |
| 503 | `backup_unavailable`, `thumbnail_busy` | HDD tidak tersedia / thumbnail sedang dibuat |
| 507 | `quota_exceeded`, `insufficient_storage` | Kuota pengguna / ruang SSD tidak cukup |

---

## 11. Ringkasan semua endpoint

| Method | Path | Akses |
|---|---|---|
| POST | `/api/v1/auth/register` | publik (bila diaktifkan) |
| POST | `/api/v1/auth/login` | publik |
| POST | `/api/v1/auth/refresh` | publik |
| POST | `/api/v1/auth/logout` | user |
| POST | `/api/v1/auth/logout-all` | user |
| GET | `/api/v1/auth/me` | user |
| POST | `/api/v1/auth/change-password` | user |
| GET | `/api/v1/auth/sessions` | user |
| DELETE | `/api/v1/auth/sessions/{session_id}` | user |
| GET | `/api/v1/files` | user |
| POST | `/api/v1/files` | user |
| GET | `/api/v1/files/{file_id}` | user |
| DELETE | `/api/v1/files/{file_id}` | user |
| GET | `/api/v1/files/{file_id}/download` | user |
| PATCH | `/api/v1/files/{file_id}/rename` | user |
| POST | `/api/v1/files/{file_id}/move` | user |
| POST | `/api/v1/files/{file_id}/copy` | user |
| POST | `/api/v1/folders` | user |
| GET | `/api/v1/folders` | user |
| GET | `/api/v1/folders/{folder_id}` | user |
| DELETE | `/api/v1/folders/{folder_id}` | user |
| PATCH | `/api/v1/folders/{folder_id}/rename` | user |
| POST | `/api/v1/folders/{folder_id}/move` | user |
| POST | `/api/v1/uploads/init` | user |
| GET | `/api/v1/uploads` | user |
| PUT | `/api/v1/uploads/{upload_id}/chunks/{chunk_index}` | user |
| POST | `/api/v1/uploads/{upload_id}/chunks/{chunk_index}` | user |
| GET | `/api/v1/uploads/{upload_id}/status` | user |
| POST | `/api/v1/uploads/{upload_id}/complete` | user |
| POST | `/api/v1/uploads/{upload_id}/cancel` | user |
| GET | `/api/v1/media/photos` | user |
| GET | `/api/v1/media/videos` | user |
| GET | `/api/v1/media/{file_id}` | user |
| GET | `/api/v1/media/{file_id}/stream` | user |
| GET | `/api/v1/media/{file_id}/thumbnail` | user |
| GET | `/api/v1/storage/status` | user |
| GET | `/api/v1/storage/ssd` | admin |
| GET | `/api/v1/storage/hdd` | admin |
| POST | `/api/v1/storage/backup` | admin |
| GET | `/api/v1/storage/backup` | admin |
| GET | `/api/v1/storage/backup/{job_id}` | admin |
| GET | `/api/v1/trash` | user |
| DELETE | `/api/v1/trash` | user |
| POST | `/api/v1/trash/{file_id}/restore` | user |
| POST | `/api/v1/trash/{file_id}/delete` | user |
| DELETE | `/api/v1/trash/{file_id}/delete` | user |
| GET | `/api/v1/activity` | user |
| GET | `/api/v1/admin/activity` | admin |
| GET | `/api/v1/admin/users` | admin |
| POST | `/api/v1/admin/users` | admin |
| GET | `/api/v1/admin/users/{user_id}` | admin |
| PATCH | `/api/v1/admin/users/{user_id}` | admin |
| DELETE | `/api/v1/admin/users/{user_id}` | admin |
| GET | `/api/v1/system/health` | publik |
| GET | `/api/v1/system/status` | admin |
| GET | `/api/v1/system/statistics` | admin |

## 12. Batas default (bisa diubah admin server)

| Batas | Default | Kunci konfigurasi |
|---|---|---|
| Masa berlaku access token | 15 menit | `ACCESS_TOKEN_EXPIRE_MINUTES` |
| Masa berlaku sesi / refresh token | 30 hari | `REFRESH_TOKEN_EXPIRE_DAYS` |
| Login gagal sebelum dikunci | 5 per username / 5 menit, kunci 15 menit | `LOGIN_MAX_ATTEMPTS`, `LOGIN_WINDOW_SECONDS`, `LOGIN_LOCKOUT_SECONDS` |
| Body JSON | 1 MiB | `MAX_REQUEST_BODY_BYTES` |
| Upload sekali kirim | 100 MiB | `MAX_SIMPLE_UPLOAD_BYTES` |
| Ukuran file (chunked) | 64 GiB | `MAX_UPLOAD_SIZE_BYTES` |
| Ukuran chunk | 256 KiB – 64 MiB, default 8 MiB | `UPLOAD_CHUNK_SIZE_MIN/MAX/DEFAULT` |
| Sesi upload tanpa aktivitas | 24 jam | `UPLOAD_EXPIRE_HOURS` |
| Upload aktif per pengguna | 10 | `MAX_ACTIVE_UPLOADS_PER_USER` |
| Masa simpan trash | 30 hari | `TRASH_RETENTION_DAYS` |
| Ukuran thumbnail | 320 px | `THUMBNAIL_SIZE` |
