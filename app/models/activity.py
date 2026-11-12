from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import UTCDateTime, utcnow


class ActivityLog(Base):
    __tablename__ = "activity_logs"
    __table_args__ = (Index("ix_activity_logs_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # JSON yang sudah disanitasi (tanpa password/token/secret).
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class TrashItem(Base):
    """Item di recycle bin. Tepat satu dari file_id / folder_id terisi."""

    __tablename__ = "trash"
    __table_args__ = (
        CheckConstraint(
            "(file_id IS NOT NULL AND folder_id IS NULL) OR (file_id IS NULL AND folder_id IS NOT NULL)",
            name="one_target",
        ),
        Index("ix_trash_owner_deleted", "owner_id", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    file_id: Mapped[str | None] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), nullable=True, unique=True
    )
    folder_id: Mapped[str | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True, unique=True
    )
    original_name: Mapped[str] = mapped_column(String(255))
    # Tanpa FK: folder asal bisa saja sudah dihapus permanen saat restore.
    original_folder_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    deleted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    purge_after: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
