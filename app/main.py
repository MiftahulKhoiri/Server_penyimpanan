"""Application factory Server_penyimpanan.

Dijalankan dengan: uvicorn --factory app.main:create_app  (lihat run.py)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import openapi_meta
from app.api.v1.router import api_router
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.rate_limit import LoginRateLimiter
from app.core.security import SecurityService
from app.database.session import Database
from app.services.storage_guard import check_location
from app.services.system_service import prime_cpu_counter
from app.services.storage_paths import StoragePaths
from app import __version__ as _version
from app.services import trash_service
from app.services.activity_service import log_activity
from app.services.backup_service import BackupManager, mark_interrupted_jobs, maybe_start_auto_backup
from app.services.upload_locks import UploadLocks
from app.services.upload_service import UploadHashRegistry, cleanup_uploads

logger = logging.getLogger("server_penyimpanan")


def run_maintenance_once(app: FastAPI) -> dict[str, int] | None:
    """Bersihkan upload kedaluwarsa & file sementara. Dilewati bila SSD/DB tidak siap."""
    settings: Settings = app.state.settings
    database: Database = app.state.database
    if not database.ensure_ready():
        return None
    status = check_location(
        settings.PRIMARY_STORAGE_PATH, settings.PRIMARY_MOUNT_POINT, settings.REQUIRE_MOUNTED_STORAGE
    )
    if not status.available:
        return None
    storage = StoragePaths(settings.PRIMARY_STORAGE_PATH)
    storage.ensure_layout()
    with database.session() as db:
        stats = cleanup_uploads(db, storage, app.state.upload_hashes)
        stats["trash_purged"] = trash_service.purge_expired(db, storage)
        stats["auto_backup_started"] = int(
            maybe_start_auto_backup(db, settings, app.state.backup_manager) is not None
        )
    if any(stats.values()):
        logger.info("Pemeliharaan: %s", stats)
    return stats


def _on_startup(app: FastAPI) -> None:
    database: Database = app.state.database
    if not database.initialize():
        logger.warning("Server berjalan dalam mode degraded: %s", database.error)
        return
    settings: Settings = app.state.settings
    with database.session() as db:
        interrupted = mark_interrupted_jobs(db)
        log_activity(
            db, "system.start",
            details={
                "version": _version,
                "allow_registration": settings.ALLOW_REGISTRATION,
                "require_mounted_storage": settings.REQUIRE_MOUNTED_STORAGE,
                "backup_auto_interval_hours": settings.BACKUP_AUTO_INTERVAL_HOURS,
                "trash_retention_days": settings.TRASH_RETENTION_DAYS,
                "interrupted_backups": interrupted,
            },
        )
        db.commit()


async def _maintenance_loop(app: FastAPI, interval: int) -> None:
    await asyncio.sleep(min(30, interval))
    while True:
        try:
            await run_in_threadpool(run_maintenance_once, app)
        except Exception:
            logger.exception("Pembersihan berkala gagal")
        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        import anyio.to_thread

        anyio.to_thread.current_default_thread_limiter().total_tokens = app.state.settings.THREADPOOL_SIZE
        await run_in_threadpool(_on_startup, app)
        task = None
        interval = app.state.settings.UPLOAD_CLEANUP_INTERVAL_SECONDS
        if interval > 0:
            task = asyncio.create_task(_maintenance_loop(app, interval))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await run_in_threadpool(app.state.backup_manager.wait, 5)
            app.state.database.dispose()

    app = FastAPI(
        title="Server_penyimpanan API",
        description=openapi_meta.DESCRIPTION,
        version=__version__,
        openapi_tags=openapi_meta.TAGS,
        # operationId = nama fungsi endpoint: stabil & enak dipakai generator client Kotlin.
        generate_unique_id_function=lambda route: route.name,
        license_info={"name": "GPL-3.0", "url": "https://www.gnu.org/licenses/gpl-3.0.html"},
        docs_url="/docs" if settings.ENABLE_DOCS else None,
        redoc_url="/redoc" if settings.ENABLE_DOCS else None,
        openapi_url="/openapi.json" if settings.ENABLE_DOCS else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = Database(settings)
    app.state.security = SecurityService(settings)
    app.state.backup_manager = BackupManager(app.state.database, settings)
    app.state.upload_hashes = UploadHashRegistry()
    app.state.upload_locks = UploadLocks()
    app.state.started_monotonic = time.monotonic()
    prime_cpu_counter()
    app.state.login_limiter = LoginRateLimiter(
        settings.LOGIN_MAX_ATTEMPTS, settings.LOGIN_WINDOW_SECONDS, settings.LOGIN_LOCKOUT_SECONDS
    )

    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=False,  # autentikasi lewat header Bearer, bukan cookie
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Range", "Content-Range", "X-Chunk-SHA256"],
            expose_headers=["Content-Range", "Accept-Ranges", "Content-Length", "Content-Disposition", "Retry-After"],
            max_age=600,
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/v1/auth") or request.url.path.startswith("/api/v1/admin"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # Ditambahkan terakhir = lapisan terluar: menolak body raksasa sebelum dibaca aplikasi.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.MAX_REQUEST_BODY_BYTES)
    register_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v1")
    return app
