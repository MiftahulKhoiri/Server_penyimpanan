"""Pencatatan activity log. Data rahasia selalu disensor sebelum disimpan."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import ActivityLog

_SENSITIVE_MARKERS = ("password", "token", "secret", "authorization", "hash", "cookie")
_MAX_DETAILS_CHARS = 4000


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if any(marker in key_str.lower() for marker in _SENSITIVE_MARKERS):
                clean[key_str] = "[DISENSOR]"
            else:
                clean[key_str] = _sanitize(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def log_activity(
    db: Session,
    action: str,
    *,
    user_id: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    details: dict[str, Any] | None = None,
) -> ActivityLog:
    """Tambahkan entri log ke sesi. Pemanggil yang melakukan commit."""
    details_json = None
    if details:
        details_json = json.dumps(_sanitize(details), ensure_ascii=False)[:_MAX_DETAILS_CHARS]
    entry = ActivityLog(
        user_id=user_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        ip_address=ip_address,
        user_agent=(user_agent or None) and user_agent[:255],
        details=details_json,
    )
    db.add(entry)
    return entry
