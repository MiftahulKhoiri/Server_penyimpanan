from __future__ import annotations

import hashlib
import os
import re
from typing import Annotated

import anyio
from fastapi import APIRouter, Header, Path, Request, status
from sqlalchemy import select
from starlette.requests import ClientDisconnect

from app.api.v1.files import write_all
from app.core.dependencies import CurrentUser, DbSession, SettingsDep, StorageDep
from app.core.errors import AppError, unprocessable
from app.models import Upload, UploadStatus
from app.schemas.files import ChunkResult, FileOut, UploadCompleteOut, UploadInitRequest, UploadOut
from app.services import upload_service
from app.services.upload_service import UploadHashRegistry
from app.utils.mime import SNIFF_BYTES, MimeMismatch, decide_mime
from app.utils.request_info import client_info

router = APIRouter(
    prefix="/uploads",
    tags=["uploads"],
    responses={401: {"description": "Tidak terautentikasi"}, 404: {"description": "Upload tidak ditemukan"}},
)

_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _registry(request: Request) -> UploadHashRegistry:
    return request.app.state.upload_hashes


@router.post(
    "/init",
    response_model=UploadOut,
    status_code=status.HTTP_201_CREATED,
    responses={413: {"description": "Terlalu besar"}, 415: {"description": "Ekstensi ditolak"},
               429: {"description": "Terlalu banyak upload aktif"}, 507: {"description": "Ruang tidak cukup"}},
)
def init_upload(
    body: UploadInitRequest, request: Request, user: CurrentUser, db: DbSession,
    settings: SettingsDep, storage: StorageDep,
) -> UploadOut:
    """Mulai sesi upload chunked. Respons berisi `chunk_size` & `total_chunks` yang harus dipakai klien."""
    upload, duplicate = upload_service.init_upload(db, settings, storage, user, body, client_info(request))
    return upload_service.to_out(db, upload, duplicate)


@router.get("", response_model=list[UploadOut])
def list_active_uploads(user: CurrentUser, db: DbSession) -> list[UploadOut]:
    """Upload milik Anda yang masih bisa dilanjutkan (berguna setelah aplikasi ditutup)."""
    rows = db.scalars(
        select(Upload)
        .where(Upload.owner_id == user.id, Upload.status.in_(upload_service.WRITABLE))
        .order_by(Upload.created_at.desc())
    ).all()
    return [upload_service.to_out(db, row) for row in rows]


