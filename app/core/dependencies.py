"""Dependency FastAPI: sesi database, user saat ini, dan pembatasan role."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import forbidden, service_unavailable, unauthorized
from app.core.rate_limit import LoginRateLimiter
from app.core.security import SecurityService, TokenError
from app.database.session import Database
from app.models import User
from app.services import auth_service
from app.services.storage_guard import check_location
from app.services.storage_paths import StoragePaths

_bearer = HTTPBearer(auto_error=False, description="Access token JWT dari /api/v1/auth/login")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_security(request: Request) -> SecurityService:
    return request.app.state.security


def get_login_limiter(request: Request) -> LoginRateLimiter:
    return request.app.state.login_limiter


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_db(database: Annotated[Database, Depends(get_database)]) -> Iterator[Session]:
    if not database.ensure_ready():
        if database.error == "primary_storage_unavailable":
            raise service_unavailable(
                "primary_storage_unavailable", "Penyimpanan utama (SSD) tidak tersedia"
            )
        raise service_unavailable("database_unavailable", "Database tidak tersedia")
    if not database.primary_storage_ok():
        raise service_unavailable("primary_storage_unavailable", "Penyimpanan utama (SSD) tidak tersedia")
    db = database.session()
    try:
        yield db
    finally:
        db.close()


@dataclass(frozen=True)
class AuthContext:
    user: User
    session_id: str


def get_auth_context(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
    security: Annotated[SecurityService, Depends(get_security)],
) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized()
    try:
        payload = security.decode_access_token(credentials.credentials)
    except TokenError as exc:
        message = "Access token kedaluwarsa" if exc.code == "token_expired" else "Access token tidak valid"
        raise unauthorized(exc.code, message) from exc
    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise unauthorized("token_invalid", "Access token tidak valid") from exc
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized("account_inactive", "Akun tidak ditemukan atau tidak aktif")
    session_id = str(payload["sid"])
    if not auth_service.active_session_exists(db, user.id, session_id):
        raise unauthorized("session_revoked", "Sesi sudah berakhir, silakan login ulang")
    request.state.user_id = user.id
    return AuthContext(user=user, session_id=session_id)


def get_current_user(ctx: Annotated[AuthContext, Depends(get_auth_context)]) -> User:
    return ctx.user


def get_storage(settings: Annotated[Settings, Depends(get_settings)]) -> StoragePaths:
    """Pastikan SSD ter-mount sebelum operasi baca/tulis file."""
    status = check_location(
        settings.PRIMARY_STORAGE_PATH, settings.PRIMARY_MOUNT_POINT, settings.REQUIRE_MOUNTED_STORAGE
    )
    if not status.available:
        raise service_unavailable("primary_storage_unavailable", "Penyimpanan utama (SSD) tidak tersedia")
    paths = StoragePaths(settings.PRIMARY_STORAGE_PATH)
    try:
        paths.ensure_layout()
    except OSError as exc:
        raise service_unavailable("primary_storage_unavailable", "Penyimpanan utama tidak dapat ditulis") from exc
    return paths


def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if not user.is_admin:
        raise forbidden("admin_required", "Hanya administrator yang boleh mengakses")
    return user


DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
CurrentAuth = Annotated[AuthContext, Depends(get_auth_context)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
SecurityDep = Annotated[SecurityService, Depends(get_security)]
LimiterDep = Annotated[LoginRateLimiter, Depends(get_login_limiter)]
StorageDep = Annotated[StoragePaths, Depends(get_storage)]
