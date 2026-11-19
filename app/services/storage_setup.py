"""Klien helper root pemasangan SSD/HDD (lihat scripts/storage_helper.py).

Server tidak punya hak root; semua pemasangan disk dilakukan helper lewat socket Unix yang
hanya bisa dibuka user layanan. Satu koneksi = satu permintaan JSON = satu respons JSON.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.errors import AppError

MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# Kode error helper -> status HTTP. Kode lain (gagal mount, fstab, dsb.) menjadi 502.
_HTTP_STATUS = {
    "busy": 409,
    "mount_point_busy": 409,
    "mount_point_not_empty": 409,
    "mount_point_conflict": 409,
    "mounted_elsewhere": 409,
    "used_by_other_role": 409,
    "device_busy": 409,
    "no_change": 409,
    "not_mounted": 409,
    "partition_not_found": 404,
    "invalid_uuid": 422,
    "invalid_mount_point": 422,
    "invalid_role": 422,
    "system_disk": 422,
    "no_filesystem": 422,
    "unsupported_filesystem": 422,
    "no_uuid": 422,
    "ssd_via_ssh": 422,
}


class HelperUnavailable(Exception):
    """Socket helper tidak ada / tidak bisa dibuka (helper belum dipasang)."""


class HelperFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_app_error(self) -> AppError:
        return AppError(_HTTP_STATUS.get(self.code, 502), self.code, self.message)


class StorageHelperClient:
    def __init__(self, socket_path: Path, timeout: float = 90.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def call(self, op: str, **params: Any) -> dict[str, Any]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            try:
                sock.connect(str(self.socket_path))
            except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
                raise HelperUnavailable(str(exc)) from exc
            sock.sendall(json.dumps({"op": op, **params}).encode("utf-8") + b"\n")
            sock.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise HelperFailed("helper_bad_response", "Respons helper terlalu besar")
                chunks.append(chunk)
        except TimeoutError as exc:
            raise HelperFailed("helper_timeout", "Helper pemasangan disk tidak merespons") from exc
        except OSError as exc:  # koneksi putus di tengah jalan
            raise HelperUnavailable(str(exc)) from exc
        finally:
            sock.close()
        try:
            response = json.loads(b"".join(chunks).decode("utf-8"))
        except ValueError as exc:
            raise HelperFailed("helper_bad_response", "Respons helper tidak valid") from exc
        if not isinstance(response, dict):
            raise HelperFailed("helper_bad_response", "Respons helper tidak valid")
        if not response.get("ok"):
            raise HelperFailed(str(response.get("code") or "helper_error"), str(response.get("message") or "Helper gagal"))
        result = response.get("result")
        return result if isinstance(result, dict) else {}


def client_for(settings: Settings) -> StorageHelperClient:
    return StorageHelperClient(settings.STORAGE_HELPER_SOCKET)


def restart_pending(settings: Settings, roles: list[dict[str, Any]]) -> bool:
    """Apakah konfigurasi di disk (dibaca helper) berbeda dengan yang dipakai server ini."""
    running = {
        "ssd": (settings.PRIMARY_MOUNT_POINT, {
            "PRIMARY_STORAGE_PATH": settings.PRIMARY_STORAGE_PATH, "DATABASE_PATH": settings.DATABASE_PATH,
        }),
        "hdd": (settings.BACKUP_MOUNT_POINT, {"BACKUP_STORAGE_PATH": settings.BACKUP_STORAGE_PATH}),
    }
    for role in roles:
        expected = running.get(str(role.get("role")))
        if expected is None:
            continue
        mount_point, paths = expected
        if str(mount_point) != role.get("mount_point"):
            return True
        for key, value in (role.get("paths") or {}).items():
            if key in paths and str(paths[key]) != value:
                return True
    return False
