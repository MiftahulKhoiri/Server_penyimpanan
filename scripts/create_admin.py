#!/usr/bin/env python3
"""Membuat akun administrator.

Pemakaian interaktif:
    .venv/bin/python scripts/create_admin.py --username admin
Non-interaktif (password dari stdin, tidak tersimpan di history shell):
    printf '%s' "$PASSWORD" | .venv/bin/python scripts/create_admin.py --username admin --password-stdin
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.core.security import SecurityService  # noqa: E402
from app.database.session import Database  # noqa: E402
from app.models import Role  # noqa: E402
from app.schemas.auth import normalize_email, normalize_username  # noqa: E402
from app.services import auth_service  # noqa: E402
from app.services.activity_service import log_activity  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Buat akun administrator Server_penyimpanan")
    parser.add_argument("--username", required=True)
    parser.add_argument("--email", default=None)
    parser.add_argument("--password-stdin", action="store_true", help="baca password dari stdin")
    args = parser.parse_args()

    try:
        username = normalize_username(args.username)
        email = normalize_email(args.email)
    except ValueError as exc:
        print(f"GAGAL: {exc}", file=sys.stderr)
        return 2

    if args.password_stdin:
        password = sys.stdin.read().rstrip("\n")
    else:
        password = getpass.getpass("Password admin: ")
        if password != getpass.getpass("Ulangi password: "):
            print("GAGAL: password tidak sama", file=sys.stderr)
            return 2
    if len(password) > 128:
        print("GAGAL: password maksimal 128 karakter", file=sys.stderr)
        return 2

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Konfigurasi tidak valid: {exc}", file=sys.stderr)
        return 2

    database = Database(settings)
    if not database.initialize():
        print(f"GAGAL: database tidak siap ({database.error})", file=sys.stderr)
        return 1
    security = SecurityService(settings)
    db = database.session()
    try:
        user = auth_service.create_user(
            db, settings, security, username=username, password=password, email=email,
            role=Role.ADMIN.value,
        )
        log_activity(db, "admin.user_create", user_id=user.id, target_type="user", target_id=user.id,
                     details={"username": username, "role": "admin", "source": "cli"})
        db.commit()
    except AppError as exc:
        db.rollback()
        print(f"GAGAL: {exc.message}", file=sys.stderr)
        return 1
    finally:
        db.close()
        database.dispose()
    print(f"Admin '{username}' berhasil dibuat (id={user.id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
