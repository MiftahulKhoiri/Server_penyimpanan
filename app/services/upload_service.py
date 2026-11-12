"""Upload bertahap (chunked & resumable).

Alur:
  1. init      -> server membuat sesi + file sementara kosong (.tmp/uploads/<id>/data.part)
  2. chunk     -> tiap chunk di-stream langsung ke offset-nya di data.part (tidak ke RAM),
                  SHA-256 chunk dihitung sambil menulis, lalu fdatasync
  3. status    -> klien melihat chunk mana yang sudah diterima untuk melanjutkan (resume)
  4. complete  -> verifikasi ukuran + SHA-256, rename atomik ke lokasi final
"""

from __future__ import annotations

import hashlib
import logging
import math
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, conflict, not_found, unprocessable
from app.database.types import new_uuid, utcnow
from app.models import StoredFile, Upload, UploadChunk, UploadStatus, User
from app.schemas.files import UploadInitRequest, UploadOut
from app.services import file_service
from app.services.activity_service import log_activity
from app.services.storage_paths import DIR_MODE, StoragePaths, UnsafePathError
from app.utils.request_info import ClientInfo

logger = logging.getLogger("server_penyimpanan.uploads")

MAX_TOTAL_CHUNKS = 100_000
READ_BLOCK = 1024 * 1024
WRITABLE = (UploadStatus.PENDING.value, UploadStatus.UPLOADING.value)
TERMINAL = (UploadStatus.COMPLETED.value, UploadStatus.FAILED.value,
            UploadStatus.CANCELLED.value, UploadStatus.EXPIRED.value)


# ---------------- Hash berjalan (in-memory) ----------------
@dataclass
class _HashState:
    next_index: int
    hasher: "hashlib._Hash | None"  # None = tidak valid, hitung ulang saat complete


class UploadHashRegistry:
    """Menyimpan SHA-256 berjalan untuk upload yang chunk-nya datang berurutan,
    sehingga complete tidak perlu membaca ulang seluruh file (penting di USB 2.0 Pi 3).
    Bila urutan acak / server restart, hash dihitung ulang dari disk."""

    def __init__(self) -> None:
        self._states: dict[str, _HashState] = {}
        self._lock = threading.Lock()

    def begin_chunk(self, upload_id: str, index: int) -> "hashlib._Hash | None":
        with self._lock:
            state = self._states.get(upload_id)
            if state is None:
                return hashlib.sha256() if index == 0 else None
            if state.hasher is not None and state.next_index == index:
                return state.hasher.copy()
            return None

    def commit_chunk(self, upload_id: str, index: int, hasher: "hashlib._Hash | None", *, same_as_before: bool) -> None:
        with self._lock:
            state = self._states.get(upload_id)
            if hasher is not None and (state is None and index == 0 or state is not None and state.next_index == index):
                self._states[upload_id] = _HashState(index + 1, hasher)
            elif same_as_before:
                return  # chunk dikirim ulang dengan isi identik: hash berjalan tetap valid
            else:
                self._states[upload_id] = _HashState(-1, None)

    def final_digest(self, upload_id: str, total_chunks: int) -> str | None:
        with self._lock:
            state = self._states.get(upload_id)
            if state and state.hasher is not None and state.next_index == total_chunks:
                return state.hasher.hexdigest()
            return None

    def discard(self, upload_id: str) -> None:
        with self._lock:
            self._states.pop(upload_id, None)


# ---------------- Bantuan ----------------
def get_owned_upload(db: Session, owner_id: int, upload_id: str) -> Upload:
    row = db.get(Upload, upload_id) if len(upload_id) <= 36 else None
    if row is None or row.owner_id != owner_id:
        raise not_found("upload_not_found", "Sesi upload tidak ditemukan")
    return row


def expected_chunk_size(upload: Upload, index: int) -> int:
    if index < upload.total_chunks - 1:
        return upload.chunk_size
    return upload.total_size - upload.chunk_size * (upload.total_chunks - 1)


