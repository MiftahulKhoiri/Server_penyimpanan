"""Application factory Server_penyimpanan.

Dijalankan dengan: uvicorn --factory app.main:create_app  (lihat run.py)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.rate_limit import LoginRateLimiter
from app.core.security import SecurityService
from app.database.session import Database
from app.services.storage_guard import check_location
from app.services.storage_paths import StoragePaths
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
    if any(stats.values()):
        logger.info("Pembersihan upload: %s", stats)
    return stats


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
        if not app.state.database.initialize():
            logger.warning("Server berjalan dalam mode degraded: %s", app.state.database.error)
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
            app.state.database.dispose()

    app = FastAPI(
        title="Server_penyimpanan API",
        description="Backend Personal Cloud Storage untuk Raspberry Pi",
        version=__version__,
        docs_url="/docs" if settings.ENABLE_DOCS else None,
        redoc_url="/redoc" if settings.ENABLE_DOCS else None,
        openapi_url="/openapi.json" if settings.ENABLE_DOCS else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = Database(settings)
    app.state.security = SecurityService(settings)
    app.state.upload_hashes = UploadHashRegistry()
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

    register_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v1")
    return app
