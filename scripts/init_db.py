#!/usr/bin/env python3
"""Inisialisasi / migrasi database ke skema terbaru.

Pemakaian:  .venv/bin/python scripts/init_db.py
Aman dijalankan berulang. DB lama otomatis di-backup sebelum migrasi.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.database import migrate  # noqa: E402
from app.database.session import make_engine  # noqa: E402
from app.services.storage_guard import check_location  # noqa: E402


def main() -> int:
    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Konfigurasi tidak valid: {exc}", file=sys.stderr)
        return 2
    status = check_location(
        settings.DATABASE_PATH, settings.PRIMARY_MOUNT_POINT, settings.REQUIRE_MOUNTED_STORAGE
    )
    if not status.available:
        print(
            f"GAGAL: lokasi database tidak tersedia ({status.reason}). "
            f"Pastikan {settings.PRIMARY_MOUNT_POINT} ter-mount.",
            file=sys.stderr,
        )
        return 1
    settings.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    engine = make_engine(settings.DATABASE_PATH)
    try:
        before = migrate.current_revision(engine)
        migrate.upgrade_to_head(engine, settings.DATABASE_PATH)
        after = migrate.current_revision(engine)
    finally:
        engine.dispose()
    print(f"Database siap: {settings.DATABASE_PATH} (revisi {before} -> {after})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
