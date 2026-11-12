from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.core.dependencies import CurrentUser, DbSession, SettingsDep
from app.schemas.files import (
    BreadcrumbItem,
    FolderCreate,
    FolderDetail,
    FolderMoveRequest,
    FolderOut,
    RenameRequest,
)
from app.schemas.user import MessageResponse
from app.services import folder_service
from app.services.file_service import get_owned_folder
from app.utils.request_info import client_info

router = APIRouter(
    prefix="/folders",
    tags=["folders"],
    responses={401: {"description": "Tidak terautentikasi"}, 404: {"description": "Folder tidak ditemukan"}},
)


@router.post("", response_model=FolderOut, status_code=status.HTTP_201_CREATED,
             responses={409: {"description": "Nama bentrok"}})
def create_folder(body: FolderCreate, request: Request, user: CurrentUser, db: DbSession) -> FolderOut:
    folder = folder_service.create_folder(db, user, body.name, body.parent_id, client_info(request))
    return FolderOut.model_validate(folder)


@router.get("", response_model=list[FolderOut])
def list_folders(
    user: CurrentUser,
    db: DbSession,
    parent_id: Annotated[str | None, Query(max_length=36, description="Kosong = root")] = None,
) -> list[FolderOut]:
    return [FolderOut.model_validate(f) for f in folder_service.list_folders(db, user.id, parent_id)]


@router.get("/{folder_id}", response_model=FolderDetail)
def get_folder(folder_id: str, user: CurrentUser, db: DbSession) -> FolderDetail:
    folder = get_owned_folder(db, user.id, folder_id)
    count, total = folder_service.folder_stats(db, user.id, folder.id)
    base = FolderOut.model_validate(folder)
    return FolderDetail(
        **base.model_dump(),
        path=[BreadcrumbItem(id=f.id, name=f.name) for f in folder_service.ancestors(db, folder)],
        folders=[FolderOut.model_validate(f) for f in folder_service.list_folders(db, user.id, folder.id)],
        file_count=count,
        total_size_bytes=total,
    )


@router.patch("/{folder_id}/rename", response_model=FolderOut, responses={409: {"description": "Nama bentrok"}})
def rename_folder(folder_id: str, body: RenameRequest, request: Request, user: CurrentUser, db: DbSession) -> FolderOut:
    folder = get_owned_folder(db, user.id, folder_id)
    return FolderOut.model_validate(folder_service.rename_folder(db, user, folder, body.name, client_info(request)))


@router.post("/{folder_id}/move", response_model=FolderOut,
             responses={409: {"description": "Nama bentrok / siklus folder"}})
def move_folder(folder_id: str, body: FolderMoveRequest, request: Request, user: CurrentUser, db: DbSession) -> FolderOut:
    folder = get_owned_folder(db, user.id, folder_id)
    return FolderOut.model_validate(folder_service.move_folder(db, user, folder, body.parent_id, client_info(request)))


@router.delete("/{folder_id}", response_model=MessageResponse)
def delete_folder(folder_id: str, request: Request, user: CurrentUser, db: DbSession, settings: SettingsDep) -> MessageResponse:
    """Pindahkan folder beserta seluruh isinya ke trash (soft delete)."""
    folder = get_owned_folder(db, user.id, folder_id)
    folder_service.soft_delete_folder(db, settings, user, folder, client_info(request))
    return MessageResponse(detail="Folder dipindahkan ke trash")
