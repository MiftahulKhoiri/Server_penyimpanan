from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import UTCDateTime, new_uuid, utcnow


class MediaType(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    OTHER = "other"


class StoredFile(Base):
    __tablename__ = "files"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        CheckConstraint(
            "media_type IN ('image', 'video', 'audio', 'document', 'other')", name="media_type_valid"
        ),
        Index("ix_files_owner_folder", "owner_id", "folder_id", "is_deleted"),
        Index("ix_files_owner_name", "owner_id", "name"),
        Index("ix_files_owner_sha256", "owner_id", "sha256"),
        Index("ix_files_owner_media_created", "owner_id", "media_type", "created_at"),
        Index("ix_files_owner_created", "owner_id", "created_at"),
        Index("ix_files_owner_size", "owner_id", "size_bytes"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    folder_id: Mapped[str | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )
    # Nama tampilan dari pengguna. Path fisik TIDAK pernah memakai nama ini.
    name: Mapped[str] = mapped_column(String(255))
    extension: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(127))
    media_type: Mapped[str] = mapped_column(String(16), default=MediaType.OTHER.value)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    # Path relatif terhadap PRIMARY_STORAGE_PATH, dibangkitkan server (UUID).
    storage_path: Mapped[str] = mapped_column(String(512), unique=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    backed_up_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
