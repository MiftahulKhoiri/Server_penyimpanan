from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.core.dependencies import CurrentUser, DbSession, SettingsDep, StorageDep
from app.schemas.records import EmptyTrashResult, PurgeResult, RestoreResult, TrashItemOut, TrashList
from app.services import trash_service
from app.utils.request_info import client_info

router = APIRouter(
    prefix="/trash",
    tags=["trash"],
    responses={401: {"description": "Tidak terautentikasi"}, 404: {"description": "Item tidak ada di trash"}},
)


@router.get("", response_model=TrashList)
def list_trash(
    user: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> TrashList:
    """Isi trash milik Anda. Item dihapus permanen otomatis setelah `purge_after`."""
    items, total = trash_service.list_trash(db, user.id, page, page_size)
    return TrashList(
        items=[TrashItemOut.from_model(i) for i in items], total=total, page=page, page_size=page_size,
        retention_days=settings.TRASH_RETENTION_DAYS,
    )


@router.post("/{file_id}/restore", response_model=RestoreResult)
def restore_item(file_id: str, request: Request, user: CurrentUser, db: DbSession) -> RestoreResult:
    """Kembalikan file/folder (`file_id` = ID file atau folder) ke lokasi asal.

    Bila folder asal sudah tidak ada, item dikembalikan ke root.
    """
    item = trash_service.get_item(db, user.id, file_id)
    return RestoreResult(**trash_service.restore(db, user, item, client_info(request)))


@router.api_route("/{file_id}/delete", methods=["POST", "DELETE"], response_model=PurgeResult)
def purge_item(file_id: str, request: Request, user: CurrentUser, db: DbSession, storage: StorageDep) -> PurgeResult:
    """Hapus PERMANEN satu item dari trash beserta data fisiknya. Tidak bisa dibatalkan."""
    item = trash_service.get_item(db, user.id, file_id)
    result = trash_service.purge_item(
        db, storage, item, actor_id=user.id, client=client_info(request), reason="user_request"
    )
    return PurgeResult(**result)


@router.delete("", response_model=EmptyTrashResult)
def empty_trash(request: Request, user: CurrentUser, db: DbSession, storage: StorageDep) -> EmptyTrashResult:
    """Kosongkan seluruh trash milik Anda (permanen)."""
    return EmptyTrashResult(purged_items=trash_service.empty_trash(db, storage, user, client_info(request)))
