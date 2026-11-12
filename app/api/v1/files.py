from __future__ import annotations

import hashlib
import os
import re
from typing import Annotated

import anyio
from fastapi import APIRouter, Header, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.requests import ClientDisconnect

from app.core.dependencies import CurrentUser, DbSession, SettingsDep, StorageDep
from app.core.errors import AppError, unprocessable
from app.schemas.files import (
    CopyFileRequest,
    FileList,
    FileOut,
    MediaFilter,
    MoveFileRequest,
    RenameRequest,
    SortField,
    SortOrder,
)
from app.schemas.user import MessageResponse
from app.services import file_service
from app.services.activity_service import log_activity
from app.utils.filenames import validate_name
from app.utils.request_info import client_info

router = APIRouter(
    prefix="/files",
    tags=["files"],
    responses={401: {"description": "Tidak terautentikasi"}, 404: {"description": "File tidak ditemukan"}},
)

_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# File milik pengguna bisa berupa HTML/SVG: jangan pernah dieksekusi sebagai halaman di origin server.
FILE_SECURITY_HEADERS = {"Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; media-src 'self'"}


def write_all(fd: int, data: bytes, offset: int) -> None:
    view = memoryview(data)
    while view:
        written = os.pwrite(fd, view, offset)
        view = view[written:]
        offset += written


