"""Pemeriksaan status mount SSD/HDD sebelum operasi penyimpanan.

Tujuan utama: jangan pernah menulis ke direktori mount kosong di root
filesystem (kartu SD) ketika SSD/HDD sebenarnya tidak terpasang.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LocationStatus:
    available: bool
    reason: str | None = None  # not_mounted | not_under_mount | not_writable


def check_location(path: Path, mount_point: Path, require_mounted: bool) -> LocationStatus:
    """Periksa apakah `path` aman dipakai untuk menulis.

    - require_mounted=True: mount_point harus benar-benar mount point dan path
      harus berada di bawahnya.
    - Direktori terdekat yang sudah ada harus bisa ditulis.
    """
    if require_mounted:
        if not os.path.ismount(mount_point):
            return LocationStatus(False, "not_mounted")
        if not path.is_relative_to(mount_point):
            return LocationStatus(False, "not_under_mount")

    # Cari direktori terdekat yang sudah ada (path bisa berupa file, mis. DB).
    existing = path
    while not existing.is_dir():
        if existing.parent == existing:
            break
        existing = existing.parent
    if not os.access(existing, os.W_OK | os.X_OK):
        return LocationStatus(False, "not_writable")
    return LocationStatus(True)
