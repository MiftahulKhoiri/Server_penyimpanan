from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.core.dependencies import CurrentUser, DbSession, SettingsDep, StorageDep
from app.api.v1.files import FILE_SECURITY_HEADERS
from app.core.errors import AppError
from app.models import MediaType
from app.schemas.files import FileList, FileOut, SortField, SortOrder
from app.services import file_service, media_service
from app.services.activity_service import log_activity
from app.utils.request_info import client_info

router = APIRouter(
    prefix="/media",
    tags=["media"],
    responses={401: {"description": "Tidak terautentikasi"}, 404: {"description": "File tidak ditemukan"}},
)


def _list_media(db, user, media_type: str, folder_id, sort, order, page, page_size) -> FileList:  # type: ignore[no-untyped-def]
    if folder_id is not None:
        file_service.get_owned_folder(db, user.id, folder_id)
    rows, total = file_service.list_files(
        db, user.id, folder_id=folder_id, search_all=folder_id is None, q=None,
        media_type=media_type, sort=sort, order=order, page=page, page_size=page_size,
    )
    return FileList(items=[FileOut.from_model(r) for r in rows], total=total, page=page, page_size=page_size)


@router.get("/photos", response_model=FileList)
def list_photos(
    user: CurrentUser,
    db: DbSession,
    folder_id: Annotated[str | None, Query(max_length=36, description="Kosong = semua folder")] = None,
    sort: SortField = "created_at",
    order: SortOrder = "desc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> FileList:
    """Semua foto milik Anda (galeri), terbaru dulu secara default."""
    return _list_media(db, user, MediaType.IMAGE.value, folder_id, sort, order, page, page_size)


@router.get("/videos", response_model=FileList)
def list_videos(
    user: CurrentUser,
    db: DbSession,
    folder_id: Annotated[str | None, Query(max_length=36, description="Kosong = semua folder")] = None,
    sort: SortField = "created_at",
    order: SortOrder = "desc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> FileList:
    return _list_media(db, user, MediaType.VIDEO.value, folder_id, sort, order, page, page_size)


@router.get("/{file_id}", response_model=FileOut)
def media_info(file_id: str, user: CurrentUser, db: DbSession, settings: SettingsDep, storage: StorageDep) -> FileOut:
    """Info media; durasi & resolusi video dibaca dengan ffprobe saat pertama diminta."""
    row = file_service.get_owned_file(db, user.id, file_id)
    if row.media_type not in media_service.STREAMABLE_MEDIA:
        raise AppError(415, "not_media", "File ini bukan foto, video, atau audio")
    return FileOut.from_model(media_service.ensure_media_info(db, settings, storage, row))


@router.get(
    "/{file_id}/stream",
    response_class=FileResponse,
    responses={206: {"description": "Partial Content (Range)"}, 415: {"description": "Bukan media"}},
)
def stream_media(file_id: str, request: Request, user: CurrentUser, db: DbSession, storage: StorageDep) -> FileResponse:
    """Streaming video/audio/foto untuk diputar langsung (HTTP Range, seek didukung).

    File dibaca bertahap dari disk; tidak pernah dimuat utuh ke RAM.
    """
    row = file_service.get_owned_file(db, user.id, file_id)
    if row.media_type not in media_service.STREAMABLE_MEDIA:
        raise AppError(415, "not_media", "File ini bukan foto, video, atau audio")
    path = file_service.physical_path(storage, row)
    range_header = request.headers.get("range")
    if range_header is None or range_header.replace(" ", "").startswith("bytes=0-"):
        client = client_info(request)
        log_activity(
            db, "file.stream", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
            target_type="file", target_id=row.id, details={"name": row.name},
        )
        db.commit()
    return FileResponse(
        path,
        media_type=row.mime_type,
        filename=row.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-cache", **FILE_SECURITY_HEADERS},
    )


@router.get(
    "/{file_id}/thumbnail",
    response_class=FileResponse,
    responses={200: {"content": {"image/jpeg": {}}}, 404: {"description": "Thumbnail tidak tersedia"}},
)
async def thumbnail(
    file_id: str, request: Request, user: CurrentUser, db: DbSession, settings: SettingsDep, storage: StorageDep
) -> FileResponse:
    """Thumbnail JPEG (sisi terpanjang THUMBNAIL_SIZE px). Dibuat sekali lalu di-cache."""
    row = file_service.get_owned_file(db, user.id, file_id)
    etag = f'"{row.sha256[:32]}-{settings.THUMBNAIL_SIZE}"'
    if request.headers.get("if-none-match") == etag and row.thumbnail_path:
        return Response(status_code=304, headers={"ETag": etag})  # type: ignore[return-value]
    path = await run_in_threadpool(media_service.get_or_create_thumbnail, db, settings, storage, row)
    return FileResponse(
        path, media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400", "ETag": etag},
    )
