"""Backup SSD -> HDD.

Prinsip keamanan data:
- File asli di SSD TIDAK PERNAH dihapus atau diubah oleh backup.
- Salinan di HDD juga tidak dihapus walau file asalnya dihapus (bukan mirror).
- Tiap file disalin sambil dihitung SHA-256-nya dan dibandingkan dengan SHA-256
  yang tercatat saat upload. Bila berbeda (data SSD rusak), salinan lama di HDD
  dibiarkan utuh dan kegagalan dicatat di laporan.
- Salinan ditulis ke file sementara, di-fsync, lalu di-rename (atomik).
- Database ikut di-backup memakai SQLite online backup API + integrity_check.

Struktur di BACKUP_STORAGE_PATH:
    files/<user_id>/<aa>/<uuid>      (sama dengan struktur di SSD)
    database/server-<waktu>.db
    .tmp/                            file sementara saat menyalin
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path
from shutil import disk_usage

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, conflict, service_unavailable
from app.database.session import Database
from app.database.types import utcnow
from app.models import BackupJob, BackupStatus, StoredFile
from app.services.activity_service import log_activity
from app.services.storage_guard import check_location
from app.services.storage_paths import DIR_MODE, StoragePaths, UnsafePathError, fsync_dir

logger = logging.getLogger("server_penyimpanan.backup")

BLOCK = 1024 * 1024
MAX_REPORTED_FAILURES = 200
PROGRESS_EVERY_SECONDS = 5.0
MOUNT_CHECK_EVERY = 50


class BackupAborted(Exception):
    pass


def backup_location_ok(settings: Settings) -> bool:
    return check_location(
        settings.BACKUP_STORAGE_PATH, settings.BACKUP_MOUNT_POINT, settings.REQUIRE_MOUNTED_STORAGE
    ).available


def _drop_page_cache(fd: int) -> None:
    # Jangan biarkan backup besar mengusir cache yang berguna dari RAM 1 GB.
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(BLOCK), b""):
            digest.update(block)
        _drop_page_cache(fh.fileno())
    return digest.hexdigest()


def _copy_with_hash(src: Path, dest: Path) -> str:
    digest = hashlib.sha256()
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        for block in iter(lambda: fin.read(BLOCK), b""):
            digest.update(block)
            fout.write(block)
        fout.flush()
        os.fsync(fout.fileno())
        _drop_page_cache(fin.fileno())
        _drop_page_cache(fout.fileno())
    return digest.hexdigest()


def _resolve_in(root: Path, relpath: str) -> Path:
    return StoragePaths(root).resolve(relpath)


# ---------------- Database ----------------
def backup_database_file(settings: Settings, backup_root: Path) -> dict:
    target_dir = backup_root / "database"
    target_dir.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    name = f"server-{utcnow().strftime('%Y%m%dT%H%M%S%fZ')}.db"
    temp = target_dir / f"{name}.tmp"
    src = sqlite3.connect(str(settings.DATABASE_PATH), timeout=30)
    try:
        dst = sqlite3.connect(str(temp))
        try:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if result != "ok":
        temp.unlink(missing_ok=True)
        raise BackupAborted("database_integrity_failed")
    with open(temp, "rb+") as fh:
        os.fsync(fh.fileno())
    sha = _hash_file(temp)
    final = target_dir / name
    os.replace(temp, final)
    fsync_dir(target_dir)
    removed = []
    if settings.BACKUP_DB_KEEP > 0:
        copies = sorted(target_dir.glob("server-*.db"))
        for old in copies[: -settings.BACKUP_DB_KEEP]:
            old.unlink(missing_ok=True)
            removed.append(old.name)
    return {"file": name, "size_bytes": final.stat().st_size, "sha256": sha, "integrity": "ok", "rotated_out": removed}


# ---------------- Job ----------------
def mark_interrupted_jobs(db: Session) -> int:
    """Job yang masih 'running' saat server start berarti terputus (crash/restart)."""
    result = db.execute(
        update(BackupJob)
        .where(BackupJob.status.in_([BackupStatus.PENDING.value, BackupStatus.RUNNING.value]))
        .values(status=BackupStatus.FAILED.value, error_message="interrupted", finished_at=utcnow())
    )
    db.commit()
    return result.rowcount or 0


def run_backup(database: Database, settings: Settings, job_id: int, verify_existing: bool = False) -> None:
    """Jalankan satu job backup (sinkron). Dipanggil dari thread latar."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)  # hanya thread ini
    except (AttributeError, OSError):
        pass
    db = database.session()
    started = time.monotonic()
    job = db.get(BackupJob, job_id)
    if job is None:
        db.close()
        return
    job.status = BackupStatus.RUNNING.value
    job.started_at = utcnow()
    db.commit()

    primary_root = settings.PRIMARY_STORAGE_PATH
    backup_root = settings.BACKUP_STORAGE_PATH
    failures: list[dict] = []
    report: dict = {"verify_existing": verify_existing, "database_backup": None, "failures": failures}
    last_progress = time.monotonic()

    def fail(file_id: str, reason: str) -> None:
        job.files_failed += 1
        if len(failures) < MAX_REPORTED_FAILURES:
            failures.append({"file_id": file_id, "reason": reason})

    try:
        if not backup_location_ok(settings):
            raise BackupAborted("backup_storage_unavailable")
        for sub in ("files", "database", ".tmp"):
            (backup_root / sub).mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        report["database_backup"] = backup_database_file(settings, backup_root)

        rows = db.execute(
            select(StoredFile.id, StoredFile.storage_path, StoredFile.size_bytes, StoredFile.sha256, StoredFile.backed_up_at)
            .where(StoredFile.is_deleted.is_(False))
            .order_by(StoredFile.id)
        ).all()
        job.files_total = len(rows)
        db.commit()

        for count, (file_id, relpath, size, sha, backed_up_at) in enumerate(rows, start=1):
            if count % MOUNT_CHECK_EVERY == 0 and not backup_location_ok(settings):
                raise BackupAborted("backup_storage_lost")
            try:
                src = _resolve_in(primary_root, relpath)
                dest = _resolve_in(backup_root, relpath)
            except UnsafePathError:
                fail(file_id, "unsafe_path")
                continue
            if not src.is_file():
                fail(file_id, "source_missing")
                continue

            if dest.is_file() and dest.stat().st_size == size:
                # Sudah ada salinan berukuran sama. Verifikasi isi bila diminta atau
                # bila belum pernah tercatat selesai (mis. job sebelumnya terputus).
                if backed_up_at is not None and not verify_existing:
                    job.files_skipped += 1
                    continue
                if _hash_file(dest) == sha:
                    if backed_up_at is None:
                        db.execute(update(StoredFile).where(StoredFile.id == file_id).values(backed_up_at=utcnow()))
                    job.files_skipped += 1
                    continue
                report.setdefault("repaired", 0)
                report["repaired"] += 1  # salinan di HDD rusak: disalin ulang

            if disk_usage(backup_root).free < size + settings.BACKUP_MIN_FREE_SPACE_BYTES:
                fail(file_id, "insufficient_space")
                raise BackupAborted("backup_storage_full")

            temp = backup_root / ".tmp" / f"{file_id}.part"
            try:
                copied_sha = _copy_with_hash(src, temp)
            except OSError as exc:
                temp.unlink(missing_ok=True)
                fail(file_id, f"io_error:{exc.errno}")
                continue
            if copied_sha != sha:
                # Isi di SSD tidak sama dengan saat upload: jangan timpa salinan lama yang baik.
                temp.unlink(missing_ok=True)
                fail(file_id, "source_checksum_mismatch")
                logger.error("Checksum file %s di SSD tidak cocok dengan catatan upload", file_id)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
            os.replace(temp, dest)
            fsync_dir(dest.parent)
            db.execute(update(StoredFile).where(StoredFile.id == file_id).values(backed_up_at=utcnow()))
            job.files_copied += 1
            job.bytes_copied += size

            if time.monotonic() - last_progress > PROGRESS_EVERY_SECONDS:
                db.commit()
                last_progress = time.monotonic()

        job.status = BackupStatus.COMPLETED.value if job.files_failed == 0 else BackupStatus.PARTIAL.value
    except BackupAborted as exc:
        job.status = BackupStatus.FAILED.value if job.files_copied == 0 else BackupStatus.PARTIAL.value
        job.error_message = str(exc)
    except Exception as exc:
        logger.exception("Backup gagal")
        db.rollback()
        job = db.get(BackupJob, job_id)
        assert job is not None
        job.status = BackupStatus.FAILED.value
        job.error_message = exc.__class__.__name__
    finally:
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        job.report = json.dumps(report, ensure_ascii=False)
        job.finished_at = utcnow()
        log_activity(
            db, f"backup.{job.status}", user_id=job.triggered_by_id, target_type="backup_job", target_id=job.id,
            details={"copied": job.files_copied, "skipped": job.files_skipped, "failed": job.files_failed,
                     "bytes": job.bytes_copied, "error": job.error_message},
        )
        db.commit()
        db.close()
        logger.info("Backup #%s selesai: %s", job_id, job.status)