@router.api_route(
    "/{upload_id}/chunks/{chunk_index}",
    methods=["PUT", "POST"],
    response_model=ChunkResult,
    responses={400: {"description": "Ukuran chunk salah"}, 409: {"description": "Upload tidak aktif"},
               410: {"description": "Kedaluwarsa"}, 422: {"description": "Checksum chunk salah"}},
)
async def upload_chunk(
    upload_id: str,
    chunk_index: Annotated[int, Path(ge=0)],
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    storage: StorageDep,
    x_chunk_sha256: Annotated[str | None, Header(alias="X-Chunk-SHA256")] = None,
) -> ChunkResult:
    """Kirim satu chunk sebagai body mentah. Chunk yang sama boleh dikirim ulang (idempoten).

    Ukuran wajib tepat `chunk_size` (chunk terakhir = sisa). Header `X-Chunk-SHA256` opsional
    tetapi disarankan agar kerusakan di jaringan terdeteksi.
    """
    upload = upload_service.get_owned_upload(db, user.id, upload_id)
    upload_service.ensure_writable(upload)
    if chunk_index >= upload.total_chunks:
        raise unprocessable("invalid_chunk_index", f"chunk_index harus 0-{upload.total_chunks - 1}")
    if x_chunk_sha256 is not None and not _SHA_RE.fullmatch(x_chunk_sha256):
        raise unprocessable("invalid_checksum", "X-Chunk-SHA256 harus 64 karakter hex")
    expected = upload_service.expected_chunk_size(upload, chunk_index)
    raw_length = request.headers.get("content-length")
    if raw_length is not None and (not raw_length.isdigit() or int(raw_length) != expected):
        raise AppError(400, "chunk_size_mismatch", f"Chunk {chunk_index} harus {expected} byte")

    registry = _registry(request)
    running = registry.begin_chunk(upload.id, chunk_index)
    chunk_hash = hashlib.sha256()
    head = b""
    offset = chunk_index * upload.chunk_size
    received = 0
    fd = os.open(storage.resolve(upload.temp_path), os.O_WRONLY)
    try:
        try:
            async for piece in request.stream():
                if not piece:
                    continue
                if received + len(piece) > expected:
                    raise AppError(413, "chunk_too_large", f"Chunk {chunk_index} melebihi {expected} byte")
                write_all(fd, piece, offset + received)
                chunk_hash.update(piece)
                if running is not None:
                    running.update(piece)
                if chunk_index == 0 and len(head) < SNIFF_BYTES:
                    head += piece[: SNIFF_BYTES - len(head)]
                received += len(piece)
        except ClientDisconnect as exc:
            raise AppError(400, "client_disconnected", "Koneksi terputus; kirim ulang chunk ini") from exc
        if received != expected:
            raise AppError(400, "chunk_incomplete", f"Chunk {chunk_index} harus {expected} byte, diterima {received}")
        await anyio.to_thread.run_sync(os.fdatasync, fd)
    finally:
        os.close(fd)

    digest = chunk_hash.hexdigest()
    if x_chunk_sha256 and digest != x_chunk_sha256.lower():
        raise AppError(422, "chunk_checksum_mismatch", f"SHA-256 chunk {chunk_index} tidak cocok; kirim ulang")
    if chunk_index == 0:
        # Tolak lebih awal bila isi tidak cocok dengan ekstensi, tanpa menunggu seluruh file terkirim.
        try:
            decide_mime(upload.filename, head)
        except MimeMismatch as exc:
            upload_service.mark_failed(db, storage, registry, upload, "mime_mismatch")
            raise AppError(415, "mime_mismatch", "Isi file tidak sesuai dengan ekstensinya") from exc

    same = upload_service.record_chunk(db, settings, upload, chunk_index, expected, digest)
    registry.commit_chunk(upload.id, chunk_index, running, same_as_before=same)
    count = len(upload_service.received_indices(db, upload.id))
    return ChunkResult(
        upload_id=upload.id,
        chunk_index=chunk_index,
        size_bytes=expected,
        sha256=digest,
        received_bytes=upload.received_bytes,
        received_chunks_count=count,
        total_chunks=upload.total_chunks,
        all_received=count == upload.total_chunks,
    )


@router.get("/{upload_id}/status", response_model=UploadOut)
def upload_status(upload_id: str, user: CurrentUser, db: DbSession) -> UploadOut:
    """Status & daftar chunk yang sudah diterima, untuk melanjutkan (resume) upload."""
    return upload_service.to_out(db, upload_service.get_owned_upload(db, user.id, upload_id))


@router.post(
    "/{upload_id}/complete",
    response_model=UploadCompleteOut,
    responses={409: {"description": "Chunk belum lengkap"}, 415: {"description": "Jenis file ditolak"},
               422: {"description": "Checksum tidak cocok"}},
)
def complete_upload(
    upload_id: str, request: Request, user: CurrentUser, db: DbSession,
    settings: SettingsDep, storage: StorageDep,
) -> UploadCompleteOut:
    upload = upload_service.get_owned_upload(db, user.id, upload_id)
    if upload.status == UploadStatus.COMPLETED.value and upload.file_id:
        raise AppError(409, "upload_already_completed", "Upload sudah selesai")
    stored = upload_service.complete_upload(
        db, settings, storage, _registry(request), user, upload, client_info(request)
    )
    upload = upload_service.get_owned_upload(db, user.id, upload_id)
    return UploadCompleteOut(upload=upload_service.to_out(db, upload), file=FileOut.from_model(stored))


@router.post("/{upload_id}/cancel", response_model=UploadOut)
def cancel_upload(upload_id: str, request: Request, user: CurrentUser, db: DbSession, storage: StorageDep) -> UploadOut:
    upload = upload_service.get_owned_upload(db, user.id, upload_id)
    upload = upload_service.cancel_upload(db, storage, _registry(request), user, upload, client_info(request))
    return upload_service.to_out(db, upload)
