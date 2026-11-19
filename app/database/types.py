"""Tipe kolom bantu: semua timestamp disimpan dan dibaca sebagai UTC."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator[datetime]):
    """Simpan sebagai UTC naive di SQLite, kembalikan sebagai datetime aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Datetime tanpa timezone tidak diizinkan; gunakan UTC aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)
