"""Skema untuk trash, backup, dan activity log."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models import ActivityLog, BackupJob, TrashItem


# ---------------- Trash ----------------
class TrashItemOut(BaseModel):
    id: str = Field(description="ID file/folder; dipakai untuk restore & hapus permanen")
    type: Literal["file", "folder"]
    name: str
    original_folder_id: str | None
    size_bytes: int
    deleted_at: datetime
    purge_after: datetime

    @classmethod
    def from_model(cls, item: TrashItem) -> "TrashItemOut":
        return cls(
            id=item.file_id or item.folder_id or "",
            type="file" if item.file_id else "folder",
            name=item.original_name,
            original_folder_id=item.original_folder_id,
            size_bytes=item.size_bytes,
            deleted_at=item.deleted_at,
            purge_after=item.purge_after,
        )


class TrashList(BaseModel):
    items: list[TrashItemOut]
    total: int
    page: int
    page_size: int
    retention_days: int


class RestoreResult(BaseModel):
    type: Literal["file", "folder"]
    id: str
    name: str = Field(description="Nama setelah restore (bisa diberi nomor bila bentrok)")
    folder_id: str | None = Field(description="Lokasi setelah restore; null = root")


class PurgeResult(BaseModel):
    type: Literal["file", "folder"]
    id: str
    files: int
    bytes: int


class EmptyTrashResult(BaseModel):
    purged_items: int


# ---------------- Backup ----------------
class BackupStartRequest(BaseModel):
    verify_existing: bool = Field(
        default=False, description="Hitung ulang SHA-256 salinan yang sudah ada di HDD (lebih lambat)"
    )


class BackupJobOut(BaseModel):
    id: int
    status: str
    triggered_by_id: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    files_total: int
    files_copied: int
    files_skipped: int
    files_failed: int
    bytes_copied: int
    error_message: str | None
    report: dict[str, Any] | None = None

    @classmethod
    def from_model(cls, job: BackupJob, *, with_report: bool = True) -> "BackupJobOut":
        report = None
        if with_report and job.report:
            try:
                report = json.loads(job.report)
            except ValueError:
                report = None
        return cls(
            id=job.id, status=job.status, triggered_by_id=job.triggered_by_id, created_at=job.created_at,
            started_at=job.started_at, finished_at=job.finished_at, files_total=job.files_total,
            files_copied=job.files_copied, files_skipped=job.files_skipped, files_failed=job.files_failed,
            bytes_copied=job.bytes_copied, error_message=job.error_message, report=report,
        )


class BackupJobList(BaseModel):
    items: list[BackupJobOut]
    total: int
    page: int
    page_size: int


# ---------------- Activity ----------------
class ActivityOut(BaseModel):
    id: int
    user_id: int | None
    action: str
    target_type: str | None
    target_id: str | None
    ip_address: str | None
    user_agent: str | None
    details: dict[str, Any] | None
    created_at: datetime

    @classmethod
    def from_model(cls, row: ActivityLog) -> "ActivityOut":
        try:
            details = json.loads(row.details) if row.details else None
        except ValueError:
            details = None
        return cls(
            id=row.id, user_id=row.user_id, action=row.action, target_type=row.target_type,
            target_id=row.target_id, ip_address=row.ip_address, user_agent=row.user_agent,
            details=details, created_at=row.created_at,
        )


class ActivityList(BaseModel):
    items: list[ActivityOut]
    total: int
    page: int
    page_size: int
