from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import UTCDateTime, new_uuid, utcnow


class RefreshToken(Base):
    """Satu baris = satu refresh token. Token dirotasi setiap refresh;
    semua token dari satu login berbagi `family_id` (= session id perangkat)."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_active", "user_id", "revoked_at"),
        Index("ix_refresh_tokens_family", "family_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[str] = mapped_column(String(36))
    # Hanya hash SHA-256 yang disimpan, token asli tidak pernah disimpan.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    device_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    replaced_by_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