def ensure_writable(upload: Upload) -> None:
    if upload.status not in WRITABLE:
        raise conflict("upload_not_active", f"Upload tidak aktif (status: {upload.status})")
    if upload.expires_at <= utcnow():
        raise AppError(410, "upload_expired", "Sesi upload sudah kedaluwarsa, mulai ulang upload")


def received_indices(db: Session, upload_id: str) -> list[int]:
    return list(
        db.scalars(
            select(UploadChunk.chunk_index).where(UploadChunk.upload_id == upload_id).order_by(UploadChunk.chunk_index)
        ).all()
    )


def to_out(db: Session, upload: Upload, duplicate_file_id: str | None = None) -> UploadOut:
    indices = received_indices(db, upload.id)
    return UploadOut(
        id=upload.id,
        filename=upload.filename,
        folder_id=upload.folder_id,
        total_size=upload.total_size,
        chunk_size=upload.chunk_size,
        total_chunks=upload.total_chunks,
        status=upload.status,
        received_bytes=upload.received_bytes,
        received_chunks=indices,
        missing_chunks=upload.total_chunks - len(indices),
        expected_sha256=upload.expected_sha256,
        file_id=upload.file_id,
        error_message=upload.error_message,
        created_at=upload.created_at,
        expires_at=upload.expires_at,
        duplicate_file_id=duplicate_file_id,
    )


def find_duplicate(db: Session, owner_id: int, sha256: str, size: int) -> str | None:
    return db.scalar(
        select(StoredFile.id)
        .where(
            StoredFile.owner_id == owner_id,
            StoredFile.sha256 == sha256,
            StoredFile.size_bytes == size,
            StoredFile.is_deleted.is_(False),
        )
        .limit(1)
    )


def _remove_upload_dir(storage: StoragePaths, upload_id: str) -> None:
    try:
        shutil.rmtree(storage.upload_dir(upload_id), ignore_errors=True)
    except UnsafePathError:
        pass


# ---------------- Init ----------------
def init_upload(
    db: Session,
    settings: Settings,
    storage: StoragePaths,
    user: User,
    body: UploadInitRequest,
    client: ClientInfo,
) -> tuple[Upload, str | None]:
    file_service.ensure_extension_allowed(settings, body.filename)
    if body.total_size > settings.MAX_UPLOAD_SIZE_BYTES:
        raise AppError(413, "file_too_large", f"Ukuran file melebihi batas {settings.MAX_UPLOAD_SIZE_BYTES} byte")
    chunk_size = body.chunk_size or settings.UPLOAD_CHUNK_SIZE_DEFAULT
    if not settings.UPLOAD_CHUNK_SIZE_MIN <= chunk_size <= settings.UPLOAD_CHUNK_SIZE_MAX:
        raise unprocessable(
            "invalid_chunk_size",
            f"chunk_size harus {settings.UPLOAD_CHUNK_SIZE_MIN}-{settings.UPLOAD_CHUNK_SIZE_MAX} byte",
        )
    chunk_size = min(chunk_size, body.total_size)
    total_chunks = math.ceil(body.total_size / chunk_size)
    if total_chunks > MAX_TOTAL_CHUNKS:
        raise unprocessable("too_many_chunks", "chunk_size terlalu kecil untuk ukuran file ini")

    folder_id = file_service.resolve_folder_id(db, user.id, body.folder_id)
    active = db.scalar(
        select(func.count(Upload.id)).where(
            Upload.owner_id == user.id, Upload.status.in_(file_service.ACTIVE_UPLOAD_STATUSES)
        )
    ) or 0
    if active >= settings.MAX_ACTIVE_UPLOADS_PER_USER:
        raise AppError(429, "too_many_uploads", "Terlalu banyak upload aktif; selesaikan atau batalkan dulu")
    file_service.ensure_capacity(db, settings, storage, user, body.total_size)

    duplicate = find_duplicate(db, user.id, body.sha256, body.total_size) if body.sha256 else None
    upload_id = new_uuid()
    now = utcnow()
    upload = Upload(
        id=upload_id,
        owner_id=user.id,
        folder_id=folder_id,
        filename=body.filename,
        mime_type=body.mime_type,
        total_size=body.total_size,
        chunk_size=chunk_size,
        total_chunks=total_chunks,
        expected_sha256=body.sha256,
        status=UploadStatus.PENDING.value,
        received_bytes=0,
        temp_path=storage.upload_relpath(upload_id),
        created_at=now,
        expires_at=now + timedelta(hours=settings.UPLOAD_EXPIRE_HOURS),
    )
    directory = storage.upload_dir(upload_id)
    directory.mkdir(parents=True, mode=DIR_MODE)
    try:
        storage.resolve(upload.temp_path).touch(mode=0o600, exist_ok=False)
        db.add(upload)
        log_activity(
            db, "upload.init", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
            target_type="upload", target_id=upload_id,
            details={"filename": body.filename, "total_size": body.total_size, "total_chunks": total_chunks},
        )
        db.commit()
    except BaseException:
        db.rollback()
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return upload, duplicate


