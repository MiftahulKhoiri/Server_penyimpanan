"""Skema pemasangan SSD/HDD dari aplikasi (Admin server > Disk)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["ssd", "hdd"]

# Sama dengan validasi di scripts/storage_helper.py (helper tetap memvalidasi ulang).
MOUNT_POINT_PATTERN = r"^/(mnt|media|srv)/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
UUID_PATTERN = r"^[0-9A-Fa-f][0-9A-Fa-f-]{3,63}$"


class PartitionOut(BaseModel):
    """Partisi (atau disk tanpa partisi) yang terdeteksi di server."""

    path: str = Field(description="Perangkat, mis. /dev/sda1")
    name: str | None
    disk: str = Field(description="Nama disk induk, mis. sda")
    size_bytes: int | None
    fstype: str | None
    uuid: str | None
    label: str | None
    model: str | None
    transport: str | None = Field(description="usb, sata, nvme, ...")
    removable: bool
    mountpoint: str | None
    system: bool = Field(description="Bagian dari disk sistem (kartu SD/boot): tidak pernah bisa dipakai")
    usable: bool = Field(description="Bisa dipasang sebagai HDD")
    reason: str | None = Field(description="Kode alasan bila tidak bisa dipakai")
    reason_text: str | None
    role: Role | None = Field(description="Sedang dipakai sebagai ssd/hdd")


class RoleOut(BaseModel):
    """Status satu peran disk (SSD utama / HDD backup) menurut konfigurasi di server."""

    role: Role
    mount_point: str
    paths: dict[str, str] = Field(description="Path data di bawah mount point, mis. PRIMARY_STORAGE_PATH")
    mounted: bool
    device: str | None
    uuid: str | None
    fstype: str | None
    label: str | None
    model: str | None
    managed: bool = Field(description="Entri /etc/fstab dikelola aplikasi")
    fstab_uuid: str | None
    total_bytes: int | None
    free_bytes: int | None
    can_mount: bool = Field(description="Disk bisa dipilih/dipasang dari aplikasi (hanya HDD)")
    can_unmount: bool = Field(description="Disk bisa dilepas dari aplikasi (hanya HDD yang terpasang)")


class SetupJobOut(BaseModel):
    """Operasi disk terakhir. Pindah lokasi mount berjalan di latar dan menjalankan ulang server."""

    id: int
    op: Literal["mount", "unmount", "set_mount_point"]
    role: Role
    status: Literal["running", "succeeded", "failed"]
    detail: dict[str, Any] = {}
    steps: list[str] = []
    message: str | None = None
    error_code: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class StorageSetupOut(BaseModel):
    helper_available: bool = Field(description="false = helper belum terpasang; jalankan sudo ./scripts/update.sh")
    helper_error: str | None = None
    restart_pending: bool = Field(
        description="Konfigurasi di disk berbeda dengan yang dipakai server berjalan (server sedang/perlu restart)"
    )
    roles: list[RoleOut] = []
    partitions: list[PartitionOut] = []
    job: SetupJobOut | None = None


class MountRequest(BaseModel):
    uuid: str = Field(pattern=UUID_PATTERN, description="UUID partisi dari daftar `partitions`")


class MountPointRequest(BaseModel):
    mount_point: str = Field(
        pattern=MOUNT_POINT_PATTERN, description="Lokasi baru: /mnt/<nama>, /media/<nama>, atau /srv/<nama>"
    )


class SetupActionOut(BaseModel):
    detail: str
    restarting: bool = Field(description="true = server akan berhenti sebentar lalu jalan lagi; pantau GET /storage/setup/job")
    job: SetupJobOut


class SetupJobResponse(BaseModel):
    job: SetupJobOut | None
