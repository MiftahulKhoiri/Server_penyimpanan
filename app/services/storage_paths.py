"""Tata letak direktori di penyimpanan utama dan resolusi path yang aman.

Struktur di bawah PRIMARY_STORAGE_PATH:
    files/<user_id>/<2 huruf awal uuid>/<uuid>   file final
    .tmp/uploads/<upload_id>/data.part          upload chunked yang berjalan
    .tmp/simple/<uuid>.part                     upload sekali kirim yang berjalan
    .tmp/copy/<uuid>.part                       salinan file yang sedang dibuat
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DIR_MODE = 0o700


class UnsafePathError(Exception):
    pass


class StoragePaths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.tmp = root / ".tmp"
        self.uploads_tmp = self.tmp / "uploads"
        self.simple_tmp = self.tmp / "simple"
        self.copy_tmp = self.tmp / "copy"

    def ensure_layout(self) -> None:
        for path in (self.root, self.root / "files", self.uploads_tmp, self.simple_tmp, self.copy_tmp):
            path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)

    @staticmethod
    def file_relpath(owner_id: int, file_id: str) -> str:
        if not _UUID_RE.fullmatch(file_id):
            raise UnsafePathError("file_id bukan UUID")
        return f"files/{int(owner_id)}/{file_id[:2]}/{file_id}"

    def resolve(self, relpath: str) -> Path:
        """Ubah path relatif dari DB menjadi path absolut, dijamin tetap di dalam root."""
        if not relpath or relpath.startswith("/") or "\x00" in relpath:
            raise UnsafePathError("Path tidak valid")
        root = self.root.resolve()
        target = (root / relpath).resolve()
        if target == root or not target.is_relative_to(root):
            raise UnsafePathError("Path keluar dari direktori penyimpanan")
        return target

    def upload_dir(self, upload_id: str) -> Path:
        if not _UUID_RE.fullmatch(upload_id):
            raise UnsafePathError("upload_id bukan UUID")
        return self.uploads_tmp / upload_id

    def upload_relpath(self, upload_id: str) -> str:
        self.upload_dir(upload_id)
        return f".tmp/uploads/{upload_id}/data.part"

    def new_temp_file(self, kind: str = "simple") -> Path:
        base = self.copy_tmp if kind == "copy" else self.simple_tmp
        base.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        return base / f"{uuid.uuid4()}.part"


def fsync_dir(path: Path) -> None:
    """Pastikan entri direktori (hasil rename) tersimpan permanen."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
