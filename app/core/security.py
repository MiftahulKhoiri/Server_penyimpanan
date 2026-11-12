"""Password hashing (Argon2id), JWT access token, dan refresh token opaque."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings


class TokenError(Exception):
    """Token tidak valid, kedaluwarsa, atau bertipe salah."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AccessToken:
    token: str
    expires_in: int


class SecurityService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._hasher = PasswordHasher(
            time_cost=settings.ARGON2_TIME_COST,
            memory_cost=settings.ARGON2_MEMORY_COST_KIB,
            parallelism=settings.ARGON2_PARALLELISM,
            hash_len=32,
            salt_len=16,
        )
        # Hash dummy dipakai saat username tidak ada, supaya waktu respons
        # login gagal tidak membocorkan apakah username terdaftar.
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(16))

    # ---------- Password ----------
    def hash_password(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify_password(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def verify_dummy(self, password: str) -> None:
        self.verify_password(self._dummy_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True

    # ---------- Access token (JWT) ----------
    def create_access_token(
        self,
        *,
        user_id: int,
        role: str,
        session_id: str,
        now: datetime | None = None,
        expires_delta: timedelta | None = None,
    ) -> AccessToken:
        now = now or datetime.now(timezone.utc)
        delta = expires_delta or timedelta(minutes=self._settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        payload: dict[str, Any] = {
            "sub": str(user_id),
            "sid": session_id,
            "role": role,
            "type": "access",
            "iss": self._settings.APP_NAME,
            "iat": int(now.timestamp()),
            "exp": int((now + delta).timestamp()),
            "jti": uuid.uuid4().hex,
        }
        token = jwt.encode(payload, self._settings.SECRET_KEY, algorithm=self._settings.JWT_ALGORITHM)
        return AccessToken(token=token, expires_in=int(delta.total_seconds()))

    def decode_access_token(self, token: str) -> dict[str, Any]:
        try:
            payload = jwt.decode(
                token,
                self._settings.SECRET_KEY,
                algorithms=[self._settings.JWT_ALGORITHM],
                issuer=self._settings.APP_NAME,
                options={"require": ["exp", "iat", "sub", "sid", "type", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("token_expired") from exc
        except jwt.PyJWTError as exc:
            raise TokenError("token_invalid") from exc
        if payload.get("type") != "access":
            raise TokenError("token_invalid")
        return payload


# ---------- Refresh token (opaque, disimpan sebagai hash) ----------
def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
