"""Pemasangan SSD/HDD dari aplikasi (khusus admin).

Semua aksi dikerjakan helper root (scripts/storage_helper.py) lewat socket Unix. Batasannya:
- HDD: pilih disk & pasang, lepas, pindah lokasi mount.
- SSD: hanya pindah lokasi mount (disk yang sama). Melepas/mengganti SSD lewat SSH, karena
  database server ada di SSD; tanpa SSD tidak ada yang bisa login ke aplikasi.
Pindah lokasi mount menjalankan ulang server (±10-30 detik); pantau GET /storage/setup/job.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.core.dependencies import AdminUser, DbSession, SettingsDep
from app.core.errors import AppError, conflict, service_unavailable
from app.models import User
from app.schemas.storage_setup import (
    MountPointRequest,
    MountRequest,
    PartitionOut,
    Role,
    RoleOut,
    SetupActionOut,
    SetupJobOut,
    SetupJobResponse,
    StorageSetupOut,
)
from app.services import storage_setup
from app.services.activity_service import log_activity
from app.services.backup_service import BackupManager
from app.utils.request_info import client_info

router = APIRouter(
    prefix="/storage/setup",
    tags=["storage"],
    responses={401: {"description": "Tidak terautentikasi"}, 403: {"description": "Bukan admin"}},
)

_HELPER_MISSING = (
    "Helper pemasangan disk belum terpasang di server. Jalankan sekali di Raspberry Pi: "
    "sudo ./scripts/update.sh (atau install_pi.sh)"
)
_ACTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "Partisi tidak ditemukan"},
    409: {"description": "Bentrok: disk/lokasi sedang dipakai, backup berjalan, atau operasi lain berjalan"},
    422: {"description": "Input/disk tidak bisa dipakai (mis. disk sistem, filesystem bukan ext4)"},
    502: {"description": "Perintah sistem gagal (mount, fstab)"},
    503: {"description": "Helper belum terpasang"},
}
RolePath = Annotated[Role, Path(description="ssd = penyimpanan utama, hdd = backup")]


async def _call(settings, op: str, **params) -> dict:  # type: ignore[no-untyped-def]
    client = storage_setup.client_for(settings)
    try:
        return await run_in_threadpool(client.call, op, **params)
    except storage_setup.HelperUnavailable as exc:
        raise service_unavailable("storage_helper_unavailable", _HELPER_MISSING) from exc
    except storage_setup.HelperFailed as exc:
        raise exc.to_app_error() from exc


def _ensure_no_backup(request: Request) -> None:
    manager: BackupManager = request.app.state.backup_manager
    if manager.running:
        raise conflict("backup_running", "Backup sedang berjalan; tunggu sampai selesai")


def _log(db: Session, request: Request, admin: User, action: str, role: str, details: dict) -> None:
    client = client_info(request)
    log_activity(
        db, action, user_id=admin.id, ip_address=client.ip_address, user_agent=client.user_agent,
        target_type="storage", target_id=role, details=details,
    )
    db.commit()


@router.get("", response_model=StorageSetupOut)
async def storage_setup_status(admin: AdminUser, settings: SettingsDep) -> StorageSetupOut:
    """Disk yang terdeteksi, status SSD/HDD, dan operasi terakhir."""
    try:
        result = await run_in_threadpool(storage_setup.client_for(settings).call, "status")
    except storage_setup.HelperUnavailable:
        return StorageSetupOut(helper_available=False, helper_error=_HELPER_MISSING, restart_pending=False)
    except storage_setup.HelperFailed as exc:
        return StorageSetupOut(helper_available=True, helper_error=exc.message, restart_pending=False)
    roles = [
        RoleOut(
            **role,
            can_mount=role["role"] == "hdd",
            can_unmount=role["role"] == "hdd" and bool(role["mounted"] or role["managed"]),
        )
        for role in result.get("roles", [])
    ]
    job = result.get("job")
    return StorageSetupOut(
        helper_available=True,
        restart_pending=storage_setup.restart_pending(settings, result.get("roles", [])),
        roles=roles,
        partitions=[PartitionOut(**p) for p in result.get("partitions", [])],
        job=SetupJobOut(**job) if job else None,
    )


@router.get("/job", response_model=SetupJobResponse, responses={503: {"description": "Helper belum terpasang"}})
async def storage_setup_job(admin: AdminUser, settings: SettingsDep) -> SetupJobResponse:
    """Status operasi disk terakhir (dipakai untuk memantau pindah lokasi mount)."""
    result = await _call(settings, "job")
    job = result.get("job")
    return SetupJobResponse(job=SetupJobOut(**job) if job else None)


@router.post("/{role}/mount", response_model=SetupActionOut, responses=_ACTION_RESPONSES)
async def storage_mount(
    role: RolePath, body: MountRequest, request: Request, admin: AdminUser, db: DbSession, settings: SettingsDep
) -> SetupActionOut:
    """Pasang partisi terpilih sebagai HDD backup dan simpan ke /etc/fstab (nofail, berdasarkan UUID).

    Tidak pernah memformat. Hanya partisi ext4/xfs/btrfs/f2fs. SSD tidak bisa dipasang dari aplikasi.
    """
    if role == "ssd":
        raise AppError(422, "ssd_via_ssh", "SSD memuat database server, jadi hanya bisa dipasang lewat SSH")
    _ensure_no_backup(request)
    result = await _call(settings, "mount", role=role, uuid=body.uuid)
    job = SetupJobOut(**result["job"])
    await run_in_threadpool(_log, db, request, admin, "storage.mount", role, {"uuid": body.uuid, **job.detail})
    return SetupActionOut(detail=job.message or "Disk terpasang", restarting=False, job=job)


@router.post("/{role}/unmount", response_model=SetupActionOut, responses=_ACTION_RESPONSES)
async def storage_unmount(
    role: RolePath, request: Request, admin: AdminUser, db: DbSession, settings: SettingsDep
) -> SetupActionOut:
    """Lepas HDD dan hapus entrinya dari /etc/fstab. Data di disk tidak dihapus; aman dicabut sesudahnya."""
    if role == "ssd":
        raise AppError(422, "ssd_via_ssh", "SSD memuat database server, jadi hanya bisa dilepas lewat SSH")
    _ensure_no_backup(request)
    result = await _call(settings, "unmount", role=role)
    job = SetupJobOut(**result["job"])
    await run_in_threadpool(_log, db, request, admin, "storage.unmount", role, job.detail)
    return SetupActionOut(detail=job.message or "Disk dilepas", restarting=False, job=job)


@router.put("/{role}/mount-point", response_model=SetupActionOut, status_code=202, responses=_ACTION_RESPONSES)
async def storage_set_mount_point(
    role: RolePath, body: MountPointRequest, request: Request, admin: AdminUser, db: DbSession, settings: SettingsDep
) -> SetupActionOut:
    """Pindahkan lokasi mount SSD/HDD (disk yang sama, data tetap).

    Konfigurasi, /etc/fstab, dan izin systemd diperbarui, lalu **server dijalankan ulang**
    (±10-30 detik). Bila server gagal jalan, pengaturan lama dipulihkan otomatis.
    Pantau hasilnya di GET /storage/setup/job setelah server kembali.
    """
    _ensure_no_backup(request)
    current = settings.PRIMARY_MOUNT_POINT if role == "ssd" else settings.BACKUP_MOUNT_POINT
    # Dicatat SEBELUM helper dipanggil: sesudahnya server langsung dihentikan sementara.
    await run_in_threadpool(
        _log, db, request, admin, "storage.mount_point", role, {"from": str(current), "to": body.mount_point}
    )
    result = await _call(settings, "set_mount_point", role=role, mount_point=body.mount_point)
    job = SetupJobOut(**result["job"])
    return SetupActionOut(
        detail="Server akan berhenti sebentar untuk memindahkan disk, lalu berjalan lagi",
        restarting=True,
        job=job,
    )
