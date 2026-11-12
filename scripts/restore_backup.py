#!/usr/bin/env python3
"""Restore dari backup di HDD ke SSD.

Secara default hanya SIMULASI (dry-run): menampilkan apa yang akan dilakukan.
Tambahkan --apply untuk benar-benar menjalankan. Hentikan server dulu:
    sudo systemctl stop server-penyimpanan

Perintah:
    --list                        daftar salinan database di HDD
    --database latest|NAMA_FILE   kembalikan database dari HDD (DB saat ini disimpan
                                  dulu sebagai server.db.before-restore-<waktu>.bak)
    --files                       kembalikan file yang HILANG / RUSAK di SSD dari HDD.
                                  Setiap salinan diverifikasi SHA-256 sebelum dipakai.
                                  File di SSD yang masih benar tidak pernah disentuh.
      --verify                    juga hitung SHA-256 file SSD yang ada (deteksi rusak; lebih lambat)

Tidak ada file yang dihapus oleh script ini.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings, get_settings  # noqa: E402
from app.services.storage_guard import check_location  # noqa: E402
from app.services.storage_paths import DIR_MODE, StoragePaths, UnsafePathError, fsync_dir  # noqa: E402

BLOCK = 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def service_running(name: str = "server-penyimpanan") -> bool:
    if not shutil.which("systemctl"):
        return False
    result = subprocess.run(["systemctl", "is-active", "--quiet", name], check=False)
    return result.returncode == 0


# ---------------- Database ----------------
def list_db_backups(settings: Settings) -> list[Path]:
    directory = settings.BACKUP_STORAGE_PATH / "database"
    return sorted(directory.glob("server-*.db")) if directory.is_dir() else []


def integrity_ok(path: Path) -> bool:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            con.close()
    except sqlite3.Error:
        return False


def restore_database(settings: Settings, choice: str, *, apply: bool, stamp: str) -> Path:
    backups = list_db_backups(settings)
    if not backups:
        raise SystemExit("GAGAL: tidak ada salinan database di HDD")
    source = backups[-1] if choice == "latest" else settings.BACKUP_STORAGE_PATH / "database" / Path(choice).name
    if not source.is_file():
        raise SystemExit(f"GAGAL: {source.name} tidak ditemukan")
    if not integrity_ok(source):
        raise SystemExit(f"GAGAL: salinan {source.name} rusak (integrity_check gagal)")
    target = settings.DATABASE_PATH
    saved = target.with_name(f"{target.name}.before-restore-{stamp}.bak")
    print(f"Database: {source} -> {target}")
    if target.exists():
        print(f"  DB saat ini disimpan sebagai {saved.name}")
    if not apply:
        return source
    target.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    if target.exists():
        current = sqlite3.connect(str(target))
        try:
            current.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            keep = sqlite3.connect(str(saved))
            try:
                current.backup(keep)
            finally:
                keep.close()
        finally:
            current.close()
    temp = target.with_name(target.name + ".restore-tmp")
    shutil.copyfile(source, temp)
    with open(temp, "rb+") as fh:
        os.fsync(fh.fileno())
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    os.replace(temp, target)
    fsync_dir(target.parent)
    print("  database dipulihkan")
    return source


# ---------------- File ----------------
@dataclass
class FileRestoreReport:
    checked: int = 0
    ok: int = 0
    restored: list[str] = field(default_factory=list)
    unrecoverable: list[tuple[str, str]] = field(default_factory=list)


def restore_files(settings: Settings, *, apply: bool, verify: bool) -> FileRestoreReport:
    primary = StoragePaths(settings.PRIMARY_STORAGE_PATH)
    backup = StoragePaths(settings.BACKUP_STORAGE_PATH)
    con = sqlite3.connect(f"file:{settings.DATABASE_PATH}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, storage_path, size_bytes, sha256 FROM files ORDER BY id").fetchall()
    finally:
        con.close()
    report = FileRestoreReport()
    for file_id, relpath, size, sha in rows:
        report.checked += 1
        try:
            src_path = primary.resolve(relpath)
            bak_path = backup.resolve(relpath)
        except UnsafePathError:
            report.unrecoverable.append((file_id, "unsafe_path"))
            continue
        healthy = src_path.is_file() and src_path.stat().st_size == size
        if healthy and verify:
            healthy = sha256_of(src_path) == sha
        if healthy:
            report.ok += 1
            continue
        if not bak_path.is_file():
            report.unrecoverable.append((file_id, "no_backup_copy"))
            continue
        if sha256_of(bak_path) != sha:
            report.unrecoverable.append((file_id, "backup_copy_corrupted"))
            continue
        report.restored.append(file_id)
        if not apply:
            continue
        src_path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        temp = src_path.with_name(src_path.name + ".restore-tmp")
        shutil.copyfile(bak_path, temp)
        with open(temp, "rb+") as fh:
            os.fsync(fh.fileno())
        if sha256_of(temp) != sha:
            temp.unlink(missing_ok=True)
            report.restored.pop()
            report.unrecoverable.append((file_id, "copy_verification_failed"))
            continue
        if src_path.exists():  # file rusak disimpan, tidak dihapus
            os.replace(src_path, src_path.with_name(src_path.name + ".corrupt"))
        os.replace(temp, src_path)
        fsync_dir(src_path.parent)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore Server_penyimpanan dari backup HDD")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--database", metavar="latest|FILE")
    parser.add_argument("--files", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--apply", action="store_true", help="benar-benar jalankan (tanpa ini: simulasi)")
    parser.add_argument("--force", action="store_true", help="lanjut walau server masih berjalan (tidak disarankan)")
    args = parser.parse_args()
    if not (args.list or args.database or args.files):
        parser.print_help()
        return 2

    settings = get_settings()
    for label, path, mount in (
        ("SSD", settings.PRIMARY_STORAGE_PATH, settings.PRIMARY_MOUNT_POINT),
        ("HDD", settings.BACKUP_STORAGE_PATH, settings.BACKUP_MOUNT_POINT),
    ):
        if not check_location(path, mount, settings.REQUIRE_MOUNTED_STORAGE).available:
            print(f"GAGAL: {label} ({mount}) tidak tersedia", file=sys.stderr)
            return 1

    if args.list:
        backups = list_db_backups(settings)
        for path in backups:
            print(f"{path.name}  {path.stat().st_size / 1024**2:.1f} MiB")
        print(f"{len(backups)} salinan database")
        return 0

    if args.apply and service_running() and not args.force:
        print("GAGAL: hentikan server dulu: sudo systemctl stop server-penyimpanan", file=sys.stderr)
        return 1
    if not args.apply:
        print("SIMULASI (tidak ada yang diubah). Tambahkan --apply untuk menjalankan.\n")

    from app.database.types import utcnow

    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    if args.database:
        restore_database(settings, args.database, apply=args.apply, stamp=stamp)
    if args.files:
        report = restore_files(settings, apply=args.apply, verify=args.verify)
        verb = "dipulihkan" if args.apply else "akan dipulihkan"
        print(f"File: {report.checked} diperiksa, {report.ok} sehat, {len(report.restored)} {verb}, "
              f"{len(report.unrecoverable)} tidak bisa dipulihkan")
        for file_id, reason in report.unrecoverable[:50]:
            print(f"  tidak bisa dipulihkan {file_id}: {reason}")
        if report.unrecoverable:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
