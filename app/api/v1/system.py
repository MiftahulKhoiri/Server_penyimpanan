"""Endpoint sistem: health (publik), status & statistik (admin)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.core.dependencies import AdminUser, DbSession, SettingsDep, get_database
from app.database.session import Database
from app.schemas.system import DeviceOut, StatisticsOut, SystemStatusOut
from app.services import media_service, storage_service, system_service

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
def health(database: Annotated[Database, Depends(get_database)], request: Request) -> JSONResponse:
    """Cek kesehatan ringan, tanpa autentikasi dan tanpa membocorkan path.

    200 bila database dan penyimpanan utama siap, 503 bila tidak.
    Status HDD backup hanya informasi: HDD tidak ada tidak membuat server tidak sehat.
    """
    database.ensure_ready()
    storage_ok = database.primary_storage_ok()
    db_ok = database.ready and database.ping()
    healthy = storage_ok and db_ok
    backup = storage_service.device_status(request.app.state.settings, "hdd")
    body = {
        "status": "ok" if healthy else "degraded",
        "database": "ok" if db_ok else (database.error or "unavailable"),
        "primary_storage": "ok" if storage_ok else "unavailable",
        "backup_storage": "ok" if backup.available else "unavailable",
    }
    return JSONResponse(status_code=200 if healthy else 503, content=body)


@router.get("/status", response_model=SystemStatusOut, responses={403: {"description": "Bukan admin"}})
def system_status(admin: AdminUser, request: Request, db: DbSession, settings: SettingsDep) -> SystemStatusOut:
    """Status server untuk dashboard: CPU, RAM, suhu, uptime, database, SSD/HDD."""
    database: Database = request.app.state.database
    devices = storage_service.all_devices(settings)
    storage_service.save_snapshot(db, devices)
    db_ok = database.ping()
    healthy = db_ok and devices["ssd"].available
    info = system_service.runtime_info(request.app.state.started_monotonic)
    return SystemStatusOut(
        status="ok" if healthy else "degraded",
        database="ok" if db_ok else "error",
        storage={name: DeviceOut(**d.as_dict()) for name, d in devices.items()},
        media_tools={
            "pillow": _pillow_available(),
            "ffmpeg": media_service.tool_available(settings.FFMPEG_PATH),
            "ffprobe": media_service.tool_available(settings.FFPROBE_PATH),
        },
        **info,
    )


@router.get("/statistics", response_model=StatisticsOut, responses={403: {"description": "Bukan admin"}})
def system_statistics(admin: AdminUser, db: DbSession) -> StatisticsOut:
    """Jumlah pengguna, file, ukuran total, per jenis media, trash, upload aktif."""
    return StatisticsOut(**system_service.statistics(db))


def _pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True
