from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import UTCDateTime, new_uuid, utcnow


class Folder(Base):
    __tablename__ = "folders"
    __table_args__ = (
        Index("ix_folders_owner_parent", "owner_id", "parent_id", "is_deleted"),
        Index("ix_folders_owner_name", "owner_id", "name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # NULL = folder di root milik pengguna.
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
