"""Backup SSD -> HDD sebagai SET BACKUP yang konsisten.

Satu set backup = satu snapshot database + seluruh file yang dirujuk snapshot itu,
dicatat dalam manifest. Set hanya dianggap valid bila manifest berstatus
`completed`.

Mengapa konsisten walau pengguna tetap memakai server selama backup:
1. Backup memegang "backup pin" (lihat backup_pin.py) SEBELUM snapshot database.
   Selama pin dipegang, penghapusan permanen tidak menghapus data fisik tetapi
   memindahkannya ke .pending-delete/, sehingga setiap file yang tercatat di
   snapshot tetap bisa dibaca sampai backup selesai.
2. Snapshot database diambil dengan SQLite online backup API (satu transaksi baca,
   konsisten), lalu daftar file diambil DARI SNAPSHOT itu, bukan dari DB live.
3. File di SSD bersifat write-once (nama UUID, tidak pernah ditimpa); SHA-256-nya
   tercatat saat upload. Setiap salinan di-hash sambil disalin dan wajib sama
   dengan SHA-256 di snapshot. Perubahan/kerusakan file selama backup terdeteksi
   dan set dinyatakan gagal, salinan yang berbeda tidak pernah ditulis.

Struktur di BACKUP_STORAGE_PATH:
    files/<user_id>/<aa>/<uuid>   penyimpanan isi bersama antar-set (inkremental,
                                  isi diverifikasi SHA-256, tidak pernah dihapus)
    sets/<set_id>/                set yang sudah final (completed)
        manifest.json             ID, waktu, status, info database, daftar file
        database.db               snapshot database
    sets/.staging-<set_id>/       set yang sedang dibuat / gagal / terputus
                                  (TIDAK PERNAH dianggap valid; dibersihkan run berikutnya)
    database/server-*.db          salinan format lama (dibiarkan, tidak dihapus)
    .tmp/                         file sementara saat menyalin
    .backup.lock                  mencegah dua proses backup berjalan bersamaan

Finalisasi atomik: staging di-rename menjadi sets/<set_id> (rename direktori
dalam filesystem yang sama bersifat atomik). Retensi hanya dijalankan SETELAH set
baru final dan hanya menghapus set lama yang melebihi BACKUP_DB_KEEP; set yang
completed terbaru tidak pernah ikut terhapus.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
from app.services.backup_pin import backup_pin, drain_pending, pending_path
from app.services.storage_guard import check_location
from app.services.storage_paths import (
    DIR_MODE,
    StoragePaths,
    UnsafePathError,
    fsync_dir,
)

logger = logging.getLogger("server_penyimpanan.backup")

BLOCK = 1024 * 1024
MAX_REPORTED_FAILURES = 200
PROGRESS_EVERY_SECONDS = 5.0
MOUNT_CHECK_EVERY = 50
MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"
SNAPSHOT_NAME = "database.db"
SUMMARY_NAME = "set.json"
SETS_DIR = "sets"
STAGING_PREFIX = ".staging-"

SET_IN_PROGRESS = "in_progress"
SET_COMPLETED = "completed"
SET_FAILED = "failed"


class BackupAborted(Exception):
    pass


class BackupBusy(Exception):
    """Proses lain sedang menjalankan backup."""


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


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(BLOCK), b""):
            digest.update(block)
        _drop_page_cache(fh.fileno())
    return digest.hexdigest()


_hash_file = hash_file  # nama lama, dipertahankan untuk kompatibilitas


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


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as fh:
        os.fsync(fh.fileno())


def _write_json_atomic(path: Path, data: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)
    fsync_dir(path.parent)


# ---------------- Kunci proses backup (lintas proses) ----------------
@contextmanager
def backup_process_lock(backup_root: Path) -> Iterator[None]:
    backup_root.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    fd = os.open(backup_root / ".backup.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise BackupBusy("backup_running_elsewhere") from exc
            raise
        yield
    finally:
        os.close(fd)


# ---------------- Set backup: baca & validasi ----------------
@dataclass(frozen=True)
class BackupSet:
    """`manifest` = ringkasan kecil (set.json): tanpa daftar file, aman dibaca untuk banyak set
    sekaligus di Pi 3. Daftar file lengkap dibaca hanya saat dibutuhkan lewat load_manifest()."""

    set_id: str
    path: Path
    manifest: dict

    def load_manifest(self) -> dict:
        return read_manifest(self.path) or {"status": "unreadable"}

    @property
    def status(self) -> str:
        return str(self.manifest.get("status"))

    @property
    def database_path(self) -> Path:
        return self.path / SNAPSHOT_NAME


def sets_root(backup_root: Path) -> Path:
    return backup_root / SETS_DIR


def read_manifest(set_dir: Path) -> dict | None:
    try:
        with open(set_dir / MANIFEST_NAME, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_summary(set_dir: Path) -> dict | None:
    """Ringkasan set. Status valid hanya bila ringkasan DAN manifest sama-sama ada."""
    try:
        with open(set_dir / SUMMARY_NAME, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        # Set tanpa ringkasan (mis. dibuat tangan): baca manifest; tetap divalidasi penuh saat restore.
        data = read_manifest(set_dir)
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k != "files"}
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not (set_dir / MANIFEST_NAME).is_file():
        return {**data, "status": "manifest_missing"}
    return data


def list_sets(backup_root: Path) -> list[BackupSet]:
    """Set final (bukan staging), urut dari terlama. Status dibaca dari manifest."""
    root = sets_root(backup_root)
    if not root.is_dir():
        return []
    result = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        manifest = read_summary(entry)
        if manifest is None:
            manifest = {"status": "unreadable"}
        result.append(BackupSet(entry.name, entry, manifest))
    return result


def completed_sets(backup_root: Path) -> list[BackupSet]:
    return [s for s in list_sets(backup_root) if s.status == SET_COMPLETED and s.manifest.get("set_id") == s.set_id]


def sqlite_integrity_ok(path: Path) -> bool:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and row[0] == "ok"
        finally:
            con.close()
    except sqlite3.Error:
        return False


def validate_set(backup_root: Path, backup_set: BackupSet, *, check_files: bool = True) -> list[str]:
    """Kembalikan daftar masalah; kosong = set valid untuk restore."""
    problems: list[str] = []
    if backup_set.status != SET_COMPLETED:
        return [f"status_{backup_set.status}"]
    manifest = backup_set.load_manifest()
    if manifest.get("status") != SET_COMPLETED:
        return [f"manifest_status_{manifest.get('status')}"]
    if manifest.get("set_id") != backup_set.set_id:
        return ["manifest_set_id_mismatch"]
    db_info = manifest.get("database") or {}
    db_path = backup_set.database_path
    if not db_path.is_file():
        return ["database_missing"]
    if hash_file(db_path) != db_info.get("sha256"):
        return ["database_checksum_mismatch"]
    if not sqlite_integrity_ok(db_path):
        return ["database_integrity_failed"]
    if not check_files:
        return problems
    store = StoragePaths(backup_root)
    for entry in manifest.get("files", []):
        try:
            path = store.resolve(entry["path"])
        except (UnsafePathError, KeyError, TypeError):
            problems.append(f"unsafe_path:{entry.get('file_id')}")
            continue
        if not path.is_file() or path.stat().st_size != entry.get("size"):
            problems.append(f"file_missing:{entry.get('file_id')}")
        elif hash_file(path) != entry.get("sha256"):
            problems.append(f"file_checksum_mismatch:{entry.get('file_id')}")
        if len(problems) >= MAX_REPORTED_FAILURES:
            break
    return problems


# ---------------- Database snapshot ----------------
def snapshot_database(settings: Settings, target: Path) -> dict:
    """Snapshot konsisten (online backup API) + integrity_check + fsync."""
    src = sqlite3.connect(str(settings.DATABASE_PATH), timeout=30)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # file tunggal tanpa -wal
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if result != "ok":
        raise BackupAborted("database_integrity_failed")
    _fsync_file(target)
    return {"file": target.name, "size_bytes": target.stat().st_size, "sha256": hash_file(target), "integrity": "ok"}


def backup_database_file(settings: Settings, backup_root: Path) -> dict:
    """Salinan database format lama di database/ (dipertahankan untuk kompatibilitas skrip)."""
    target_dir = backup_root / "database"
    target_dir.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    name = f"server-{utcnow().strftime('%Y%m%dT%H%M%S%fZ')}.db"
    temp = target_dir / f"{name}.tmp"
    try:
        info = snapshot_database(settings, temp)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    final = target_dir / name
    os.replace(temp, final)
    fsync_dir(target_dir)
    return {**info, "file": name}


# ---------------- Job ----------------
def mark_interrupted_jobs(db: Session) -> int:
    """Job yang masih 'running' saat server start berarti terputus (crash/restart)."""
    result = db.execute(
        update(BackupJob)
        .where(BackupJob.status.in_([BackupStatus.PENDING.value, BackupStatus.RUNNING.value]))
        .values(status=BackupStatus.FAILED.value, error_message="interrupted", finished_at=utcnow())
    )
    db.commit()
    return getattr(result, "rowcount", 0) or 0


def _cleanup_staging(backup_root: Path) -> int:
    """Hapus staging dari run sebelumnya (gagal/terputus). Aman: dipanggil di bawah kunci proses."""
    root = sets_root(backup_root)
    removed = 0
    if root.is_dir():
        for entry in root.iterdir():
            if entry.name.startswith(STAGING_PREFIX) and entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
    tmp = backup_root / ".tmp"
    if tmp.is_dir():
        for entry in tmp.iterdir():
            if entry.is_file():
                entry.unlink(missing_ok=True)
    return removed


def apply_retention(backup_root: Path, keep: int, protect: str) -> list[str]:
    """Hapus set completed tertua di luar `keep` terbaru. Set `protect` (baru) tak pernah dihapus.
    Set berstatus selain completed di sets/ tidak dihitung dan tidak dihapus di sini."""
    if keep <= 0:
        return []
    valid = completed_sets(backup_root)
    removed = []
    for old in valid[:-keep]:
        if old.set_id == protect:
            continue
        shutil.rmtree(old.path, ignore_errors=True)
        removed.append(old.set_id)
    if removed:
        fsync_dir(sets_root(backup_root))
    return removed


def _source_path(primary_root: Path, relpath: str) -> Path | None:
    src = _resolve_in(primary_root, relpath)
    if src.is_file():
        return src
    try:
        pending = pending_path(primary_root, relpath)
    except UnsafePathError:
        return None
    return pending if pending.is_file() else None


def run_backup(database: Database, settings: Settings, job_id: int, verify_existing: bool = False) -> None:
    """Jalankan satu job backup (sinkron). Dipanggil dari thread latar atau skrip CLI."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)  # hanya thread ini
    except (AttributeError, OSError):
        pass
    db = database.session()
    started = time.monotonic()
    found = db.get(BackupJob, job_id)
    if found is None:
        db.close()
        return
    job: BackupJob = found
    job.status = BackupStatus.RUNNING.value
    job.started_at = utcnow()
    db.commit()

    primary_root = settings.PRIMARY_STORAGE_PATH
    backup_root = settings.BACKUP_STORAGE_PATH
    set_id = f"{utcnow().strftime('%Y%m%dT%H%M%SZ')}-job{job_id:06d}"
    failures: list[dict] = []
    report: dict = {"verify_existing": verify_existing, "set_id": set_id, "set_status": SET_IN_PROGRESS,
                    "database_backup": None, "failures": failures}
    # ID file yang sudah terverifikasi. Ditulis ke DB live dalam transaksi singkat secara
    # berkala: transaksi tulis TIDAK boleh terbuka selama menyalin file (bisa bermenit-menit
    # di Pi 3), karena kunci tulis SQLite akan memblokir login/upload pengguna lain.
    verified: list[str] = []
    staging = sets_root(backup_root) / f"{STAGING_PREFIX}{set_id}"
    manifest: dict = {}

    def flush() -> None:
        _flush_verified(db, verified)

    def fail(file_id: str, reason: str) -> None:
        job.files_failed += 1
        if len(failures) < MAX_REPORTED_FAILURES:
            failures.append({"file_id": file_id, "reason": reason})

    def mark_staging_failed(reason: str) -> None:
        if manifest and staging.is_dir():
            try:
                _write_json_atomic(staging / MANIFEST_NAME, {**manifest, "files": [], "status": SET_FAILED,
                                                             "error": reason, "finished_at": utcnow().isoformat()})
            except OSError:
                pass

    try:
        if not backup_location_ok(settings):
            raise BackupAborted("backup_storage_unavailable")
        with backup_process_lock(backup_root):
            for sub in ("files", ".tmp", SETS_DIR):
                (backup_root / sub).mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
            report["stale_staging_removed"] = _cleanup_staging(backup_root)
            staging.mkdir(mode=DIR_MODE)
            manifest = {
                "manifest_version": MANIFEST_VERSION, "set_id": set_id, "backup_job_id": job_id,
                "created_at": utcnow().isoformat(), "status": SET_IN_PROGRESS,
            }
            _write_json_atomic(staging / MANIFEST_NAME, manifest)

            with backup_pin(primary_root):
                manifest["snapshot_at"] = utcnow().isoformat()
                db_info = snapshot_database(settings, staging / SNAPSHOT_NAME)
                report["database_backup"] = db_info
                files_out = staging / "files.jsonl"
                _copy_files(staging, files_out, settings, job, db, verify_existing, verified, fail, report)
                flush()

            if job.files_failed:
                raise BackupAborted("required_files_failed")

            # Verifikasi akhir sebelum finalisasi: snapshot DB tidak berubah sejak ditulis.
            if hash_file(staging / SNAPSHOT_NAME) != db_info["sha256"] or not sqlite_integrity_ok(staging / SNAPSHOT_NAME):
                raise BackupAborted("database_verification_failed")
            _write_final_manifest(staging, manifest, db_info, files_out)
            files_out.unlink(missing_ok=True)
            final = sets_root(backup_root) / set_id
            os.rename(staging, final)  # atomik: set hanya terlihat setelah lengkap
            fsync_dir(sets_root(backup_root))
            report["set_status"] = SET_COMPLETED
            report["retention_removed"] = apply_retention(backup_root, settings.BACKUP_DB_KEEP, set_id)
        job.status = BackupStatus.COMPLETED.value
    except BackupBusy:
        job.status = BackupStatus.FAILED.value
        job.error_message = "backup_running_elsewhere"
        report["set_status"] = SET_FAILED
    except BackupAborted as exc:
        flush()
        mark_staging_failed(str(exc))
        job.status = BackupStatus.FAILED.value
        job.error_message = str(exc)
        report["set_status"] = SET_FAILED
    except OSError as exc:
        logger.exception("Backup gagal (I/O)")
        db.rollback()
        reason = "backup_storage_full" if exc.errno == errno.ENOSPC else f"io_error:{exc.errno}"
        mark_staging_failed(reason)
        job = _reload_job(db, job_id)
        job.status = BackupStatus.FAILED.value
        job.error_message = reason
        report["set_status"] = SET_FAILED
    except Exception as exc:
        logger.exception("Backup gagal")
        db.rollback()
        mark_staging_failed(exc.__class__.__name__)
        job = _reload_job(db, job_id)
        job.status = BackupStatus.FAILED.value
        job.error_message = exc.__class__.__name__
        report["set_status"] = SET_FAILED
    finally:
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        job.report = json.dumps(report, ensure_ascii=False)
        job.finished_at = utcnow()
        log_activity(
            db, f"backup.{job.status}", user_id=job.triggered_by_id, target_type="backup_job", target_id=job.id,
            details={"copied": job.files_copied, "skipped": job.files_skipped, "failed": job.files_failed,
                     "bytes": job.bytes_copied, "error": job.error_message, "set_id": set_id},
        )
        db.commit()
        db.close()
        try:
            drain_pending(primary_root)
        except OSError:
            logger.warning("Gagal mengosongkan .pending-delete setelah backup")
        logger.info("Backup #%s selesai: %s", job_id, job.status)


