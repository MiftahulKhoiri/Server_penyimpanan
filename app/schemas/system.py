from __future__ import annotations

from pydantic import BaseModel


class DeviceOut(BaseModel):
    """Detail perangkat penyimpanan (khusus admin)."""

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


class DeviceSummary(BaseModel):
    """Ringkasan perangkat untuk semua pengguna (tanpa path/device)."""

    available: bool
    total_bytes: int | None
    free_bytes: int | None
    used_percent: float | None


class UserUsage(BaseModel):
    file_count: int
    used_bytes: int
    trash_bytes: int
    quota_bytes: int | None
    quota_free_bytes: int | None


class StorageStatusOut(BaseModel):
    primary: DeviceSummary
    backup: DeviceSummary
    usage: UserUsage


class CpuInfo(BaseModel):
    percent: float
    count: int
    load_average: list[float] | None
    temperature_c: float | None


class MemoryInfo(BaseModel):
    total_bytes: int
    available_bytes: int
    used_percent: float
    swap_total_bytes: int
    swap_used_bytes: int
    process_rss_bytes: int


class SystemStatusOut(BaseModel):
    status: str
    version: str
    python: str
    platform: str
    server_uptime_seconds: int
    system_uptime_seconds: int
    cpu: CpuInfo
    memory: MemoryInfo
    database: str
    storage: dict[str, DeviceOut]
    media_tools: dict[str, bool]


class CountSize(BaseModel):
    count: int
    total_bytes: int


class FileStats(CountSize):
    by_media_type: dict[str, CountSize]


class StatisticsOut(BaseModel):
    users: dict[str, int]
    files: FileStats
    folders: int
    trash: dict[str, int]
    active_uploads: int
    last_24h: dict[str, int]
