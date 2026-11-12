from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import UTCDateTime, utcnow


class StorageDevice(Base):
    """Snapshot status perangkat penyimpanan terakhir (SSD utama / HDD backup)."""

    __tablename__ = "storage_devices"
    __table_args__ = (CheckConstraint("role IN ('primary', 'backup')", name="role_valid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)
    role: Mapped[str] = mapped_column(String(16))
    mount_point: Mapped[str] = mapped_column(String(512))
    storage_path: Mapped[str] = mapped_column(String(512))
    is_mounted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    used_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    free_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class BackupStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class BackupJob(Base):
    __tablename__ = "backup_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    triggered_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default=BackupStatus.PENDING.value, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    files_total: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    files_copied: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    files_skipped: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    files_failed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    bytes_copied: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    # Laporan JSON: daftar file gagal/checksum mismatch, dll.
    report: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