def _reload_job(db: Session, job_id: int) -> BackupJob:
    reloaded = db.get(BackupJob, job_id)
    assert reloaded is not None
    return reloaded


def _copy_files(
    staging: Path, files_out: Path, settings: Settings, job: BackupJob, db: Session,
    verify_existing: bool, verified: list[str], fail: Callable[[str, str], None], report: dict,
) -> None:
    """Salin semua file yang dirujuk SNAPSHOT ke penyimpanan isi bersama, tulis daftar ke files.jsonl."""
    primary_root = settings.PRIMARY_STORAGE_PATH
    backup_root = settings.BACKUP_STORAGE_PATH
    snapshot = sqlite3.connect(f"file:{staging / SNAPSHOT_NAME}?mode=ro", uri=True)
    last_flush = time.monotonic()
    try:
        total = snapshot.execute("SELECT count(*) FROM files").fetchone()[0]
        job.files_total = int(total)
        db.commit()
        # Termasuk file di trash: snapshot memuat barisnya, jadi datanya juga wajib ada.
        cursor = snapshot.execute(
            "SELECT id, storage_path, size_bytes, sha256, backed_up_at FROM files ORDER BY id"
        )
        with open(files_out, "w", encoding="utf-8") as out:
            for count, (file_id, relpath, size, sha, backed_up_at) in enumerate(cursor, start=1):
                if count % MOUNT_CHECK_EVERY == 0 and not backup_location_ok(settings):
                    raise BackupAborted("backup_storage_lost")
                entry = {"file_id": file_id, "path": relpath, "size": size, "sha256": sha}
                try:
                    dest = _resolve_in(backup_root, relpath)
                    src = _source_path(primary_root, relpath)
                except UnsafePathError:
                    fail(file_id, "unsafe_path")
                    continue

                if dest.is_file() and dest.stat().st_size == size:
                    # Salinan berukuran sama sudah ada. Verifikasi isi bila diminta atau
                    # bila belum pernah tercatat selesai (mis. job sebelumnya terputus).
                    if backed_up_at is not None and not verify_existing:
                        job.files_skipped += 1
                        out.write(json.dumps(entry) + "\n")
                        continue
                    if hash_file(dest) == sha:
                        if backed_up_at is None:
                            verified.append(file_id)
                        job.files_skipped += 1
                        out.write(json.dumps(entry) + "\n")
                        continue
                    report["repaired"] = report.get("repaired", 0) + 1  # salinan di HDD rusak: salin ulang

                if src is None:
                    fail(file_id, "source_missing")
                    continue
                if disk_usage(backup_root).free < size + settings.BACKUP_MIN_FREE_SPACE_BYTES:
                    fail(file_id, "insufficient_space")
                    raise BackupAborted("backup_storage_full")

                temp = backup_root / ".tmp" / f"{file_id}.part"
                try:
                    try:
                        copied_sha = _copy_with_hash(src, temp)
                    except FileNotFoundError:
                        # File bisa saja dipindah ke .pending-delete/ (purge) tepat setelah
                        # lokasinya diperiksa: cari ulang sekali sebelum menyerah.
                        src = _source_path(primary_root, relpath)
                        if src is None:
                            raise
                        copied_sha = _copy_with_hash(src, temp)
                except OSError as exc:
                    temp.unlink(missing_ok=True)
                    if exc.errno == errno.ENOSPC:
                        fail(file_id, "insufficient_space")
                        raise BackupAborted("backup_storage_full") from exc
                    fail(file_id, "source_missing" if exc.errno == errno.ENOENT else f"io_error:{exc.errno}")
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
                out.write(json.dumps(entry) + "\n")
                verified.append(file_id)
                job.files_copied += 1
                job.bytes_copied += size

                if time.monotonic() - last_flush > PROGRESS_EVERY_SECONDS or len(verified) >= 200:
                    _flush_verified(db, verified)
                    last_flush = time.monotonic()
            out.flush()
            os.fsync(out.fileno())
    except sqlite3.DatabaseError as exc:
        raise BackupAborted("database_snapshot_unreadable") from exc
    finally:
        snapshot.close()


