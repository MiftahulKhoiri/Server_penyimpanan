#!/usr/bin/env python3
"""Menjalankan Server_penyimpanan dengan uvicorn (1 worker, hemat RAM)."""

from __future__ import annotations

import sys

import uvicorn
from pydantic import ValidationError

from app.core.config import get_settings


def main() -> int:
    try:
        settings = get_settings()
    except ValidationError as exc:
        print("Konfigurasi tidak valid (.env):", file=sys.stderr)
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "-"
            print(f"  {loc}: {err['msg']}", file=sys.stderr)
        return 2

    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.HOST,
        port=settings.PORT,
        workers=1,  # rate limiter in-memory & SQLite: wajib 1 worker
        proxy_headers=True,
        forwarded_allow_ips=settings.FORWARDED_ALLOW_IPS,
        log_level=settings.LOG_LEVEL.lower(),
        timeout_keep_alive=15,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
