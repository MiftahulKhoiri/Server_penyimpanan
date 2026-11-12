"""Informasi sistem ringan untuk dashboard (psutil + /proc, tanpa proses berat)."""

from __future__ import annotations

import os
import platform
import time
from datetime import timedelta
from pathlib import Path

import psutil
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app import __version__
from app.database.types import utcnow
from app.models import ActivityLog, Folder, StoredFile, TrashItem, Upload, User
from app.services.file_service import ACTIVE_UPLOAD_STATUSES

_THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")


def prime_cpu_counter() -> None:
    # Panggilan pertama psutil.cpu_percent(None) selalu 0; panggil sekali saat start.
    psutil.cpu_percent(interval=None)


def cpu_temperature() -> float | None:
    try:
        return round(int(_THERMAL.read_text().strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


def runtime_info(started_monotonic: float) -> dict:
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    proc = psutil.Process()
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = None
    return {
        "version": __version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}",
        "server_uptime_seconds": int(time.monotonic() - started_monotonic),
        "system_uptime_seconds": int(time.time() - psutil.boot_time()),
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "count": psutil.cpu_count() or 1,
            "load_average": load,
            "temperature_c": cpu_temperature(),
        },
        "memory": {
            "total_bytes": vm.total,
            "available_bytes": vm.available,
            "used_percent": vm.percent,
            "swap_total_bytes": swap.total,
            "swap_used_bytes": swap.used,
            "process_rss_bytes": proc.memory_info().rss,
        },
    }


def statistics(db: Session) -> dict:
    users_total, users_active = db.execute(
        select(func.count(User.id), func.coalesce(func.sum(case((User.is_active.is_(True), 1), else_=0)), 0))
    ).one()
    files_count, files_bytes = db.execute(
        select(func.count(StoredFile.id), func.coalesce(func.sum(StoredFile.size_bytes), 0)).where(
            StoredFile.is_deleted.is_(False)
        )
    ).one()
    by_media = {
        media: {"count": int(count), "total_bytes": int(size)}
        for media, count, size in db.execute(
            select(StoredFile.media_type, func.count(StoredFile.id), func.coalesce(func.sum(StoredFile.size_bytes), 0))
            .where(StoredFile.is_deleted.is_(False))
            .group_by(StoredFile.media_type)
        ).all()
    }
    trash_count, trash_bytes = db.execute(
        select(func.count(TrashItem.id), func.coalesce(func.sum(TrashItem.size_bytes), 0))
    ).one()
    folders = db.scalar(select(func.count(Folder.id)).where(Folder.is_deleted.is_(False))) or 0
    active_uploads = db.scalar(select(func.count(Upload.id)).where(Upload.status.in_(ACTIVE_UPLOAD_STATUSES))) or 0
    since = utcnow() - timedelta(hours=24)
    activity_24h = db.scalar(select(func.count(ActivityLog.id)).where(ActivityLog.created_at >= since)) or 0
    uploads_24h = db.scalar(
        select(func.count(ActivityLog.id)).where(ActivityLog.created_at >= since, ActivityLog.action == "file.upload")
    ) or 0
    return {
        "users": {"total": int(users_total), "active": int(users_active)},
        "files": {"count": int(files_count), "total_bytes": int(files_bytes), "by_media_type": by_media},
        "folders": int(folders),
        "trash": {"items": int(trash_count), "total_bytes": int(trash_bytes)},
        "active_uploads": int(active_uploads),
        "last_24h": {"activities": int(activity_24h), "uploads": int(uploads_24h)},
    }

