from __future__ import annotations

import re
from datetime import datetime

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,}$")


def normalize_username(value: str) -> str:
    value = value.strip()
    if not USERNAME_RE.fullmatch(value):
        raise ValueError("Username 3-32 karakter: huruf, angka, titik, garis bawah, atau strip")
    return value.lower()


def normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    if not value:
        return None
    if len(value) > 254 or not EMAIL_RE.fullmatch(value):
        raise ValueError("Format email tidak valid")
    return value


Username = Annotated[str, Field(max_length=32), AfterValidator(normalize_username)]
OptionalEmail = Annotated[str | None, Field(default=None, max_length=254), AfterValidator(normalize_email)]
# Batas atas panjang password mencegah DoS lewat hashing input raksasa.
Password = Annotated[str, Field(min_length=8, max_length=128)]


class RegisterRequest(BaseModel):
    username: Username
    password: Password
    email: OptionalEmail = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    device_name: str | None = Field(default=None, max_length=100)

    @field_validator("username")
    @classmethod
    def _strip_lower(cls, value: str) -> str:
        return value.strip().lower()


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=200)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: Password


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Masa berlaku access token (detik)")
    refresh_expires_in: int = Field(description="Masa berlaku refresh token (detik)")
    session_id: str


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    device_name: str | None
    ip_address: str | None
    user_agent: str | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    current: bool = False
