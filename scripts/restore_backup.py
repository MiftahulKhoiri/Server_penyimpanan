#!/usr/bin/env python3
"""Restore dari backup di HDD ke SSD.

Secara default hanya SIMULASI (dry-run): menampilkan apa yang akan dilakukan.
Tambahkan --apply untuk benar-benar menjalankan. Hentikan server dulu:
    sudo systemctl stop server-penyimpanan

Hanya SET BACKUP berstatus `completed` yang dipakai. Sebelum mengubah apa pun,
set divalidasi: SHA-256 + integrity_check database, dan (untuk --set) SHA-256
setiap file di manifest. Bila validasi gagal, tidak ada yang diubah.

Perintah:
    --list                        daftar set backup (dan salinan database format lama)
    --set latest|SET_ID           pulihkan database + file dari satu set backup lengkap.
                                  DB saat ini disimpan dulu sebagai server.db.before-restore-<waktu>.bak.
                                  File di SSD yang masih benar tidak pernah ditimpa; file rusak
                                  disimpan sebagai <nama>.corrupt.
    --database latest|SET_ID      pulihkan HANYA database dari set backup
      --allow-legacy              izinkan NAMA_FILE salinan format lama (database/server-*.db,
                                  tanpa manifest; tidak dijamin konsisten dengan file)
    --files                       kembalikan file yang HILANG / RUSAK di SSD (menurut DB saat ini)
                                  dari penyimpanan isi backup. Tiap salinan diverifikasi SHA-256.
      --verify                    juga hitung SHA-256 file SSD yang ada (deteksi rusak; lebih lambat)

Tidak ada file yang dihapus oleh script ini. Bila dihentikan di tengah jalan
(Ctrl+C / listrik padam), setiap penggantian bersifat atomik (file sementara +
rename); jalankan ulang perintah yang sama untuk melanjutkan.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings, get_settings
from app.services.backup_service import (
    BackupSet,
    completed_sets,
    list_sets,
    sqlite_integrity_ok,
    validate_set,
)
from app.services.storage_guard import check_location
from app.services.storage_paths import (
    DIR_MODE,
    StoragePaths,
    UnsafePathError,
    fsync_dir,
)

BLOCK = 1024 * 1024


class RestoreError(SystemExit):
    """Validasi gagal: tidak ada yang diubah."""


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


# ---------------- Pemilihan & validasi ----------------
def list_legacy_db_backups(settings: Settings) -> list[Path]:
    directory = settings.BACKUP_STORAGE_PATH / "database"
    return sorted(directory.glob("server-*.db")) if directory.is_dir() else []


def list_db_backups(settings: Settings) -> list[Path]:
    """Snapshot database dari set backup yang completed (terlama dulu)."""
    return [s.database_path for s in completed_sets(settings.BACKUP_STORAGE_PATH)]


def integrity_ok(path: Path) -> bool:
    return sqlite_integrity_ok(path)


def choose_set(settings: Settings, choice: str) -> BackupSet:
    valid = completed_sets(settings.BACKUP_STORAGE_PATH)
    if choice == "latest":
        if not valid:
            raise RestoreError("GAGAL: tidak ada set backup berstatus completed di HDD")
        return valid[-1]
    name = Path(choice).name
    for candidate in list_sets(settings.BACKUP_STORAGE_PATH):
        if candidate.set_id == name:
            if candidate.status != "completed":
                raise RestoreError(f"GAGAL: set {name} berstatus {candidate.status}, bukan backup valid")
            return candidate
    raise RestoreError(f"GAGAL: set backup {name} tidak ditemukan")


def validated_set(settings: Settings, choice: str, *, check_files: bool) -> BackupSet:
    chosen = choose_set(settings, choice)
    problems = validate_set(settings.BACKUP_STORAGE_PATH, chosen, check_files=check_files)
    if problems:
        shown = ", ".join(problems[:10])
        raise RestoreError(f"GAGAL: set {chosen.set_id} tidak valid ({len(problems)} masalah: {shown})")
    return chosen


# ---------------- Database ----------------
def _replace_database(settings: Settings, source: Path, *, apply: bool, stamp: str) -> None:
    target = settings.DATABASE_PATH
    saved = target.with_name(f"{target.name}.before-restore-{stamp}.bak")
    print(f"Database: {source} -> {target}")
    if target.exists():
        print(f"  DB saat ini disimpan sebagai {saved.name}")
    if not apply:
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    if target.exists() and not saved.exists():
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
    if not integrity_ok(temp):  # salinan rusak di perjalanan: DB produksi tidak disentuh
        temp.unlink(missing_ok=True)
        raise RestoreError("GAGAL: salinan database rusak setelah disalin; DB produksi tidak diubah")
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    os.replace(temp, target)
    fsync_dir(target.parent)
    print("  database dipulihkan")


def restore_database(
    settings: Settings, choice: str, *, apply: bool, stamp: str, allow_legacy: bool = False
) -> Path:
    name = Path(choice).name
    if name.startswith("server-") and name.endswith(".db"):
        if not allow_legacy:
            raise RestoreError("GAGAL: salinan format lama tanpa manifest; tambahkan --allow-legacy bila yakin")
        source = settings.BACKUP_STORAGE_PATH / "database" / name
        if not source.is_file():
            raise RestoreError(f"GAGAL: {name} tidak ditemukan")
        if not integrity_ok(source):
            raise RestoreError(f"GAGAL: salinan {name} rusak (integrity_check gagal)")
        print("PERINGATAN: salinan format lama; file pengguna mungkin tidak cocok dengan database ini")
    else:
        source = validated_set(settings, choice, check_files=False).database_path
    _replace_database(settings, source, apply=apply, stamp=stamp)
    return source


# ---------------- File ----------------
@dataclass
class FileRestoreReport:
    checked: int = 0
    ok: int = 0
    restored: list[str] = field(default_factory=list)
    unrecoverable: list[tuple[str, str]] = field(default_factory=list)


def _restore_entries(
    settings: Settings, entries: Iterable[tuple[str, str, int, str]], *, apply: bool, verify: bool
) -> FileRestoreReport:
    primary = StoragePaths(settings.PRIMARY_STORAGE_PATH)
    backup = StoragePaths(settings.BACKUP_STORAGE_PATH)
    report = FileRestoreReport()
    for file_id, relpath, size, sha in entries:
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


def restore_files(settings: Settings, *, apply: bool, verify: bool) -> FileRestoreReport:
    con = sqlite3.connect(f"file:{settings.DATABASE_PATH}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, storage_path, size_bytes, sha256 FROM files ORDER BY id").fetchall()
    finally:
        con.close()
    return _restore_entries(settings, rows, apply=apply, verify=verify)


def restore_set(settings: Settings, choice: str, *, apply: bool, stamp: str, verify: bool = True) -> FileRestoreReport:
    """Pulihkan database + file dari satu set completed. Validasi PENUH sebelum mengubah apa pun."""
    chosen = validated_set(settings, choice, check_files=True)
    print(f"Set backup {chosen.set_id}: valid ({chosen.manifest.get('file_count', 0)} file)")
    _replace_database(settings, chosen.database_path, apply=apply, stamp=stamp)
    entries = (
        (e["file_id"], e["path"], int(e["size"]), e["sha256"]) for e in chosen.load_manifest().get("files", [])
    )
    return _restore_entries(settings, entries, apply=apply, verify=verify)


def _print_file_report(report: FileRestoreReport, apply: bool) -> int:
    verb = "dipulihkan" if apply else "akan dipulihkan"
    print(f"File: {report.checked} diperiksa, {report.ok} sehat, {len(report.restored)} {verb}, "
          f"{len(report.unrecoverable)} tidak bisa dipulihkan")
    for file_id, reason in report.unrecoverable[:50]:
        print(f"  tidak bisa dipulihkan {file_id}: {reason}")
    return 1 if report.unrecoverable else 0


def _clean_stale_temp(settings: Settings) -> None:
    """Sisa file sementara dari restore yang terputus (bukan data pengguna)."""
    Path(str(settings.DATABASE_PATH) + ".restore-tmp").unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore Server_penyimpanan dari backup HDD")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--set", metavar="latest|SET_ID")
    parser.add_argument("--database", metavar="latest|SET_ID")
    parser.add_argument("--allow-legacy", action="store_true")
    parser.add_argument("--files", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--apply", action="store_true", help="benar-benar jalankan (tanpa ini: simulasi)")
    parser.add_argument("--force", action="store_true", help="lanjut walau server masih berjalan (tidak disarankan)")
    args = parser.parse_args()
    if not (args.list or args.database or args.files or args.set):
        parser.print_help()
        return 2
    if args.set and (args.database or args.files):
        print("GAGAL: --set sudah memulihkan database dan file; jangan digabung dengan --database/--files",
              file=sys.stderr)
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
        sets = list_sets(settings.BACKUP_STORAGE_PATH)
        for item in sets:
            print(f"{item.set_id}  status={item.status}  file={item.manifest.get('file_count', '-')}")
        print(f"{len(sets)} set backup ({len(completed_sets(settings.BACKUP_STORAGE_PATH))} completed)")
        legacy = list_legacy_db_backups(settings)
        if legacy:
            print(f"{len(legacy)} salinan database format lama di database/ (butuh --allow-legacy)")
        return 0

    if args.apply and service_running() and not args.force:
        print("GAGAL: hentikan server dulu: sudo systemctl stop server-penyimpanan", file=sys.stderr)
        return 1
    if not args.apply:
        print("SIMULASI (tidak ada yang diubah). Tambahkan --apply untuk menjalankan.\n")
    else:
        _clean_stale_temp(settings)

    from app.database.types import utcnow

    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    try:
        if args.set:
            return _print_file_report(restore_set(settings, args.set, apply=args.apply, stamp=stamp,
                                                  verify=True), args.apply)
        if args.database:
            restore_database(settings, args.database, apply=args.apply, stamp=stamp, allow_legacy=args.allow_legacy)
        if args.files:
            return _print_file_report(restore_files(settings, apply=args.apply, verify=args.verify), args.apply)
    except RestoreError as exc:
        print(exc.code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