def _flush_verified(db: Session, verified: list[str]) -> None:
    stamp = utcnow()
    for start in range(0, len(verified), 500):
        batch = verified[start : start + 500]
        db.execute(update(StoredFile).where(StoredFile.id.in_(batch)).values(backed_up_at=stamp))
    verified.clear()
    db.commit()


def _write_final_manifest(staging: Path, manifest: dict, db_info: dict, files_jsonl: Path) -> None:
    """Tulis manifest final secara streaming (daftar file bisa ratusan ribu baris; jangan muat ke RAM)."""
    header = {**manifest, "status": SET_COMPLETED, "finished_at": utcnow().isoformat(),
              "database": {"file": SNAPSHOT_NAME, "size_bytes": db_info["size_bytes"],
                           "sha256": db_info["sha256"], "integrity": db_info["integrity"]}}
    count = 0
    total_bytes = 0
    temp = staging / (MANIFEST_NAME + ".tmp")
    with open(temp, "w", encoding="utf-8") as fh, open(files_jsonl, encoding="utf-8") as src:
        head = json.dumps(header, ensure_ascii=False)
        fh.write(head[:-1] + ', "files": [')
        for line in src:
            entry = json.loads(line)
            fh.write(("," if count else "") + "\n" + json.dumps(entry, ensure_ascii=False))
            count += 1
            total_bytes += int(entry["size"])
        fh.write(f'\n], "file_count": {count}, "total_bytes": {total_bytes}}}')
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, staging / MANIFEST_NAME)
    _write_json_atomic(staging / SUMMARY_NAME, {**header, "file_count": count, "total_bytes": total_bytes})


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
