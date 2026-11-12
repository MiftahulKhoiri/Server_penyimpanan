"""Metadata OpenAPI: deskripsi umum, grup endpoint, dan skema error standar."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

DESCRIPTION = """
Backend **Personal Cloud Storage** untuk Raspberry Pi: simpan, kelola, unggah, dan unduh foto,
video, serta dokumen dari aplikasi Android.

**Autentikasi:** login di `POST /api/v1/auth/login`, lalu kirim header
`Authorization: Bearer <access_token>` di setiap request. Access token berlaku 15 menit;
perbarui dengan `POST /api/v1/auth/refresh` (refresh token **berganti** setiap kali dipakai).
Di halaman ini klik **Authorize** lalu tempel `access_token`.

**Format error** selalu `{"detail": "<pesan untuk manusia>", "code": "<kode untuk program>"}`.
Gunakan `code` untuk logika aplikasi; `detail` boleh berubah.

**Waktu** dalam UTC (ISO 8601), **ukuran** dalam byte, **ID** file/folder/upload berupa UUID.

Panduan lengkap untuk developer aplikasi: `docs/API.md` di repository.
"""

TAGS: list[dict[str, Any]] = [
    {"name": "auth", "description": "Login, refresh token (rotasi), logout, sesi perangkat, ganti password."},
    {"name": "files", "description": "Daftar/cari file, upload kecil, unduh (Range), rename, pindah, salin, hapus ke trash."},
    {"name": "folders", "description": "Folder bertingkat: buat, daftar, detail + breadcrumb, rename, pindah, hapus ke trash."},
    {"name": "uploads", "description": "Upload file besar bertahap (chunked) yang bisa dilanjutkan setelah koneksi putus."},
    {"name": "media", "description": "Galeri foto & video, info media, streaming dengan HTTP Range, thumbnail."},
    {"name": "trash", "description": "Recycle bin: daftar, kembalikan, hapus permanen, kosongkan."},
    {"name": "storage", "description": "Kapasitas & kuota; detail SSD/HDD dan backup (admin)."},
    {"name": "activity", "description": "Riwayat aktivitas akun."},
    {"name": "admin", "description": "Khusus administrator: kelola pengguna & seluruh activity log."},
    {"name": "system", "description": "Health check (publik), status server & statistik (admin)."},
]


class ErrorOut(BaseModel):
    detail: str = Field(description="Pesan untuk ditampilkan/dicatat")
    code: str = Field(description="Kode mesin, mis. `token_expired`, `file_not_found`")


class ValidationErrorItem(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ValidationErrorOut(ErrorOut):
    errors: list[ValidationErrorItem]


def binary_body(description: str) -> dict[str, Any]:
    """Deklarasi body biner mentah untuk endpoint yang membaca request.stream() langsung.

    Tanpa ini OpenAPI tidak memuat requestBody, dan generator client (Kotlin dsb.) membuat
    fungsi upload tanpa parameter data.
    """
    return {
        "requestBody": {
            "required": True,
            "description": description,
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
    }


COMMON_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorOut, "description": "Belum login / token kedaluwarsa / sesi dicabut"},
    413: {"model": ErrorOut, "description": "Body request terlalu besar"},
    422: {"model": ValidationErrorOut, "description": "Input tidak valid"},
    503: {"model": ErrorOut, "description": "SSD atau database tidak tersedia (coba lagi; lihat header Retry-After)"},
}
