#!/usr/bin/env python3
"""Jalankan backup SSD -> HDD sekarang dari command line (sinkron, menampilkan laporan).

Pemakaian (sebagai user layanan, lewat manage.sh):
    sudo scripts/manage.sh backup-now [--verify-existing]

Aman dijalankan bersamaan dengan server: bila backup lain sedang berjalan, script berhenti.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.database.session import Database  # noqa: E402
from app.database.types import utcnow  # noqa: E402
from app.models import BackupJob, BackupStatus  # noqa: E402
from app.services.backup_service import backup_location_ok, run_backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup SSD -> HDD sekarang")
    parser.add_argument("--verify-existing", action="store_true", help="hitung ulang SHA-256 salinan di HDD")
    args = parser.parse_args()
    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Konfigurasi tidak valid: {exc}", file=sys.stderr)
        return 2
    if not backup_location_ok(settings):
        print(f"GAGAL: HDD backup tidak tersedia ({settings.BACKUP_MOUNT_POINT} tidak ter-mount/ditulis)", file=sys.stderr)
        return 1
    database = Database(settings)
    if not database.initialize():
        print(f"GAGAL: database tidak siap ({database.error})", file=sys.stderr)
        return 1
    db = database.session()
    try:
        running = db.scalar(
            select(BackupJob.id).where(BackupJob.status.in_([BackupStatus.PENDING.value, BackupStatus.RUNNING.value]))
        )
        if running:
            print(f"GAGAL: backup #{running} sedang berjalan", file=sys.stderr)
            return 1
        job = BackupJob(status=BackupStatus.PENDING.value, created_at=utcnow())
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()
    print(f"Backup #{job_id} dimulai ...")
    run_backup(database, settings, job_id, args.verify_existing)
    db = database.session()
    job = db.get(BackupJob, job_id)
    assert job is not None
    print(f"Status      : {job.status}")
    print(f"File        : {job.files_total} total, {job.files_copied} disalin, "
          f"{job.files_skipped} dilewati, {job.files_failed} gagal")
    print(f"Data disalin: {job.bytes_copied / 1024**2:.1f} MiB")
    if job.error_message:
        print(f"Error       : {job.error_message}")
    report = json.loads(job.report or "{}")
    if report.get("database_backup"):
        print(f"Database    : {report['database_backup']['file']} (integrity {report['database_backup']['integrity']})")
    for failure in report.get("failures", [])[:20]:
        print(f"  gagal {failure['file_id']}: {failure['reason']}")
    ok = job.status == BackupStatus.COMPLETED.value
    db.close()
    database.dispose()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
