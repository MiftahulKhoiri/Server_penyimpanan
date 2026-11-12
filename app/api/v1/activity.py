from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.dependencies import AdminUser, CurrentUser, DbSession
from app.schemas.records import ActivityList, ActivityOut
from app.services.activity_query import query_activity

router = APIRouter(tags=["activity"], responses={401: {"description": "Tidak terautentikasi"}})

ActionFilter = Annotated[str | None, Query(max_length=64, description="Aksi persis, atau awalan diakhiri titik (mis. 'file.')")]
PageQ = Annotated[int, Query(ge=1)]
PageSizeQ = Annotated[int, Query(ge=1, le=200)]


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@router.get("/activity", response_model=ActivityList)
def my_activity(
    user: CurrentUser,
    db: DbSession,
    action: ActionFilter = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 50,
) -> ActivityList:
    """Riwayat aktivitas akun Anda sendiri (login, upload, unduh, hapus, ...)."""
    rows, total = query_activity(
        db, user_id=user.id, action=action, since=_aware(since), until=_aware(until), page=page, page_size=page_size
    )
    return ActivityList(items=[ActivityOut.from_model(r) for r in rows], total=total, page=page, page_size=page_size)


@router.get("/admin/activity", response_model=ActivityList, tags=["admin"],
            responses={403: {"description": "Bukan admin"}})
def all_activity(
    admin: AdminUser,
    db: DbSession,
    user_id: int | None = None,
    action: ActionFilter = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 50,
) -> ActivityList:
    """Seluruh activity log server (termasuk aktivitas sistem dengan user_id null)."""
    rows, total = query_activity(
        db, user_id=user_id, action=action, since=_aware(since), until=_aware(until), page=page, page_size=page_size
    )
    return ActivityList(items=[ActivityOut.from_model(r) for r in rows], total=total, page=page, page_size=page_size)
