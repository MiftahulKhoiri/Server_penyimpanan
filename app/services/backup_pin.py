"""Koordinasi antara backup dan penghapusan fisik file di SSD.

Masalah yang dicegah: backup membuat snapshot database, lalu menyalin file yang
tercatat di snapshot itu. Bila di antara keduanya pengguna menghapus permanen
sebuah file (purge trash), snapshot menunjuk ke file yang sudah hilang dan set
backup menjadi tidak konsisten.

Solusi (berlaku lintas proses, mis. server + scripts/backup_now.py):
- Backup memegang kunci BERSAMA (flock LOCK_SH) pada PRIMARY/.backup.pin selama
  snapshot database + penyalinan file.
- Penghapusan fisik mencoba kunci EKSKLUSIF tanpa menunggu. Berhasil -> file
  langsung dihapus (sambil memegang kunci, sehingga backup yang baru mulai
  menunggu sampai penghapusan selesai dan snapshot-nya tidak lagi memuat baris
  tersebut). Gagal (backup sedang berjalan) -> file DIPINDAHKAN (rename atomik,
  filesystem yang sama) ke PRIMARY/.pending-delete/ dan backup tetap bisa
  membacanya dari sana.
- Pemeliharaan berkala mengosongkan .pending-delete/ ketika tidak ada backup.

flock otomatis dilepas kernel bila proses mati (listrik padam / kill), jadi
tidak ada kunci yang "nyangkut".
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.services.storage_paths import DIR_MODE, StoragePaths, UnsafePathError

logger = logging.getLogger("server_penyimpanan.backup_pin")

PIN_NAME = ".backup.pin"
PENDING_DIR = ".pending-delete"


def _pin_path(root: Path) -> Path:
    return root / PIN_NAME


def _open_pin(root: Path) -> int:
    root.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    return os.open(_pin_path(root), os.O_RDWR | os.O_CREAT, 0o600)


@contextmanager
def backup_pin(root: Path) -> Iterator[None]:
    """Dipegang backup selama snapshot + penyalinan (menunggu penghapusan yang sedang berjalan)."""
    fd = _open_pin(root)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)  # melepas kunci


@contextmanager
def try_exclusive(root: Path) -> Iterator[bool]:
    """True bila tidak ada backup yang sedang memegang pin (kunci dipegang selama blok)."""
    fd = _open_pin(root)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise
            acquired = False
        yield acquired
    finally:
        os.close(fd)


def pending_path(root: Path, relpath: str) -> Path:
    """Lokasi file yang penghapusannya ditunda (nama datar, tidak bisa keluar dari direktori)."""
    flat = relpath.replace("/", "__")
    if not flat or flat.startswith(".") or "\x00" in flat:
        raise UnsafePathError("Path tidak valid")
    return root / PENDING_DIR / flat


def discard_files(storage: StoragePaths, relpaths: list[str]) -> int:
    """Hapus data fisik; bila backup sedang berjalan, tunda dengan memindahkannya ke .pending-delete/."""
    handled = 0
    with try_exclusive(storage.root) as free:
        for rel in relpaths:
            try:
                path = storage.resolve(rel)
                if free:
                    path.unlink(missing_ok=True)
                else:
                    target = pending_path(storage.root, rel)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
                    try:
                        os.replace(path, target)
                    except FileNotFoundError:
                        pass
                handled += 1
            except UnsafePathError:
                logger.error("Path tidak aman saat menghapus data fisik: %s", rel)
            except OSError as exc:
                logger.warning("Gagal menghapus data fisik (%s): %s", rel, exc.__class__.__name__)
    return handled


def drain_pending(root: Path) -> int:
    """Hapus file di .pending-delete/ bila tidak ada backup yang berjalan."""
    directory = root / PENDING_DIR
    if not directory.is_dir():
        return 0
    removed = 0
    with try_exclusive(root) as free:
        if not free:
            return 0
        for entry in directory.iterdir():
            try:
                entry.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                logger.warning("Gagal mengosongkan %s: %s", entry.name, exc.__class__.__name__)
    return removed
