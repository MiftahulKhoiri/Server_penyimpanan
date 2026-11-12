"""Status perangkat penyimpanan (SSD utama & HDD backup) secara ringan."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from shutil import disk_usage

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.types import utcnow
from app.models import StorageDevice, StoredFile
from app.services.storage_guard import check_location

DEVICES = {
    "ssd": ("primary", "PRIMARY_MOUNT_POINT", "PRIMARY_STORAGE_PATH"),
    "hdd": ("backup", "BACKUP_MOUNT_POINT", "BACKUP_STORAGE_PATH"),
}


@dataclass
class DeviceStatus:
    name: str
    role: str
    mount_point: str
    storage_path: str
    mounted: bool
    available: bool
    reason: str | None
    filesystem: str | None
    device: str | None
    total_bytes: int | None
    used_bytes: int | None
    free_bytes: int | None
    used_percent: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def _mount_table() -> dict[str, tuple[str, str]]:
    table: dict[str, tuple[str, str]] = {}
    try:
        with open("/proc/self/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3:
                    target = parts[1].replace("\\040", " ")
                    table[target] = (parts[0], parts[2])
    except OSError:
        pass
    return table


def _nearest_existing(path: Path) -> Path | None:
    current = path
    while not current.exists():
        if current.parent == current:
            return None
        current = current.parent
    return current


def device_status(settings: Settings, name: str) -> DeviceStatus:
    role, mount_attr, path_attr = DEVICES[name]
    mount_point: Path = getattr(settings, mount_attr)
    storage_path: Path = getattr(settings, path_attr)
    mounted = os.path.ismount(mount_point)
    location = check_location(storage_path, mount_point, settings.REQUIRE_MOUNTED_STORAGE)

    device = filesystem = None
    total = used = free = None
    percent = None
    # Kapasitas hanya dibaca dari perangkat yang benar-benar ter-mount (atau dari
    # filesystem yang memuat path saat mode pengembangan).
    probe = mount_point if mounted else (None if settings.REQUIRE_MOUNTED_STORAGE else _nearest_existing(storage_path))
    if probe is not None:
        try:
            usage = disk_usage(probe)
            total, used, free = usage.total, usage.used, usage.free
            percent = round(used / total * 100, 1) if total else None
        except OSError:
            pass
    if mounted:
        device, filesystem = _mount_table().get(str(mount_point), (None, None))
    return DeviceStatus(
        name=name,
        role=role,
        mount_point=str(mount_point),
        storage_path=str(storage_path),
        mounted=mounted,
        available=location.available,
        reason=location.reason,
        filesystem=filesystem,
        device=device,
        total_bytes=total,
        used_bytes=used,
        free_bytes=free,
        used_percent=percent,
    )


def all_devices(settings: Settings) -> dict[str, DeviceStatus]:
    return {name: device_status(settings, name) for name in DEVICES}


def save_snapshot(db: Session, statuses: dict[str, DeviceStatus]) -> None:
    now = utcnow()
    for status in statuses.values():
        row = db.scalar(select(StorageDevice).where(StorageDevice.name == status.name))
        if row is None:
            row = StorageDevice(name=status.name, role=status.role)
            db.add(row)
        row.mount_point = status.mount_point
        row.storage_path = status.storage_path
        row.is_mounted = status.mounted
        row.total_bytes = status.total_bytes
        row.used_bytes = status.used_bytes
        row.free_bytes = status.free_bytes
        row.last_checked_at = now
    db.commit()


def user_usage(db: Session, owner_id: int) -> tuple[int, int, int]:
    """(jumlah file aktif, byte file aktif, byte di trash)."""
    active_count, active_bytes = db.execute(
        select(func.count(StoredFile.id), func.coalesce(func.sum(StoredFile.size_bytes), 0)).where(
            StoredFile.owner_id == owner_id, StoredFile.is_deleted.is_(False)
        )
    ).one()
    trash_bytes = db.scalar(
        select(func.coalesce(func.sum(StoredFile.size_bytes), 0)).where(
            StoredFile.owner_id == owner_id, StoredFile.is_deleted.is_(True)
        )
    )
    return int(active_count), int(active_bytes), int(trash_bytes or 0)
