#!/usr/bin/env python3
"""Pemeriksaan perangkat penyimpanan SSD & HDD untuk Server_penyimpanan.

Hanya memakai pustaka standar Python (bisa dijalankan sebelum venv dibuat).
Tidak mengubah apa pun: hanya membaca dan melaporkan.

Pemakaian:
    python3 scripts/check_storage.py [--env-file /etc/server-penyimpanan/server.env] [--json]

Kode keluar: 0 = SSD siap, 1 = SSD bermasalah (server tidak bisa menyimpan file),
HDD yang bermasalah hanya menghasilkan peringatan (server utama tetap bisa jalan).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULTS = {
    "PRIMARY_MOUNT_POINT": "/mnt/ssd",
    "PRIMARY_STORAGE_PATH": "/mnt/ssd/storage",
    "DATABASE_PATH": "/mnt/ssd/database/server.db",
    "BACKUP_MOUNT_POINT": "/mnt/hdd",
    "BACKUP_STORAGE_PATH": "/mnt/hdd/backup",
}
RECOMMENDED_FS = {"ext4", "xfs", "btrfs", "f2fs"}
FSTAB_OPTIONS = "defaults,noatime,nofail,x-systemd.device-timeout=30s"


@dataclass
class DeviceReport:
    name: str
    mount_point: str
    storage_path: str
    mounted: bool = False
    device: str | None = None
    filesystem: str | None = None
    uuid: str | None = None
    total_bytes: int | None = None
    free_bytes: int | None = None
    writable: bool | None = None
    fstab_entry: str | None = None
    fstab_has_nofail: bool | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggested_fstab: str | None = None


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def mount_table(text: str | None = None) -> dict[str, tuple[str, str]]:
    if text is None:
        try:
            text = Path("/proc/self/mounts").read_text(encoding="utf-8")
        except OSError:
            return {}
    table: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            table[parts[1].replace("\\040", " ")] = (parts[0], parts[2])
    return table


def fstab_entries(text: str) -> dict[str, tuple[str, str]]:
    """mount point -> (baris lengkap, opsi)."""
    entries: dict[str, tuple[str, str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 4:
            entries[parts[1]] = (line, parts[3])
    return entries


def device_uuid(device: str) -> str | None:
    if not shutil.which("blkid"):
        return None
    try:
        out = subprocess.run(
            ["blkid", "-s", "UUID", "-o", "value", device], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def _nearest_dir(path: Path) -> Path:
    current = path
    while not current.is_dir() and current.parent != current:
        current = current.parent
    return current


def write_test(directory: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".write-test-", delete=True) as fh:
            fh.write(b"ok")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError:
        return False


def inspect_device(
    name: str, mount_point: Path, storage_path: Path, *, mounts: dict[str, tuple[str, str]], fstab_text: str,
    critical: bool,
) -> DeviceReport:
    report = DeviceReport(name=name, mount_point=str(mount_point), storage_path=str(storage_path))
    add = report.errors.append if critical else report.warnings.append

    if str(mount_point) == "/":
        report.errors.append("Mount point tidak boleh '/' (root filesystem/kartu SD)")
        return report
    if not storage_path.is_relative_to(mount_point):
        report.errors.append(f"Path {storage_path} tidak berada di bawah mount point {mount_point}")

    report.mounted = os.path.ismount(mount_point)
    entries = fstab_entries(fstab_text)
    if str(mount_point) in entries:
        report.fstab_entry, options = entries[str(mount_point)]
        report.fstab_has_nofail = "nofail" in options.split(",")
        if not report.fstab_has_nofail:
            report.warnings.append(
                "Entri fstab tanpa 'nofail': boot bisa macet ke emergency mode bila disk dicabut"
            )

    if not mount_point.exists():
        add(f"Mount point {mount_point} belum ada")
        return report
    if not report.mounted:
        add(f"{mount_point} TIDAK ter-mount (direktori kosong di kartu SD) — server menolak menulis ke sini")
        if report.fstab_entry is None:
            report.suggested_fstab = (
                f"UUID=<uuid-partisi> {mount_point} ext4 {FSTAB_OPTIONS} 0 2   "
                "(lihat UUID dengan: sudo blkid)"
            )
        return report

    report.device, report.filesystem = mounts.get(str(mount_point), (None, None))
    if report.device and report.device.startswith("/dev/"):
        report.uuid = device_uuid(report.device)
    if report.filesystem and report.filesystem not in RECOMMENDED_FS:
        report.warnings.append(
            f"Filesystem {report.filesystem} kurang cocok (izin Linux/fsync); disarankan ext4"
        )
    try:
        if os.stat(mount_point).st_dev == os.stat("/").st_dev:
            add("Mount point berada di perangkat yang sama dengan root filesystem")
    except OSError:
        pass
    usage = shutil.disk_usage(mount_point)
    report.total_bytes, report.free_bytes = usage.total, usage.free
    target_dir = _nearest_dir(storage_path)
    report.writable = os.access(target_dir, os.W_OK | os.X_OK) and write_test(target_dir)
    if not report.writable:
        add(f"Tidak bisa menulis ke {target_dir} sebagai user {_current_user()}")
    if report.fstab_entry is None and report.uuid:
        fs = report.filesystem or "ext4"
        report.suggested_fstab = f"UUID={report.uuid} {mount_point} {fs} {FSTAB_OPTIONS} 0 2"
        report.warnings.append("Belum ada entri fstab: disk tidak akan ter-mount otomatis saat boot")
    return report


def _current_user() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError):
        return str(os.geteuid())


def _gib(value: int | None) -> str:
    return "-" if value is None else f"{value / 1024**3:.1f} GiB"


def run(config: dict[str, str], fstab_text: str) -> tuple[list[DeviceReport], int]:
    cfg = {**DEFAULTS, **{k: v for k, v in config.items() if v}}
    mounts = mount_table()
    ssd = inspect_device(
        "SSD (utama)", Path(cfg["PRIMARY_MOUNT_POINT"]), Path(cfg["PRIMARY_STORAGE_PATH"]),
        mounts=mounts, fstab_text=fstab_text, critical=True,
    )
    db_path = Path(cfg["DATABASE_PATH"])
    if not db_path.is_relative_to(Path(cfg["PRIMARY_MOUNT_POINT"])):
        ssd.errors.append(f"DATABASE_PATH {db_path} harus berada di SSD ({cfg['PRIMARY_MOUNT_POINT']})")
    hdd = inspect_device(
        "HDD (backup)", Path(cfg["BACKUP_MOUNT_POINT"]), Path(cfg["BACKUP_STORAGE_PATH"]),
        mounts=mounts, fstab_text=fstab_text, critical=False,
    )
    if ssd.mounted and hdd.mounted and ssd.device and ssd.device == hdd.device:
        hdd.warnings.append("SSD dan HDD adalah perangkat yang sama: backup tidak melindungi dari kerusakan disk")
    return [ssd, hdd], (1 if ssd.errors else 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Periksa SSD & HDD Server_penyimpanan (read-only)")
    parser.add_argument("--env-file", type=Path, default=None, help="file konfigurasi (default: /etc/server-penyimpanan/server.env bila ada)")
    parser.add_argument("--fstab", type=Path, default=Path("/etc/fstab"))
    parser.add_argument("--json", action="store_true", help="keluaran JSON")
    args = parser.parse_args()

    env_file = args.env_file
    if env_file is None:
        for candidate in (Path("/etc/server-penyimpanan/server.env"), Path(".env")):
            if candidate.is_file():
                env_file = candidate
                break
    config: dict[str, str] = {}
    if env_file is not None:
        try:
            config = parse_env_file(env_file)
        except OSError as exc:
            print(f"Tidak bisa membaca {env_file}: {exc}", file=sys.stderr)
            return 2
    config.update({k: os.environ[k] for k in DEFAULTS if k in os.environ})
    try:
        fstab_text = args.fstab.read_text(encoding="utf-8")
    except OSError:
        fstab_text = ""

    reports, code = run(config, fstab_text)
    if args.json:
        print(json.dumps({"ok": code == 0, "devices": [asdict(r) for r in reports]}, indent=2, ensure_ascii=False))
        return code

    print(f"Konfigurasi: {env_file or 'default'}")
    for r in reports:
        state = "TER-MOUNT" if r.mounted else "TIDAK TER-MOUNT"
        print(f"\n== {r.name}: {r.mount_point} [{state}]")
        if r.mounted:
            print(f"   device     : {r.device}  (fs {r.filesystem}, UUID {r.uuid or '-'})")
            print(f"   kapasitas  : {_gib(r.total_bytes)}, kosong {_gib(r.free_bytes)}")
            print(f"   bisa ditulis: {'ya' if r.writable else 'TIDAK'}  ({r.storage_path})")
        print(f"   fstab      : {r.fstab_entry or '(tidak ada)'}")
        for msg in r.errors:
            print(f"   [GAGAL] {msg}")
        for msg in r.warnings:
            print(f"   [PERINGATAN] {msg}")
        if r.suggested_fstab:
            print(f"   saran baris /etc/fstab:\n     {r.suggested_fstab}")
    print("\nHasil:", "SSD siap dipakai" if code == 0 else "SSD BELUM siap — server tidak akan bisa menyimpan file")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