# ---------------- Chunk ----------------
def record_chunk(db: Session, settings: Settings, upload: Upload, index: int, size: int, sha256: str) -> bool:
    """Simpan metadata chunk. Return True bila chunk ini sebelumnya sudah ada dengan isi identik.

    Status upload hanya diperbarui bila upload MASIH aktif (UPDATE bersyarat). Dengan begitu
    chunk yang selesai terlambat tidak bisa menghidupkan lagi upload yang sudah gagal,
    dibatalkan, atau sedang/selesai diproses.
    """
    now = utcnow()
    for attempt in range(2):
        existing = db.scalar(
            select(UploadChunk).where(UploadChunk.upload_id == upload.id, UploadChunk.chunk_index == index)
        )
        same = existing is not None and existing.sha256 == sha256
        if existing is not None:
            existing.size_bytes = size
            existing.sha256 = sha256
            existing.received_at = now
        else:
            db.add(UploadChunk(upload_id=upload.id, chunk_index=index, size_bytes=size, sha256=sha256, received_at=now))
        try:
            db.flush()
            break
        except IntegrityError:  # chunk yang sama dikirim paralel
            db.rollback()
            if attempt:
                raise
    received = int(
        db.scalar(select(func.coalesce(func.sum(UploadChunk.size_bytes), 0)).where(UploadChunk.upload_id == upload.id))
        or 0
    )
    result = db.execute(
        update(Upload)
        .where(Upload.id == upload.id, Upload.status.in_(WRITABLE))
        .values(
            status=UploadStatus.UPLOADING.value,
            received_bytes=received,
            expires_at=now + timedelta(hours=settings.UPLOAD_EXPIRE_HOURS),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) != 1:
        db.rollback()
        raise conflict("upload_not_active", "Upload sudah tidak aktif (dibatalkan, gagal, atau sedang diproses)")
    db.commit()
    db.refresh(upload)
    return same


# ---------------- Complete ----------------
def mark_failed(db: Session, storage: StoragePaths, registry: UploadHashRegistry, upload: Upload, code: str) -> None:
    db.rollback()
    db.execute(
        update(Upload).where(Upload.id == upload.id).values(status=UploadStatus.FAILED.value, error_message=code, updated_at=utcnow())
    )
    db.commit()
    _remove_upload_dir(storage, upload.id)
    registry.discard(upload.id)


def _compute_digest_from_disk(db: Session, storage: StoragePaths, upload: Upload) -> str:
    """Baca ulang data.part per chunk (blok 1 MB), verifikasi SHA-256 tiap chunk, hitung SHA-256 total."""
    chunk_hashes: dict[int, str] = {
        int(index): str(sha)
        for index, sha in db.execute(
            select(UploadChunk.chunk_index, UploadChunk.sha256).where(UploadChunk.upload_id == upload.id)
        ).all()
    }
    total = hashlib.sha256()
    path = storage.resolve(upload.temp_path)
    with open(path, "rb") as fh:
        for index in range(upload.total_chunks):
            remaining = expected_chunk_size(upload, index)
            part = hashlib.sha256()
            while remaining:
                block = fh.read(min(READ_BLOCK, remaining))
                if not block:
                    raise AppError(422, "upload_corrupted", "Data upload tidak lengkap di disk")
                part.update(block)
                total.update(block)
                remaining -= len(block)
            if part.hexdigest() != chunk_hashes.get(index):
                raise AppError(422, "upload_corrupted", f"Chunk {index} rusak di disk, upload ulang file")
    return total.hexdigest()


def claim_upload(db: Session, upload: Upload) -> None:
    """Tandai upload 'processing'. Harus dipanggil di bawah kunci eksklusif upload tersebut,
    sehingga tidak ada chunk yang masih menulis dan chunk baru akan ditolak."""
    db.refresh(upload)
    ensure_writable(upload)
    received = len(received_indices(db, upload.id))
    if received != upload.total_chunks:
        raise conflict(
            "upload_incomplete", f"Masih ada {upload.total_chunks - received} chunk yang belum diterima"
        )
    claimed = db.execute(
        update(Upload)
        .where(Upload.id == upload.id, Upload.status.in_(WRITABLE))
        .values(status=UploadStatus.PROCESSING.value, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if getattr(claimed, "rowcount", 0) != 1:
        raise conflict("upload_busy", "Upload sedang diproses")
    db.refresh(upload)


def complete_upload(
    db: Session,
    settings: Settings,
    storage: StoragePaths,
    registry: UploadHashRegistry,
    user: User,
    upload: Upload,
    client: ClientInfo,
) -> StoredFile:
    """Klaim lalu finalisasi (dipakai bila pemanggil tidak perlu kunci, mis. skrip/tes)."""
    claim_upload(db, upload)
    return finish_claimed_upload(db, settings, storage, registry, user, upload, client)


def finish_claimed_upload(
    db: Session,
    settings: Settings,
    storage: StoragePaths,
    registry: UploadHashRegistry,
    user: User,
    upload: Upload,
    client: ClientInfo,
) -> StoredFile:
    """Verifikasi checksum & pindahkan ke lokasi final. Upload harus sudah di-claim."""
    started = time.monotonic()
    try:
        part = storage.resolve(upload.temp_path)
        if part.stat().st_size != upload.total_size:
            raise AppError(422, "upload_corrupted", "Ukuran data upload tidak sesuai")
        digest = registry.final_digest(upload.id, upload.total_chunks)
        verified_from = "stream"
        if digest is None:
            digest = _compute_digest_from_disk(db, storage, upload)
            verified_from = "disk"
        if upload.expected_sha256 and digest != upload.expected_sha256:
            raise AppError(422, "checksum_mismatch", "SHA-256 file tidak cocok; upload ditolak")

        folder_id = upload.folder_id
        if folder_id is not None:
            try:
                file_service.get_owned_folder(db, user.id, folder_id)
            except AppError:
                folder_id = None  # folder tujuan dihapus selama upload: simpan di root
        stored = file_service.finalize_new_file(
            db, settings, storage, user,
            folder_id=folder_id, name=upload.filename, temp_path=part,
            size=upload.total_size, sha256=digest, client=client,
            details={"upload_id": upload.id, "chunks": upload.total_chunks, "verified_from": verified_from},
        )
    except AppError as exc:
        if exc.status_code in (415, 422):
            mark_failed(db, storage, registry, upload, exc.code)
        else:
            db.rollback()
            db.execute(update(Upload).where(Upload.id == upload.id).values(status=UploadStatus.UPLOADING.value))
            db.commit()
        raise
    except BaseException:
        db.rollback()
        db.execute(update(Upload).where(Upload.id == upload.id).values(status=UploadStatus.UPLOADING.value))
        db.commit()
        raise

    refreshed = db.get(Upload, upload.id)
    assert refreshed is not None
    upload = refreshed
    upload.status = UploadStatus.COMPLETED.value
    upload.file_id = stored.id
    upload.completed_at = utcnow()
    db.commit()
    _remove_upload_dir(storage, upload.id)
    registry.discard(upload.id)
    logger.info("Upload %s selesai (%d byte, %.1fs, hash via %s)", upload.id, upload.total_size,
                time.monotonic() - started, verified_from)
    return stored


# ---------------- Cancel ----------------
def cancel_upload(
    db: Session, storage: StoragePaths, registry: UploadHashRegistry, user: User, upload: Upload, client: ClientInfo
) -> Upload:
    db.refresh(upload)
    if upload.status not in WRITABLE:
        raise conflict("upload_not_active", f"Upload tidak bisa dibatalkan (status: {upload.status})")
    result = db.execute(
        update(Upload)
        .where(Upload.id == upload.id, Upload.status.in_(WRITABLE))
        .values(status=UploadStatus.CANCELLED.value, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) != 1:
        db.rollback()
        raise conflict("upload_not_active", "Upload sudah tidak aktif")
    log_activity(
        db, "upload.cancel", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
        target_type="upload", target_id=upload.id,
    )
    db.commit()
    db.refresh(upload)
    _remove_upload_dir(storage, upload.id)
    registry.discard(upload.id)
    return upload


# ---------------- Pembersihan ----------------
ORPHAN_GRACE_SECONDS = 3600
TEMP_FILE_MAX_AGE_SECONDS = 6 * 3600
TERMINAL_ROW_RETENTION_DAYS = 7


def cleanup_uploads(db: Session, storage: StoragePaths, registry: UploadHashRegistry | None = None) -> dict[str, int]:
    """Hapus upload kedaluwarsa, direktori/file sementara yatim, dan baris upload lama."""
    now = utcnow()
    stats = {"expired": 0, "orphan_dirs": 0, "temp_files": 0, "rows_deleted": 0}

    expired = db.scalars(
        select(Upload).where(Upload.status.in_(file_service.ACTIVE_UPLOAD_STATUSES), Upload.expires_at <= now)
    ).all()
    for upload in expired:
        upload.status = UploadStatus.EXPIRED.value
        upload.error_message = "expired"
        _remove_upload_dir(storage, upload.id)
        if registry:
            registry.discard(upload.id)
        stats["expired"] += 1
    db.commit()

    active_ids = set(
        db.scalars(select(Upload.id).where(Upload.status.in_(file_service.ACTIVE_UPLOAD_STATUSES))).all()
    )
    wall = time.time()
    if storage.uploads_tmp.is_dir():
        for entry in storage.uploads_tmp.iterdir():
            if entry.name in active_ids:
                continue
            try:
                age = wall - entry.stat().st_mtime
            except OSError:
                continue
            if age > ORPHAN_GRACE_SECONDS:
                shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)
                stats["orphan_dirs"] += 1
    for directory in (storage.simple_tmp, storage.copy_tmp):
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            try:
                if wall - entry.stat().st_mtime > TEMP_FILE_MAX_AGE_SECONDS:
                    entry.unlink(missing_ok=True)
                    stats["temp_files"] += 1
            except OSError:
                continue

    result = db.execute(
        delete(Upload).where(
            Upload.status.in_(TERMINAL),
            Upload.updated_at < now - timedelta(days=TERMINAL_ROW_RETENTION_DAYS),
        )
    )
    stats["rows_deleted"] = getattr(result, 'rowcount', 0) or 0
    db.commit()
    return stats
