"""Query activity log dengan filter & pagination."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ActivityLog


def query_activity(
    db: Session,
    *,
    user_id: int | None,
    action: str | None,
    since: datetime | None,
    until: datetime | None,
    page: int,
    page_size: int,
) -> tuple[list[ActivityLog], int]:
    query = select(ActivityLog)
    if user_id is not None:
        query = query.where(ActivityLog.user_id == user_id)
    if action:
        # "auth." = semua aksi berawalan auth., selain itu cocok persis.
        if action.endswith("."):
            escaped = action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(ActivityLog.action.like(f"{escaped}%", escape="\\"))
        else:
            query = query.where(ActivityLog.action == action)
    if since is not None:
        query = query.where(ActivityLog.created_at >= since)
    if until is not None:
        query = query.where(ActivityLog.created_at < until)
    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = db.scalars(
        query.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total
