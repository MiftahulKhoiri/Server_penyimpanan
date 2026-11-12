"""Engine SQLite dan manajemen sesi.

Database diinisialisasi secara malas: jika SSD belum ter-mount saat boot,
server tetap hidup (mode degraded, endpoint mengembalikan 503) dan mencoba
lagi secara berkala tanpa pernah membuat file DB di root filesystem.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.database import migrate
from app.services.storage_guard import check_location

logger = logging.getLogger("server_penyimpanan.database")

RETRY_INTERVAL_SECONDS = 5.0


def make_engine(db_path: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA cache_size=-2048")  # 2 MiB per koneksi (hemat RAM Pi 3)
        cur.execute("PRAGMA temp_store=FILE")
        cur.close()

    return engine


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: Engine | None = None
        self._sessionmaker: sessionmaker[Session] | None = None
        self.error: str | None = "not_initialized"
        self._lock = threading.Lock()
        self._last_attempt = 0.0

    @property
    def ready(self) -> bool:
        return self._sessionmaker is not None

    def primary_storage_ok(self) -> bool:
        s = self.settings
        return check_location(s.DATABASE_PATH, s.PRIMARY_MOUNT_POINT, s.REQUIRE_MOUNTED_STORAGE).available

    def initialize(self) -> bool:
        with self._lock:
            if self.ready:
                return True
            self._last_attempt = time.monotonic()
            s = self.settings
            status = check_location(s.DATABASE_PATH, s.PRIMARY_MOUNT_POINT, s.REQUIRE_MOUNTED_STORAGE)
            if not status.available:
                self.error = "primary_storage_unavailable"
                logger.warning("Database tidak diinisialisasi: penyimpanan utama %s", status.reason)
                return False
            engine: Engine | None = None
            try:
                s.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                engine = make_engine(s.DATABASE_PATH)
                if s.AUTO_MIGRATE:
                    migrate.upgrade_to_head(engine, s.DATABASE_PATH)
                elif not migrate.is_at_head(engine, s.DATABASE_PATH):
                    self.error = "migration_required"
                    logger.error("Skema database belum terbaru; jalankan scripts/init_db.py")
                    engine.dispose()
                    return False
            except Exception:
                logger.exception("Gagal menginisialisasi database")
                if engine is not None:
                    engine.dispose()
                self.error = "database_init_failed"
                return False
            self.engine = engine
            self._sessionmaker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
            self.error = None
            logger.info("Database siap")
            return True

    def ensure_ready(self) -> bool:
        if self.ready:
            return True
        if time.monotonic() - self._last_attempt < RETRY_INTERVAL_SECONDS:
            return False
        return self.initialize()

    def session(self) -> Session:
        if self._sessionmaker is None:
            raise RuntimeError("Database belum siap")
        return self._sessionmaker()

    def ping(self) -> bool:
        if self.engine is None:
            return False
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.exception("Ping database gagal")
            return False

    def dispose(self) -> None:
        with self._lock:
            if self.engine is not None:
                self.engine.dispose()
            self.engine = None
            self._sessionmaker = None
            self.error = "not_initialized"