class BackupManager:
    """Memastikan hanya satu backup berjalan, di thread latar."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, db: Session, user_id: int | None, *, verify_existing: bool = False) -> BackupJob:
        with self._lock:
            if self.running or db.scalar(
                select(BackupJob.id).where(BackupJob.status.in_([BackupStatus.PENDING.value, BackupStatus.RUNNING.value]))
            ):
                raise conflict("backup_running", "Backup lain sedang berjalan")
            if not backup_location_ok(self.settings):
                raise service_unavailable("backup_unavailable", "Penyimpanan backup (HDD) tidak tersedia")
            job = BackupJob(triggered_by_id=user_id, status=BackupStatus.PENDING.value, created_at=utcnow())
            db.add(job)
            db.commit()
            self._thread = threading.Thread(
                target=self._run, args=(job.id, verify_existing), name=f"backup-{job.id}", daemon=True
            )
            self._thread.start()
            return job

    def _run(self, job_id: int, verify_existing: bool) -> None:
        try:
            run_backup(self.database, self.settings, job_id, verify_existing)
        except Exception:
            logger.exception("Thread backup gagal")

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)


def maybe_start_auto_backup(db: Session, settings: Settings, manager: BackupManager) -> BackupJob | None:
    """Mulai backup otomatis bila BACKUP_AUTO_INTERVAL_HOURS > 0 dan sudah waktunya."""
    if settings.BACKUP_AUTO_INTERVAL_HOURS <= 0 or manager.running or not backup_location_ok(settings):
        return None
    last = db.scalar(
        select(BackupJob.created_at)
        .where(BackupJob.status.in_([BackupStatus.COMPLETED.value, BackupStatus.PARTIAL.value,
                                     BackupStatus.RUNNING.value, BackupStatus.PENDING.value]))
        .order_by(BackupJob.created_at.desc())
        .limit(1)
    )
    if last is not None and utcnow() - last < timedelta(hours=settings.BACKUP_AUTO_INTERVAL_HOURS):
        return None
    try:
        return manager.start(db, None)
    except AppError:
        return None
