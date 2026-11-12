from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.auth import OptionalEmail, Password, Username


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None
    role: str
    is_active: bool
    storage_quota_bytes: int | None
    created_at: datetime
    last_login_at: datetime | None


class UserList(BaseModel):
    items: list[UserOut]
    total: int
    page: int
    page_size: int


class AdminUserCreate(BaseModel):
    username: Username
    password: Password
    email: OptionalEmail = None
    role: Literal["admin", "user"] = "user"
    storage_quota_bytes: int | None = Field(default=None, ge=0)


class AdminUserUpdate(BaseModel):
    """Hanya field yang dikirim yang diubah. Kirim email/storage_quota_bytes = null untuk mengosongkan."""

    email: OptionalEmail = None
    role: Literal["admin", "user"] | None = None
    is_active: bool | None = None
    password: Password | None = None
    storage_quota_bytes: int | None = Field(default=None, ge=0)


class MessageResponse(BaseModel):
    detail: str
