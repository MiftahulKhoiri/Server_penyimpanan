from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.types import UTCDateTime, new_uuid, utcnow


class UploadStatus(str, enum.Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    PROCESSING = "processing"  # sedang diverifikasi & difinalisasi
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Upload(Base):
    """Sesi upload bertahap (chunked/resumable)."""

    __tablename__ = "uploads"
    __table_args__ = (Index("ix_uploads_owner_status", "owner_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    folder_id: Mapped[str | None] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(127), nullable=True)
    total_size: Mapped[int] = mapped_column(BigInteger)
    chunk_size: Mapped[int] = mapped_column(Integer)
    total_chunks: Mapped[int] = mapped_column(Integer)
    expected_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=UploadStatus.PENDING.value, index=True)
    received_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    # Direktori sementara relatif terhadap PRIMARY_STORAGE_PATH.
    temp_path: Mapped[str] = mapped_column(String(512))
    file_id: Mapped[str | None] = mapped_column(
        ForeignKey("files.id", ondelete="SET NULL"), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    chunks: Mapped[list[UploadChunk]] = relationship(
        back_populates="upload", cascade="all, delete-orphan", passive_deletes=True
    )


class UploadChunk(Base):
    __tablename__ = "upload_chunks"
    __table_args__ = (UniqueConstraint("upload_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    upload_id: Mapped[str] = mapped_column(ForeignKey("uploads.id", ondelete="CASCADE"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    upload: Mapped[Upload] = relationship(back_populates="chunks")
