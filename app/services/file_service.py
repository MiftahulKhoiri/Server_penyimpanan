"""Logika file: kepemilikan, nama unik, kapasitas, finalisasi, rename/move/copy/hapus."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import timedelta
from pathlib import Path
from shutil import disk_usage

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, conflict, not_found
from app.database.types import new_uuid, utcnow
from app.models import Folder, StoredFile, TrashItem, Upload, UploadStatus, User
from app.services.activity_service import log_activity
from app.services.storage_paths import DIR_MODE, StoragePaths, UnsafePathError, fsync_dir
from app.utils.filenames import numbered_name, split_extension
from app.utils.images import image_dimensions
from app.utils.mime import SNIFF_BYTES, MimeMismatch, decide_mime
from app.utils.request_info import ClientInfo

logger = logging.getLogger("server_penyimpanan.files")

ACTIVE_UPLOAD_STATUSES = (
    UploadStatus.PENDING.value,
    UploadStatus.UPLOADING.value,
    UploadStatus.PROCESSING.value,
)


# ---------------- Kepemilikan ----------------
def get_owned_file(db: Session, owner_id: int, file_id: str) -> StoredFile:
    row = db.get(StoredFile, file_id) if len(file_id) <= 36 else None
    if row is None or row.owner_id != owner_id or row.is_deleted:
        # Sama-sama 404 agar keberadaan file milik orang lain tidak bocor.
        raise not_found("file_not_found", "File tidak ditemukan")
    return row


def get_owned_folder(db: Session, owner_id: int, folder_id: str) -> Folder:
    row = db.get(Folder, folder_id) if len(folder_id) <= 36 else None
    if row is None or row.owner_id != owner_id or row.is_deleted:
        raise not_found("folder_not_found", "Folder tidak ditemukan")
    return row


def resolve_folder_id(db: Session, owner_id: int, folder_id: str | None) -> str | None:
    if folder_id is None:
        return None
    return get_owned_folder(db, owner_id, folder_id).id


# ---------------- Nama ----------------
def name_taken(
    db: Session,
    owner_id: int,
    folder_id: str | None,
    name: str,
    *,
    exclude_file_id: str | None = None,
    exclude_folder_id: str | None = None,
) -> bool:
    """File dan folder berbagi namespace dalam satu folder (case-insensitive)."""
    lowered = func.lower(name)
    file_q = select(StoredFile.id).where(
        StoredFile.owner_id == owner_id,
        StoredFile.is_deleted.is_(False),
        StoredFile.folder_id.is_(None) if folder_id is None else StoredFile.folder_id == folder_id,
        func.lower(StoredFile.name) == lowered,
    )
    if exclude_file_id:
        file_q = file_q.where(StoredFile.id != exclude_file_id)
    if db.scalar(file_q.limit(1)) is not None:
        return True
    folder_q = select(Folder.id).where(
        Folder.owner_id == owner_id,
        Folder.is_deleted.is_(False),
        Folder.parent_id.is_(None) if folder_id is None else Folder.parent_id == folder_id,
        func.lower(Folder.name) == lowered,
    )
    if exclude_folder_id:
        folder_q = folder_q.where(Folder.id != exclude_folder_id)
    return db.scalar(folder_q.limit(1)) is not None


def unique_name(db: Session, owner_id: int, folder_id: str | None, name: str) -> str:
    if not name_taken(db, owner_id, folder_id, name):
        return name
    for number in range(1, 10_000):
        candidate = numbered_name(name, number)
        if not name_taken(db, owner_id, folder_id, candidate):
            return candidate
    raise conflict("name_conflict", "Tidak bisa membuat nama unik")


def ensure_extension_allowed(settings: Settings, name: str) -> None:
    _, ext = split_extension(name)
    ext = ext or ""
    if ext in settings.BLOCKED_EXTENSIONS or (
        settings.ALLOWED_EXTENSIONS and ext not in settings.ALLOWED_EXTENSIONS
    ):
        raise AppError(415, "extension_not_allowed", "Jenis file (ekstensi) ini tidak diizinkan")


# ---------------- Kapasitas ----------------
def storage_used_bytes(db: Session, owner_id: int) -> int:
    """Termasuk file di trash karena masih memakai ruang disk."""
    return int(
        db.scalar(select(func.coalesce(func.sum(StoredFile.size_bytes), 0)).where(StoredFile.owner_id == owner_id))
        or 0
    )


def _active_uploads_bytes(db: Session, owner_id: int | None, *, remaining: bool) -> int:
    amount = Upload.total_size - Upload.received_bytes if remaining else Upload.total_size
    query = select(func.coalesce(func.sum(amount), 0)).where(Upload.status.in_(ACTIVE_UPLOAD_STATUSES))
    if owner_id is not None:
        query = query.where(Upload.owner_id == owner_id)
    return int(db.scalar(query) or 0)


def ensure_capacity(db: Session, settings: Settings, storage: StoragePaths, user: User, incoming: int) -> None:
    if user.storage_quota_bytes is not None:
        used = storage_used_bytes(db, user.id) + _active_uploads_bytes(db, user.id, remaining=False)
        if used + incoming > user.storage_quota_bytes:
            raise AppError(507, "quota_exceeded", "Kuota penyimpanan Anda tidak cukup")
    free = disk_usage(storage.root).free
    reserved = _active_uploads_bytes(db, None, remaining=True)
    if free - reserved - incoming < settings.MIN_FREE_SPACE_BYTES:
        raise AppError(507, "insufficient_storage", "Ruang penyimpanan server tidak cukup")


def physical_path(storage: StoragePaths, row: StoredFile) -> Path:
    try:
        path = storage.resolve(row.storage_path)
    except UnsafePathError:
        logger.error("storage_path tidak aman untuk file %s", row.id)
        raise not_found("file_data_missing", "Data file tidak tersedia") from None
    if not path.is_file():
        logger.error("Data fisik file %s hilang", row.id)
        raise not_found("file_data_missing", "Data file tidak tersedia")
    return path


# ---------------- Finalisasi file baru ----------------
def finalize_new_file(
    db: Session,
    settings: Settings,
    storage: StoragePaths,
    owner: User,
    *,
    folder_id: str | None,
    name: str,
    temp_path: Path,
    size: int,
    sha256: str,
    client: ClientInfo,
    details: dict | None = None,
) -> StoredFile:
    """Pindahkan file sementara ke lokasi final dan buat baris database.

    File sementara selalu dihapus bila terjadi kegagalan.
    """
    dest: Path | None = None
    try:
        ensure_extension_allowed(settings, name)
        with open(temp_path, "rb") as fh:
            head = fh.read(SNIFF_BYTES)
        try:
            decision = decide_mime(name, head)
        except MimeMismatch as exc:
            raise AppError(415, "mime_mismatch", "Isi file tidak sesuai dengan ekstensinya") from exc

        final_name = unique_name(db, owner.id, folder_id, name)
        file_id = new_uuid()
        relpath = storage.file_relpath(owner.id, file_id)
        dest = storage.resolve(relpath)
        dest.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        os.replace(temp_path, dest)
        fsync_dir(dest.parent)
        width, height = image_dimensions(dest, decision.mime_type)
        _, ext = split_extension(final_name)
        row = StoredFile(
            id=file_id,
            owner_id=owner.id,
            folder_id=folder_id,
            name=final_name,
            extension=ext,
            mime_type=decision.mime_type,
            media_type=decision.media_type,
            size_bytes=size,
            sha256=sha256,
            storage_path=relpath,
            width=width,
            height=height,
        )
        db.add(row)
        log_activity(
            db, "file.upload", user_id=owner.id, ip_address=client.ip_address, user_agent=client.user_agent,
            target_type="file", target_id=file_id,
            details={"name": final_name, "size_bytes": size, **(details or {})},
        )
        db.commit()
        return row
    except BaseException:
        db.rollback()
        if dest is not None:
            dest.unlink(missing_ok=True)
        temp_path.unlink(missing_ok=True)
        raise


# ---------------- Daftar & pencarian ----------------
def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_files(
    db: Session,
    owner_id: int,
    *,
    folder_id: str | None,
    search_all: bool,
    q: str | None,
    media_type: str | None,
    sort: str,
    order: str,
    page: int,
    page_size: int,
) -> tuple[list[StoredFile], int]:
    query = select(StoredFile).where(StoredFile.owner_id == owner_id, StoredFile.is_deleted.is_(False))
    if not search_all:
        query = query.where(
            StoredFile.folder_id.is_(None) if folder_id is None else StoredFile.folder_id == folder_id
        )
    if q:
        pattern = f"%{_escape_like(q)}%"
        query = query.where(func.lower(StoredFile.name).like(func.lower(pattern), escape="\\"))
    if media_type:
        query = query.where(StoredFile.media_type == media_type)

    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    column = {
        "name": func.lower(StoredFile.name),
        "size": StoredFile.size_bytes,
        "created_at": StoredFile.created_at,
        "updated_at": StoredFile.updated_at,
    }[sort]
    ordering = column.desc() if order == "desc" else column.asc()
    tiebreak = StoredFile.id.desc() if order == "desc" else StoredFile.id.asc()
    rows = db.scalars(query.order_by(ordering, tiebreak).offset((page - 1) * page_size).limit(page_size)).all()
    return list(rows), total


# ---------------- Operasi file ----------------
def rename_file(db: Session, settings: Settings, user: User, row: StoredFile, new_name: str, client: ClientInfo) -> StoredFile:
    if new_name == row.name:
        return row
    ensure_extension_allowed(settings, new_name)
    if name_taken(db, user.id, row.folder_id, new_name, exclude_file_id=row.id):
        raise conflict("name_conflict", "Sudah ada file/folder dengan nama itu di folder ini")
    old_name = row.name
    row.name = new_name
    row.extension = split_extension(new_name)[1]
    log_activity(
        db, "file.rename", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
        target_type="file", target_id=row.id, details={"old_name": old_name, "new_name": new_name},
    )
    db.commit()
    return row


def move_file(db: Session, user: User, row: StoredFile, folder_id: str | None, client: ClientInfo) -> StoredFile:
    target = resolve_folder_id(db, user.id, folder_id)
    if target == row.folder_id:
        return row
    if name_taken(db, user.id, target, row.name, exclude_file_id=row.id):
        raise conflict("name_conflict", "Folder tujuan sudah berisi file/folder dengan nama yang sama")
    old_folder = row.folder_id
    row.folder_id = target
    log_activity(
        db, "file.move", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
        target_type="file", target_id=row.id, details={"from_folder": old_folder, "to_folder": target},
    )
    db.commit()
    return row


def copy_file(
    db: Session,
    settings: Settings,
    storage: StoragePaths,
    user: User,
    row: StoredFile,
    *,
    folder_id: str | None,
    name: str | None,
    client: ClientInfo,
) -> StoredFile:
    target = resolve_folder_id(db, user.id, folder_id)
    if name is not None:
        ensure_extension_allowed(settings, name)
        if name_taken(db, user.id, target, name):
            raise conflict("name_conflict", "Sudah ada file/folder dengan nama itu di folder tujuan")
        final_name = name
    else:
        final_name = unique_name(db, user.id, target, row.name)
    ensure_capacity(db, settings, storage, user, row.size_bytes)

    src = physical_path(storage, row)
    temp = storage.new_temp_file("copy")
    new_id = new_uuid()
    relpath = storage.file_relpath(user.id, new_id)
    dest = storage.resolve(relpath)
    try:
        shutil.copyfile(src, temp)  # memakai copy_file_range/sendfile; tidak memuat file ke RAM
        with open(temp, "rb") as fh:
            os.fsync(fh.fileno())
        dest.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        os.replace(temp, dest)
        fsync_dir(dest.parent)
        copy = StoredFile(
            id=new_id,
            owner_id=user.id,
            folder_id=target,
            name=final_name,
            extension=split_extension(final_name)[1],
            mime_type=row.mime_type,
            media_type=row.media_type,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            storage_path=relpath,
            width=row.width,
            height=row.height,
            duration_seconds=row.duration_seconds,
        )
        db.add(copy)
        log_activity(
            db, "file.copy", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
            target_type="file", target_id=new_id, details={"source_id": row.id, "name": final_name},
        )
        db.commit()
        return copy
    except BaseException:
        db.rollback()
        temp.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        raise


def soft_delete_file(db: Session, settings: Settings, user: User, row: StoredFile, client: ClientInfo) -> TrashItem:
    """Pindahkan ke trash. Data fisik TIDAK dihapus."""
    now = utcnow()
    row.is_deleted = True
    row.deleted_at = now
    item = TrashItem(
        owner_id=user.id,
        file_id=row.id,
        original_name=row.name,
        original_folder_id=row.folder_id,
        size_bytes=row.size_bytes,
        deleted_at=now,
        purge_after=now + timedelta(days=settings.TRASH_RETENTION_DAYS),
    )
    db.add(item)
    log_activity(
        db, "file.delete", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
        target_type="file", target_id=row.id, details={"name": row.name, "soft": True},
    )
    db.commit()
    return item
