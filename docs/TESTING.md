# Pengujian Server_penyimpanan

## Cara menjalankan

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                     # 197 tes otomatis (±40 detik)
.venv/bin/python -m pytest --cov=app           # dengan laporan coverage
tests/deploy/run_e2e.sh                        # uji deployment end-to-end (Docker, ±8 menit)
python tests/load/load_test.py --base http://127.0.0.1:8000 --password <admin> --pid <pid>
```

| Jenis | Lokasi | Isi |
|---|---|---|
| Unit & integrasi | `tests/test_*.py` | Semua endpoint lewat HTTP (TestClient) dengan database & folder sementara sungguhan |
| Deployment | `tests/deploy/run_e2e.sh` | Debian Bookworm + systemd + Python 3.11 di Docker: instalasi, reboot, SSD/HDD dicabut & dipasang, backup/restore, update, hardening |
| Beban | `tests/load/load_test.py` | Banyak perangkat paralel: upload chunk acak & paralel, download, thumbnail, refresh token, sambil backup berjalan |

Tes otomatis tidak memakai data dummy sebagai hasil: setiap tes membuat database SQLite
baru, menulis file sungguhan ke direktori sementara, dan memeriksa isi byte-per-byte /
SHA-256 hasil download.

## Pemetaan tes wajib (spesifikasi bagian 11)

| Tes wajib | Tes yang membuktikan |
|---|---|
| Registrasi | `test_auth.py::test_register_success_and_password_hashed`, `test_register_disabled_by_default`, `test_register_duplicate_username`, `test_register_validation_does_not_echo_password` |
| Login | `test_auth.py::test_login_success`, `test_login_wrong_password_and_unknown_user_same_response`, `test_login_rate_limited`, `test_login_inactive_user_rejected` |
| Token kedaluwarsa | `test_auth.py::test_expired_access_token`, `test_refresh_expired`, `test_tampered_and_wrong_key_tokens_rejected`, `test_refresh_reuse_revokes_whole_session` |
| Hak akses pengguna | `test_files.py::test_other_user_cannot_access_file`, `test_folders.py::test_other_users_folders_invisible`, `test_uploads.py::test_other_user_cannot_touch_upload`, `test_trash.py::test_trash_isolated_between_users`, `test_admin.py::test_admin_endpoints_require_admin` |
| Upload file kecil | `test_files.py::test_upload_small_photo`, `test_upload_document_and_unknown_types`, `test_upload_checksum_header` |
| Upload file besar | `test_uploads.py::test_large_file_upload` (24 MB, 25 chunk); uji nyata 1 GB di server (laporan TAHAP 6) |
| Upload chunk | `test_uploads.py::test_chunked_upload_basic`, `test_chunk_size_validation`, `test_chunk_streamed_without_content_length_too_large`, `test_upload_races.py` (chunk paralel) |
| Resume upload | `test_uploads.py::test_resume_after_interruption`, `test_server_restart_mid_upload`, `test_resend_same_chunk_is_idempotent` |
| Checksum | `test_uploads.py::test_chunk_checksum_mismatch_not_recorded`, `test_final_checksum_mismatch_fails_upload`, `test_corruption_on_disk_detected`, `test_backup.py::test_source_corruption_detected` |
| Download | `test_files.py::test_download_full`, `test_download_unicode_filename`, `test_download_missing_physical_file` |
| Streaming video | `test_media.py::test_stream_video_with_range` (video H.264 asli dibuat ffmpeg), `test_video_thumbnail`, `test_video_info_probed_once` |
| Range Request | `test_files.py::test_download_range_request` (206, suffix, 416), `test_media.py::test_stream_video_with_range` (seek, multi-range) |
| Rename | `test_files.py::test_rename`, `test_folders.py::test_rename_folder` |
| Move | `test_files.py::test_move_between_folders`, `test_folders.py::test_move_folder_and_cycle_prevention` |
| Delete | `test_files.py::test_soft_delete_keeps_physical_file`, `test_folders.py::test_delete_folder_recursive_to_trash`, `test_trash.py::test_purge_file_removes_data_and_thumbnail` |
| Restore | `test_trash.py::test_restore_file_to_original_folder`, `test_restore_folder_as_unit`, `test_restore_name_conflict_and_missing_folder`, `test_deploy_scripts.py::test_restore_missing_and_corrupt_files` |
| Kapasitas storage | `test_files.py::test_quota_exceeded`, `test_disk_full`, `test_uploads.py::test_init_quota_counts_active_uploads`, `test_storage_system.py::test_storage_status_for_user` |
| SSD tidak tersedia | `test_infrastructure.py::test_ssd_not_mounted_server_degraded_and_no_db_written`, `test_files.py::test_ssd_unplugged_while_running`, e2e bagian 7 & 8 |
| HDD tidak tersedia | `test_storage_system.py::test_hdd_not_mounted_server_keeps_running`, `test_backup.py::test_backup_requires_admin_and_hdd`, e2e bagian 6 |
| Path traversal | `test_files.py::test_path_traversal_names_rejected`, `test_tampered_storage_path_cannot_escape`, `test_file_not_found` (ID berisi `../`) |
| File tidak ditemukan | `test_files.py::test_file_not_found`, `test_download_missing_physical_file`, `test_backup.py::test_backup_job_not_found` |
| Database error | `test_infrastructure.py::test_database_error_returns_503_without_details`, `test_failure_paths.py::test_corrupt_database_file_gives_503`, `test_database_init_failure_reported` |

## Temuan yang ditemukan lewat pengujian dan sudah diperbaiki

| Temuan | Ditemukan oleh | Tes regresi |
|---|---|---|
| Batas piksel thumbnail bisa dilewati lewat ffmpeg | `test_media.py` | `test_thumbnail_pixel_limit_and_disabled` |
| Unit systemd diuji tanpa propagasi mount yang realistis | e2e | e2e bagian 8 |
| Backup menahan kunci tulis SQLite selama menyalin file (login/upload lain gagal "database is locked") | audit kode | `test_hardening.py::test_backup_does_not_hold_write_lock_while_copying` |
| Body JSON tanpa batas ukuran di endpoint publik (DoS RAM) | audit kode | `test_hardening.py::test_huge_json_body_*` |
| File HTML/SVG pengguna bisa dieksekusi di origin server | audit kode | `test_hardening.py::test_user_files_served_with_sandbox_csp` |
| Chunk terlambat menghidupkan lagi upload yang gagal/dibatalkan, dan bisa menulis setelah checksum dihitung | uji beban | `test_upload_races.py` |
| File biner acak ditolak karena kebetulan mirip tanda tangan MP3 (2 byte) | uji beban | `test_upload_races.py::test_weak_signatures_*` |
| RSS membengkak karena fragmentasi malloc antar-thread (321 MB) | uji beban | `MALLOC_ARENA_MAX=2` + `THREADPOOL_SIZE` (243 MB puncak) |
| Pencarian admin tidak meng-escape `%`/`_` | coverage | `test_failure_paths.py::test_admin_search_escapes_wildcards` |
| `operationId` duplikat & acak pada endpoint dua method (generator client bingung) | openapi-generator | `test_docs.py::test_operation_ids_unique_and_stable` |
| Endpoint upload tidak mendeklarasikan body biner: client Kotlin `uploadChunk()` tanpa parameter data | openapi-generator | `test_docs.py::test_streaming_uploads_declare_binary_body` |

## Sinkronisasi dokumentasi

`tests/test_docs.py` gagal bila dokumentasi tertinggal dari kode:
- setiap endpoint di aplikasi tercantum di `docs/API.md` (bagian 11), dan sebaliknya;
- setiap kode error yang dipakai kode ada di tabel `docs/API.md` (bagian 10);
- angka batas default di dokumen sama dengan `Settings`;
- bentuk contoh respons JSON di dokumen sama dengan respons asli;
- kolom "Akses" (admin/user) sesuai perilaku server;
- `docs/openapi.json` sama dengan hasil `scripts/export_openapi.py`.
