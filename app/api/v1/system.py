"""Endpoint sistem. Tahap ini: /health. /status & /statistics menyusul di TAHAP 7."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.dependencies import get_database
from app.database.session import Database

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
def health(database: Annotated[Database, Depends(get_database)]) -> JSONResponse:
    """Cek kesehatan ringan, tanpa autentikasi dan tanpa membocorkan path.

    200 bila database dan penyimpanan utama siap, 503 bila tidak.
    """
    database.ensure_ready()
    storage_ok = database.primary_storage_ok()
    db_ok = database.ready and database.ping()
    healthy = storage_ok and db_ok
    body = {
        "status": "ok" if healthy else "degraded",
        "database": "ok" if db_ok else (database.error or "unavailable"),
        "primary_storage": "ok" if storage_ok else "unavailable",
    }
    return JSONResponse(status_code=200 if healthy else 503, content=body)
