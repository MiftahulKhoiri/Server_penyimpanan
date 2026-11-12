from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.core.dependencies import AdminUser, DbSession, SecurityDep, SettingsDep
from app.schemas.user import AdminUserCreate, AdminUserUpdate, MessageResponse, UserList, UserOut
from app.services import auth_service, user_service
from app.services.activity_service import log_activity
from app.utils.request_info import client_info

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    responses={401: {"description": "Tidak terautentikasi"}, 403: {"description": "Bukan admin"}},
)


@router.get("/users", response_model=UserList)
def list_users(
    admin: AdminUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=64)] = None,
) -> UserList:
    users, total = user_service.list_users(db, page=page, page_size=page_size, search=search)
    return UserList(
        items=[UserOut.model_validate(u) for u in users], total=total, page=page, page_size=page_size
    )


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: AdminUserCreate,
    admin: AdminUser,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    security: SecurityDep,
) -> UserOut:
    user = auth_service.create_user(
        db, settings, security,
        username=body.username, password=body.password, email=body.email,
        role=body.role, storage_quota_bytes=body.storage_quota_bytes,
    )
    client = client_info(request)
    log_activity(
        db, "admin.user_create", user_id=admin.id, ip_address=client.ip_address,
        user_agent=client.user_agent, target_type="user", target_id=user.id,
        details={"username": user.username, "role": user.role},
    )
    db.commit()
    return UserOut.model_validate(user)


@router.get("/users/{user_id}", response_model=UserOut)
def get_user(user_id: int, admin: AdminUser, db: DbSession) -> UserOut:
    return UserOut.model_validate(user_service.get_user_or_404(db, user_id))


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: AdminUserUpdate,
    admin: AdminUser,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    security: SecurityDep,
) -> UserOut:
    target = user_service.get_user_or_404(db, user_id)
    changed = user_service.update_user(db, settings, security, admin, target, body)
    if changed:
        client = client_info(request)
        log_activity(
            db, "admin.user_update", user_id=admin.id, ip_address=client.ip_address,
            user_agent=client.user_agent, target_type="user", target_id=target.id,
            details={"changed_fields": changed},
        )
    db.commit()
    return UserOut.model_validate(target)


@router.delete("/users/{user_id}", response_model=MessageResponse)
def delete_user(user_id: int, admin: AdminUser, request: Request, db: DbSession) -> MessageResponse:
    target = user_service.get_user_or_404(db, user_id)
    username = target.username
    user_service.delete_user(db, admin, target)
    client = client_info(request)
    log_activity(
        db, "admin.user_delete", user_id=admin.id, ip_address=client.ip_address,
        user_agent=client.user_agent, target_type="user", target_id=user_id,
        details={"username": username},
    )
    db.commit()
    return MessageResponse(detail="Pengguna dihapus")
