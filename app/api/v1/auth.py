from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.core.dependencies import (
    CurrentAuth,
    CurrentUser,
    DbSession,
    LimiterDep,
    SecurityDep,
    SettingsDep,
)
from app.core.errors import AppError, forbidden, not_found, unauthorized
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    SessionOut,
    TokenResponse,
)
from app.schemas.user import MessageResponse, UserOut
from app.services import auth_service
from app.services.activity_service import log_activity
from app.utils.request_info import client_info

router = APIRouter(prefix="/auth", tags=["auth"])

_AUTH_ERRORS = {401: {"description": "Tidak terautentikasi"}}


def _token_response(tokens: auth_service.IssuedTokens) -> TokenResponse:
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        refresh_expires_in=tokens.refresh_expires_in,
        session_id=tokens.session_id,
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(
    body: RegisterRequest,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    security: SecurityDep,
) -> UserOut:
    """Registrasi akun baru (hanya jika ALLOW_REGISTRATION=true)."""
    if not settings.ALLOW_REGISTRATION:
        raise forbidden("registration_disabled", "Registrasi dinonaktifkan; hubungi administrator")
    client = client_info(request)
    user = auth_service.create_user(
        db, settings, security, username=body.username, password=body.password, email=body.email
    )
    log_activity(
        db, "auth.register", user_id=user.id, ip_address=client.ip_address,
        user_agent=client.user_agent, target_type="user", target_id=user.id,
    )
    db.commit()
    return UserOut.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={401: {"description": "Username/password salah"}, 429: {"description": "Terlalu banyak percobaan"}},
)
def login(
    body: LoginRequest,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    security: SecurityDep,
    limiter: LimiterDep,
) -> TokenResponse:
    client = client_info(request)
    keys = limiter.keys(client.ip_address, body.username)
    retry = limiter.retry_after(keys)
    if retry is not None:
        raise AppError(
            429, "too_many_attempts", "Terlalu banyak percobaan login, coba lagi nanti",
            {"Retry-After": str(retry)},
        )

    user = auth_service.authenticate(db, security, body.username, body.password)
    if user is None:
        limiter.register_failure(keys)
        log_activity(
            db, "auth.login_failed", ip_address=client.ip_address, user_agent=client.user_agent,
            details={"username": body.username},
        )
        db.commit()
        raise unauthorized("invalid_credentials", "Username atau password salah")

    limiter.reset(keys[1:])  # reset penghitung username; penghitung IP meluruh sendiri
    tokens = auth_service.start_session(db, settings, security, user, client, body.device_name)
    log_activity(
        db, "auth.login", user_id=user.id, ip_address=client.ip_address, user_agent=client.user_agent,
        target_type="session", target_id=tokens.session_id, details={"device_name": body.device_name},
    )
    db.commit()
    return _token_response(tokens)


@router.post("/refresh", response_model=TokenResponse, responses=_AUTH_ERRORS)
def refresh(
    body: RefreshRequest,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    security: SecurityDep,
) -> TokenResponse:
    """Tukar refresh token dengan pasangan token baru. Refresh token lama langsung tidak berlaku."""
    tokens = auth_service.rotate_refresh_token(db, settings, security, body.refresh_token, client_info(request))
    db.commit()
    return _token_response(tokens)


@router.post("/logout", response_model=MessageResponse, responses=_AUTH_ERRORS)
def logout(ctx: CurrentAuth, request: Request, db: DbSession) -> MessageResponse:
    """Akhiri sesi perangkat ini (access & refresh token dicabut)."""
    client = client_info(request)
    auth_service.revoke_family(db, ctx.session_id, "logout")
    log_activity(
        db, "auth.logout", user_id=ctx.user.id, ip_address=client.ip_address,
        user_agent=client.user_agent, target_type="session", target_id=ctx.session_id,
    )
    db.commit()
    return MessageResponse(detail="Berhasil logout")


@router.post("/logout-all", response_model=MessageResponse, responses=_AUTH_ERRORS)
def logout_all(ctx: CurrentAuth, request: Request, db: DbSession) -> MessageResponse:
    """Akhiri semua sesi di semua perangkat."""
    client = client_info(request)
    count = auth_service.revoke_all_for_user(db, ctx.user.id, "logout_all")
    log_activity(
        db, "auth.logout_all", user_id=ctx.user.id, ip_address=client.ip_address,
        user_agent=client.user_agent, details={"revoked_tokens": count},
    )
    db.commit()
    return MessageResponse(detail="Semua sesi telah diakhiri")


@router.get("/me", response_model=UserOut, responses=_AUTH_ERRORS)
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/change-password", response_model=TokenResponse, responses=_AUTH_ERRORS)
def change_password(
    body: ChangePasswordRequest,
    ctx: CurrentAuth,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    security: SecurityDep,
) -> TokenResponse:
    """Ganti password. Semua sesi lain dicabut; perangkat ini menerima token baru."""
    client = client_info(request)
    current_device = None
    for session in auth_service.list_active_sessions(db, ctx.user.id):
        if session.family_id == ctx.session_id:
            current_device = session.device_name
    auth_service.change_password(db, settings, security, ctx.user, body.current_password, body.new_password)
    tokens = auth_service.start_session(db, settings, security, ctx.user, client, current_device)
    log_activity(
        db, "auth.password_change", user_id=ctx.user.id, ip_address=client.ip_address,
        user_agent=client.user_agent,
    )
    db.commit()
    return _token_response(tokens)


@router.get("/sessions", response_model=list[SessionOut], responses=_AUTH_ERRORS)
def list_sessions(ctx: CurrentAuth, db: DbSession) -> list[SessionOut]:
    """Daftar sesi perangkat yang masih aktif."""
    result = []
    for row in auth_service.list_active_sessions(db, ctx.user.id):
        item = SessionOut(
            id=row.family_id,
            device_name=row.device_name,
            ip_address=row.ip_address,
            user_agent=row.user_agent,
            created_at=auth_service.first_session_started(db, row.family_id) or row.created_at,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            current=row.family_id == ctx.session_id,
        )
        result.append(item)
    return result


@router.delete("/sessions/{session_id}", response_model=MessageResponse, responses=_AUTH_ERRORS)
def revoke_session(session_id: str, ctx: CurrentAuth, request: Request, db: DbSession) -> MessageResponse:
    """Cabut satu sesi perangkat milik sendiri."""
    if not auth_service.active_session_exists(db, ctx.user.id, session_id):
        raise not_found("session_not_found", "Sesi tidak ditemukan")
    client = client_info(request)
    auth_service.revoke_family(db, session_id, "revoked_by_user")
    log_activity(
        db, "auth.session_revoke", user_id=ctx.user.id, ip_address=client.ip_address,
        user_agent=client.user_agent, target_type="session", target_id=session_id,
    )
    db.commit()
    return MessageResponse(detail="Sesi dicabut")
