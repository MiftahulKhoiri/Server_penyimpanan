"""Logika autentikasi: registrasi, login, sesi perangkat, refresh token rotation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, conflict, unauthorized, unprocessable
from app.core.security import SecurityService, generate_refresh_token, hash_token
from app.database.types import new_uuid, utcnow
from app.models import RefreshToken, Role, User
from app.utils.request_info import ClientInfo


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    session_id: str


def validate_password_policy(settings: Settings, password: str, username: str | None = None) -> None:
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        raise unprocessable(
            "weak_password", f"Password minimal {settings.PASSWORD_MIN_LENGTH} karakter"
        )
    if password.strip() != password or not password.strip():
        raise unprocessable("weak_password", "Password tidak boleh diawali/diakhiri spasi")
    if username and password.lower() == username.lower():
        raise unprocessable("weak_password", "Password tidak boleh sama dengan username")


def create_user(
    db: Session,
    settings: Settings,
    security: SecurityService,
    *,
    username: str,
    password: str,
    email: str | None = None,
    role: str = Role.USER.value,
    storage_quota_bytes: int | None = None,
) -> User:
    validate_password_policy(settings, password, username)
    if db.scalar(select(User.id).where(User.username == username)) is not None:
        raise conflict("username_taken", "Username sudah dipakai")
    if email and db.scalar(select(User.id).where(User.email == email)) is not None:
        raise conflict("email_taken", "Email sudah dipakai")
    user = User(
        username=username,
        email=email,
        password_hash=security.hash_password(password),
        role=role,
        storage_quota_bytes=storage_quota_bytes,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:  # balapan dua registrasi bersamaan
        db.rollback()
        raise conflict("user_exists", "Username atau email sudah dipakai") from exc
    return user


def authenticate(db: Session, security: SecurityService, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        security.verify_dummy(password)
        return None
    if not security.verify_password(user.password_hash, password):
        return None
    if not user.is_active:
        return None
    if security.needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(password)
    return user


def _issue(
    db: Session,
    settings: Settings,
    security: SecurityService,
    user: User,
    *,
    family_id: str,
    client: ClientInfo,
    device_name: str | None,
) -> tuple[IssuedTokens, RefreshToken]:
    now = utcnow()
    raw_refresh = generate_refresh_token()
    refresh_ttl = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    row = RefreshToken(
        id=new_uuid(),
        user_id=user.id,
        family_id=family_id,
        token_hash=hash_token(raw_refresh),
        device_name=device_name,
        user_agent=client.user_agent,
        ip_address=client.ip_address,
        created_at=now,
        expires_at=now + refresh_ttl,
        last_used_at=now,
    )
    db.add(row)
    access = security.create_access_token(user_id=user.id, role=user.role, session_id=family_id, now=now)
    tokens = IssuedTokens(
        access_token=access.token,
        refresh_token=raw_refresh,
        expires_in=access.expires_in,
        refresh_expires_in=int(refresh_ttl.total_seconds()),
        session_id=family_id,
    )
    return tokens, row


def start_session(
    db: Session,
    settings: Settings,
    security: SecurityService,
    user: User,
    client: ClientInfo,
    device_name: str | None,
) -> IssuedTokens:
    user.last_login_at = utcnow()
    tokens, _ = _issue(
        db, settings, security, user, family_id=new_uuid(), client=client, device_name=device_name
    )
    return tokens


def revoke_family(db: Session, family_id: str, reason: str) -> int:
    result = db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )
    return result.rowcount or 0


def revoke_all_for_user(db: Session, user_id: int, reason: str) -> int:
    result = db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )
    return result.rowcount or 0


class RefreshReuseDetected(AppError):
    pass


def rotate_refresh_token(
    db: Session,
    settings: Settings,
    security: SecurityService,
    raw_refresh: str,
    client: ClientInfo,
) -> IssuedTokens:
    """Tukar refresh token lama dengan pasangan token baru (rotation).

    Jika token yang sudah dirotasi/dicabut dipakai lagi, seluruh sesi
    (family) dicabut karena kemungkinan token dicuri.
    """
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_refresh)))
    if row is None:
        raise unauthorized("refresh_token_invalid", "Refresh token tidak valid")
    now = utcnow()
    if row.revoked_at is not None:
        if row.revoked_reason == "rotated":
            revoke_family(db, row.family_id, "reuse_detected")
            db.commit()
            raise RefreshReuseDetected(
                401, "refresh_token_reused", "Refresh token sudah dipakai; sesi dicabut demi keamanan",
                {"WWW-Authenticate": "Bearer"},
            )
        raise unauthorized("session_revoked", "Sesi sudah berakhir, silakan login ulang")
    if row.expires_at <= now:
        raise unauthorized("refresh_token_expired", "Refresh token kedaluwarsa, silakan login ulang")

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        revoke_family(db, row.family_id, "user_inactive")
        db.commit()
        raise unauthorized("account_inactive", "Akun tidak aktif")

    tokens, new_row = _issue(
        db, settings, security, user, family_id=row.family_id, client=client, device_name=row.device_name
    )
    # Sesi tetap memakai masa berlaku awal (tidak diperpanjang tanpa batas):
    new_row.expires_at = row.expires_at
    tokens = IssuedTokens(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        refresh_expires_in=max(int((row.expires_at - now).total_seconds()), 0),
        session_id=tokens.session_id,
    )
    row.revoked_at = now
    row.revoked_reason = "rotated"
    row.replaced_by_id = new_row.id
    return tokens


def active_session_exists(db: Session, user_id: int, family_id: str) -> bool:
    found = db.scalar(
        select(RefreshToken.id)
        .where(
            RefreshToken.family_id == family_id,
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > utcnow(),
        )
        .limit(1)
    )
    return found is not None


def list_active_sessions(db: Session, user_id: int) -> list[RefreshToken]:
    """Satu baris per sesi perangkat (token aktif terbaru di tiap family)."""
    rows = db.scalars(
        select(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > utcnow(),
        )
        .order_by(RefreshToken.created_at.desc())
    ).all()
    seen: set[str] = set()
    sessions: list[RefreshToken] = []
    for row in rows:
        if row.family_id not in seen:
            seen.add(row.family_id)
            sessions.append(row)
    return sessions


def first_session_started(db: Session, family_id: str):  # type: ignore[no-untyped-def]
    return db.scalar(select(func.min(RefreshToken.created_at)).where(RefreshToken.family_id == family_id))


def change_password(
    db: Session,
    settings: Settings,
    security: SecurityService,
    user: User,
    current_password: str,
    new_password: str,
) -> None:
    if not security.verify_password(user.password_hash, current_password):
        raise AppError(400, "wrong_password", "Password saat ini salah")
    if current_password == new_password:
        raise unprocessable("same_password", "Password baru harus berbeda dari password lama")
    validate_password_policy(settings, new_password, user.username)
    user.password_hash = security.hash_password(new_password)
    user.password_changed_at = utcnow()
    revoke_all_for_user(db, user.id, "password_changed")