@router.get("", response_model=FileList)
def list_files(
    user: CurrentUser,
    db: DbSession,
    folder_id: Annotated[str | None, Query(max_length=36, description="Kosong = root")] = None,
    q: Annotated[str | None, Query(max_length=255, description="Cari nama; tanpa folder_id = cari di semua folder")] = None,
    media_type: MediaFilter | None = None,
    sort: SortField = "created_at",
    order: SortOrder = "desc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> FileList:
    if folder_id is not None:
        file_service.get_owned_folder(db, user.id, folder_id)
    rows, total = file_service.list_files(
        db, user.id,
        folder_id=folder_id, search_all=bool(q) and folder_id is None,
        q=q, media_type=media_type, sort=sort, order=order, page=page, page_size=page_size,
    )
    return FileList(items=[FileOut.from_model(r) for r in rows], total=total, page=page, page_size=page_size)


@router.post(
    "",
    response_model=FileOut,
    status_code=status.HTTP_201_CREATED,
    responses={411: {"description": "Content-Length wajib"}, 413: {"description": "Terlalu besar"},
               415: {"description": "Jenis file ditolak"}, 507: {"description": "Ruang tidak cukup"}},
)
async def upload_simple(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    storage: StorageDep,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    folder_id: Annotated[str | None, Query(max_length=36)] = None,
    x_content_sha256: Annotated[str | None, Header(alias="X-Content-SHA256")] = None,
) -> FileOut:
    """Upload sekali kirim untuk file kecil (≤ MAX_SIMPLE_UPLOAD_BYTES).

    Body request = isi file mentah (`application/octet-stream`), di-stream langsung ke SSD.
    Untuk file besar gunakan `/api/v1/uploads/*` (chunked & resumable).
    """
    try:
        name = validate_name(filename)
    except ValueError as exc:
        raise unprocessable("invalid_name", str(exc)) from exc
    if x_content_sha256 is not None and not _SHA_RE.fullmatch(x_content_sha256):
        raise unprocessable("invalid_checksum", "X-Content-SHA256 harus 64 karakter hex")
    raw_length = request.headers.get("content-length")
    if raw_length is None or not raw_length.isdigit():
        raise AppError(411, "length_required", "Header Content-Length wajib diisi")
    length = int(raw_length)
    if length > settings.MAX_SIMPLE_UPLOAD_BYTES:
        raise AppError(413, "file_too_large", "File terlalu besar untuk upload sekali kirim; gunakan upload chunked")

    file_service.ensure_extension_allowed(settings, name)
    target = await run_in_threadpool(file_service.resolve_folder_id, db, user.id, folder_id)
    await run_in_threadpool(file_service.ensure_capacity, db, settings, storage, user, length)

    temp = storage.new_temp_file("simple")
    hasher = hashlib.sha256()
    received = 0
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            async for piece in request.stream():
                if not piece:
                    continue
                if received + len(piece) > length:
                    raise AppError(413, "body_too_large", "Isi request melebihi Content-Length")
                write_all(fd, piece, received)
                hasher.update(piece)
                received += len(piece)
        except ClientDisconnect as exc:
            raise AppError(400, "client_disconnected", "Koneksi terputus saat upload") from exc
        if received != length:
            raise AppError(400, "body_incomplete", "Isi request lebih pendek dari Content-Length")
        await anyio.to_thread.run_sync(os.fsync, fd)
    except BaseException:
        os.close(fd)
        temp.unlink(missing_ok=True)
        raise
    os.close(fd)

    digest = hasher.hexdigest()
    if x_content_sha256 and digest != x_content_sha256.lower():
        temp.unlink(missing_ok=True)
        raise AppError(422, "checksum_mismatch", "SHA-256 file tidak cocok")
    row = await run_in_threadpool(
        file_service.finalize_new_file, db, settings, storage, user,
        folder_id=target, name=name, temp_path=temp, size=received, sha256=digest,
        client=client_info(request), details={"method": "simple"},
    )
    return FileOut.from_model(row)


@router.get("/{file_id}", response_model=FileOut)
def get_file(file_id: str, user: CurrentUser, db: DbSession) -> FileOut:
    return FileOut.from_model(file_service.get_owned_file(db, user.id, file_id))


@router.get(
    "/{file_id}/download",
    response_class=FileResponse,
    responses={200: {"content": {"application/octet-stream": {}}}, 206: {"description": "Partial Content (Range)"}},
)
def download_file(file_id: str, request: Request, user: CurrentUser, db: DbSession, storage: StorageDep) -> FileResponse:
    """Unduh file (streaming dari disk, mendukung header Range untuk melanjutkan unduhan)."""
    row = file_service.get_owned_file(db, user.id, file_id)
    path = file_service.physical_path(storage, row)
    range_header = request.headers.get("range")
    # Catat sekali per unduhan, bukan tiap potongan Range lanjutan.
    if range_header is None or range_header.replace(" ", "").startswith("bytes=0-"):
        client = client_info(request)
        log_activity(
            db, "file.download", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
            target_type="file", target_id=row.id, details={"name": row.name},
        )
        db.commit()
    return FileResponse(
        path,
        media_type=row.mime_type,
        filename=row.name,
        content_disposition_type="attachment",
        headers={"X-Content-SHA256": row.sha256, "Cache-Control": "private, no-cache", **FILE_SECURITY_HEADERS},
    )


@router.patch("/{file_id}/rename", response_model=FileOut, responses={409: {"description": "Nama bentrok"}})
def rename_file(
    file_id: str, body: RenameRequest, request: Request, user: CurrentUser, db: DbSession, settings: SettingsDep
) -> FileOut:
    row = file_service.get_owned_file(db, user.id, file_id)
    return FileOut.from_model(file_service.rename_file(db, settings, user, row, body.name, client_info(request)))


@router.post("/{file_id}/move", response_model=FileOut, responses={409: {"description": "Nama bentrok"}})
def move_file(file_id: str, body: MoveFileRequest, request: Request, user: CurrentUser, db: DbSession) -> FileOut:
    row = file_service.get_owned_file(db, user.id, file_id)
    return FileOut.from_model(file_service.move_file(db, user, row, body.folder_id, client_info(request)))


@router.post(
    "/{file_id}/copy",
    response_model=FileOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "Nama bentrok"}, 507: {"description": "Ruang tidak cukup"}},
)
def copy_file(
    file_id: str,
    body: CopyFileRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    storage: StorageDep,
) -> FileOut:
    row = file_service.get_owned_file(db, user.id, file_id)
    folder_id = body.folder_id if "folder_id" in body.model_fields_set else row.folder_id
    copy = file_service.copy_file(
        db, settings, storage, user, row, folder_id=folder_id, name=body.name, client=client_info(request)
    )
    return FileOut.from_model(copy)


@router.delete("/{file_id}", response_model=MessageResponse)
def delete_file(file_id: str, request: Request, user: CurrentUser, db: DbSession, settings: SettingsDep) -> MessageResponse:
    """Pindahkan file ke trash (soft delete). Hapus permanen lewat /api/v1/trash."""
    row = file_service.get_owned_file(db, user.id, file_id)
    file_service.soft_delete_file(db, settings, user, row, client_info(request))
    return MessageResponse(detail="File dipindahkan ke trash")
