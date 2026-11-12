"""Error aplikasi dan handler global yang tidak membocorkan informasi sensitif.

Format respons error seragam untuk aplikasi Android:
    {"detail": "<pesan>", "code": "<kode_mesin>"}
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("server_penyimpanan.errors")


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def unauthorized(code: str = "not_authenticated", message: str = "Autentikasi diperlukan") -> AppError:
    return AppError(status.HTTP_401_UNAUTHORIZED, code, message, {"WWW-Authenticate": "Bearer"})


def forbidden(code: str = "forbidden", message: str = "Akses ditolak") -> AppError:
    return AppError(status.HTTP_403_FORBIDDEN, code, message)


def not_found(code: str = "not_found", message: str = "Data tidak ditemukan") -> AppError:
    return AppError(status.HTTP_404_NOT_FOUND, code, message)


def conflict(code: str, message: str) -> AppError:
    return AppError(status.HTTP_409_CONFLICT, code, message)


def bad_request(code: str, message: str) -> AppError:
    return AppError(status.HTTP_400_BAD_REQUEST, code, message)


def unprocessable(code: str, message: str) -> AppError:
    return AppError(422, code, message)


def service_unavailable(code: str, message: str) -> AppError:
    return AppError(status.HTTP_503_SERVICE_UNAVAILABLE, code, message, {"Retry-After": "30"})


def _error_body(code: str, message: object) -> dict[str, object]:
    return {"detail": message, "code": code}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message),
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(code, exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field "input" dan "ctx" sengaja dibuang: bisa berisi password/token.
        errors = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"detail": "Input tidak valid", "code": "validation_error", "errors": errors},
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error("Database error: %s", exc.__class__.__name__, exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_error_body("database_error", "Database sedang bermasalah, coba lagi nanti"),
            headers={"Retry-After": "30"},
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error: %s", exc.__class__.__name__, exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("internal_error", "Terjadi kesalahan pada server"),
        )
