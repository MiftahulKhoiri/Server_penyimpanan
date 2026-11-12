from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from app.core.dependencies import AdminUser, CurrentUser, DbSession, SettingsDep
from app.core.errors import not_found
from app.models import BackupJob
from app.schemas.records import BackupJobList, BackupJobOut, BackupStartRequest
from app.schemas.system import DeviceOut, DeviceSummary, StorageStatusOut, UserUsage
from app.services import storage_service
from app.services.activity_service import log_activity
from app.services.backup_service import BackupManager
from app.utils.request_info import client_info

router = APIRouter(prefix="/storage", tags=["storage"], responses={401: {"description": "Tidak terautentikasi"}})


def _summary(status: storage_service.DeviceStatus) -> DeviceSummary:
    return DeviceSummary(
        available=status.available,
        total_bytes=status.total_bytes,
        free_bytes=status.free_bytes,
        used_percent=status.used_percent,
    )


@router.get("/status", response_model=StorageStatusOut)
def storage_status(user: CurrentUser, db: DbSession, settings: SettingsDep) -> StorageStatusOut:
    """Ringkasan kapasitas server + pemakaian & kuota Anda sendiri."""
    devices = storage_service.all_devices(settings)
    count, used, trash = storage_service.user_usage(db, user.id)
    quota = user.storage_quota_bytes
    return StorageStatusOut(
        primary=_summary(devices["ssd"]),
        backup=_summary(devices["hdd"]),
        usage=UserUsage(
            file_count=count,
            used_bytes=used,
            trash_bytes=trash,
            quota_bytes=quota,
            quota_free_bytes=None if quota is None else max(quota - used - trash, 0),
        ),
    )


def _device(name: str, db, settings) -> DeviceOut:  # type: ignore[no-untyped-def]
    devices = storage_service.all_devices(settings)
    storage_service.save_snapshot(db, devices)
    return DeviceOut(**devices[name].as_dict())


@router.get("/ssd", response_model=DeviceOut, responses={403: {"description": "Bukan admin"}})
def ssd_status(admin: AdminUser, db: DbSession, settings: SettingsDep) -> DeviceOut:
    """Detail SSD (penyimpanan utama): status mount, filesystem, kapasitas."""
    return _device("ssd", db, settings)


@router.get("/hdd", response_model=DeviceOut, responses={403: {"description": "Bukan admin"}})
def hdd_status(admin: AdminUser, db: DbSession, settings: SettingsDep) -> DeviceOut:
    """Detail HDD (backup). Bila tidak ter-mount: available=false, server utama tetap berjalan."""
    return _device("hdd", db, settings)


# ---------------- Backup ----------------
@router.post(
    "/backup",
    response_model=BackupJobOut,
    status_code=202,
    responses={403: {"description": "Bukan admin"}, 409: {"description": "Backup lain sedang berjalan"},
               503: {"description": "HDD tidak tersedia"}},
)
def start_backup(
    admin: AdminUser, request: Request, db: DbSession, body: BackupStartRequest | None = None,
) -> BackupJobOut:
    """Mulai backup SSD → HDD di latar belakang. Pantau lewat GET /storage/backup/{job_id}.

    File asli tidak pernah dihapus. Tiap salinan diverifikasi SHA-256.
    """
    manager: BackupManager = request.app.state.backup_manager
    job = manager.start(db, admin.id, verify_existing=bool(body and body.verify_existing))
    client = client_info(request)
    log_activity(
        db, "backup.start", user_id=admin.id, ip_address=client.ip_address, user_agent=client.user_agent,
        target_type="backup_job", target_id=job.id,
    )
    db.commit()
    return BackupJobOut.from_model(job)


@router.get("/backup", response_model=BackupJobList, responses={403: {"description": "Bukan admin"}})
def list_backups(
    admin: AdminUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> BackupJobList:
    """Riwayat job backup (terbaru dulu). Laporan lengkap ada di detail job."""
    total = db.scalar(select(func.count(BackupJob.id))) or 0
    jobs = db.scalars(
        select(BackupJob).order_by(BackupJob.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return BackupJobList(
        items=[BackupJobOut.from_model(j, with_report=False) for j in jobs], total=total, page=page, page_size=page_size
    )


@router.get("/backup/{job_id}", response_model=BackupJobOut, responses={403: {"description": "Bukan admin"}})
def backup_detail(job_id: int, admin: AdminUser, db: DbSession) -> BackupJobOut:
    """Status & laporan job backup: jumlah file disalin/dilewati/gagal, alasan kegagalan, backup database."""
    job = db.get(BackupJob, job_id)
    if job is None:
        raise not_found("backup_job_not_found", "Job backup tidak ditemukan")
    return BackupJobOut.from_model(job)
