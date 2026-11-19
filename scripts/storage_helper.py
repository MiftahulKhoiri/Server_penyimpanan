#!/usr/bin/env python3
"""Helper root untuk pemasangan SSD/HDD dari aplikasi Server_penyimpanan.

Server berjalan sebagai user biasa dengan hardening systemd, jadi tidak bisa me-mount disk.
Helper kecil ini dijalankan systemd sebagai root, satu proses per koneksi
(server-penyimpanan-storage.socket, Accept=yes): membaca SATU permintaan JSON dari stdin dan
menulis SATU respons JSON ke stdout. Hanya grup layanan yang bisa terhubung ke socket-nya.

Prinsip keamanan (semua input divalidasi ulang di sini, server tidak dipercaya begitu saja):
- Tidak pernah memformat, mempartisi, atau menghapus data.
- Hanya partisi berfilesystem Linux (ext4/xfs/btrfs/f2fs) yang punya UUID yang bisa dipasang.
  Disk sistem (kartu SD / disk yang memuat /, /boot, swap) selalu ditolak.
- Mount point hanya /mnt/<nama>, /media/<nama>, atau /srv/<nama>, dan tidak boleh folder berisi.
- Disk dipasang dengan nosuid,nodev,noexec.
- fstab, konfigurasi, dan unit systemd ditulis atomik, dicadangkan lebih dulu, dan dipulihkan
  otomatis bila ada langkah yang gagal.
- SSD hanya bisa dipindah lokasi mount-nya (disk yang sama). Melepas/mengganti SSD tetap lewat
  SSH, karena database server ada di SSD: tanpa SSD aplikasi tidak bisa login lagi.

Hanya memakai pustaka standar Python (dijalankan dengan python3 sistem, bukan venv).

Pemakaian manual (debug):  sudo python3 storage_helper.py --status
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
ALLOWED_FS = ("ext4", "xfs", "btrfs", "f2fs")
MOUNT_OPTIONS = "noatime,nosuid,nodev,noexec"
FSTAB_OPTIONS = "defaults,noatime,nosuid,nodev,noexec,nofail,x-systemd.device-timeout=30s"
MOUNT_POINT_RE = re.compile(r"^/(mnt|media|srv)/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
UUID_RE = re.compile(r"^[0-9A-Fa-f][0-9A-Fa-f-]{3,63}$")
SYSTEM_MOUNTS = ("/", "/boot", "/boot/firmware", "/usr", "/var", "/home", "/opt", "/etc", "[SWAP]")
RUN_DIR = "/run/server-penyimpanan-storage"
MANAGED_MARK = "# server-penyimpanan:{role} - dikelola aplikasi Server_penyimpanan, jangan diubah manual"
MARK_RE = re.compile(r"^# server-penyimpanan:(ssd|hdd)\b")
DISABLED_PREFIX = "# [dinonaktifkan server-penyimpanan] "
BACKUPS_KEEP = 5
HEALTH_WAIT_SECONDS = 120
IGNORED_DISK_PREFIXES = ("zram", "ram", "mtdblock")

DEFAULTS = {
    "PRIMARY_MOUNT_POINT": "/mnt/ssd",
    "PRIMARY_STORAGE_PATH": "/mnt/ssd/storage",
    "DATABASE_PATH": "/mnt/ssd/database/server.db",
    "BACKUP_MOUNT_POINT": "/mnt/hdd",
    "BACKUP_STORAGE_PATH": "/mnt/hdd/backup",
    "HOST": "0.0.0.0",
    "PORT": "8000",
}


@dataclass(frozen=True)
class RoleSpec:
    label: str
    mount_key: str
    # (kunci konfigurasi, apakah path file) - direktori yang disiapkan untuk user layanan
    path_keys: tuple[tuple[str, bool], ...]


ROLES: dict[str, RoleSpec] = {
    "ssd": RoleSpec("SSD", "PRIMARY_MOUNT_POINT", (("PRIMARY_STORAGE_PATH", False), ("DATABASE_PATH", True))),
    "hdd": RoleSpec("HDD", "BACKUP_MOUNT_POINT", (("BACKUP_STORAGE_PATH", False),)),
}

REASON_TEXT = {
    "system_disk": "Disk sistem (kartu SD/boot) tidak boleh dipakai",
    "no_filesystem": "Belum diformat (tidak ada filesystem)",
    "unsupported_filesystem": "Filesystem tidak didukung; pakai ext4 (format lewat SSH)",
    "no_uuid": "Partisi tidak punya UUID",
    "mounted_elsewhere": "Sedang terpasang di lokasi lain",
}


class HelperError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now() -> str:
    # Format "...Z" seperti waktu lain dari server (aman untuk Instant.parse di Android).
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"storage-helper: {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- konfigurasi (.env)
def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def set_env_keys(text: str, updates: dict[str, str]) -> str:
    """Ganti nilai KEY=... yang ada (semua kemunculan) atau tambahkan di akhir; baris lain utuh."""
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^([A-Z0-9_]+)=", line)
        if match and match.group(1) in updates:
            key = match.group(1)
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    missing = [key for key in updates if key not in seen]
    if missing:
        out.append("")
        out.append("# Diubah lewat aplikasi (Admin server > Disk)")
        out.extend(f"{key}={updates[key]}" for key in missing)
    return "\n".join(out) + "\n"


def reroot(path: str, old_root: str, new_root: str) -> str:
    """'/mnt/ssd/storage', '/mnt/ssd', '/mnt/data' -> '/mnt/data/storage'."""
    pure = PurePosixPath(path)
    old = PurePosixPath(old_root)
    if pure == old:
        return new_root
    try:
        relative = pure.relative_to(old)
    except ValueError as exc:
        raise HelperError(
            "config_inconsistent", f"{path} tidak berada di bawah {old_root}; perbaiki konfigurasi lewat SSH"
        ) from exc
    return str(PurePosixPath(new_root) / relative)


def validate_mount_point(value: object) -> str:
    if not isinstance(value, str) or not MOUNT_POINT_RE.fullmatch(value):
        raise HelperError(
            "invalid_mount_point",
            "Lokasi mount harus berbentuk /mnt/<nama>, /media/<nama>, atau /srv/<nama> "
            "(huruf, angka, titik, minus, garis bawah)",
        )
    return value


def validate_uuid(value: object) -> str:
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise HelperError("invalid_uuid", "UUID partisi tidak valid")
    return value


def validate_role(value: object) -> str:
    if value not in ROLES:
        raise HelperError("invalid_role", "Peran disk harus 'ssd' atau 'hdd'")
    return str(value)


# ---------------------------------------------------------------- fstab
def _unescape(field: str) -> str:
    return field.replace("\\040", " ").replace("\\011", "\t")


def fstab_line(uuid: str, mount_point: str, fstype: str) -> str:
    return f"UUID={uuid} {mount_point} {fstype} {FSTAB_OPTIONS} 0 2"


def parse_managed(text: str) -> dict[str, dict[str, str]]:
    """Entri fstab yang dikelola aplikasi: {peran: {uuid, mount_point, fstype}}."""
    lines = text.splitlines()
    managed: dict[str, dict[str, str]] = {}
    for index, line in enumerate(lines):
        match = MARK_RE.match(line)
        if not match or index + 1 >= len(lines):
            continue
        fields = lines[index + 1].split()
        if len(fields) >= 3 and fields[0].startswith("UUID="):
            managed[match.group(1)] = {
                "uuid": fields[0][len("UUID="):],
                "mount_point": _unescape(fields[1]),
                "fstype": fields[2],
            }
    return managed


def rewrite_fstab(
    text: str,
    role: str,
    entry: tuple[str, str, str] | None,
    free_mount_points: set[str],
    free_specs: set[str],
) -> str:
    """Hapus blok terkelola milik `role`, nonaktifkan (jadikan komentar) baris manual yang memakai
    mount point / perangkat yang sama, lalu tambahkan blok baru bila `entry` (uuid, mp, fstype) ada.
    Blok terkelola peran lain tidak pernah disentuh."""
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = MARK_RE.match(line)
        if match:
            nxt = lines[index + 1] if index + 1 < len(lines) else None
            has_entry = nxt is not None and nxt.strip() != "" and not nxt.lstrip().startswith("#")
            if match.group(1) != role:
                out.append(line)
                if has_entry:
                    out.append(nxt)  # type: ignore[arg-type]
            index += 2 if has_entry else 1
            continue
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            fields = stripped.split()
            if len(fields) >= 2 and (_unescape(fields[1]) in free_mount_points or fields[0] in free_specs):
                out.append(DISABLED_PREFIX + line)
                index += 1
                continue
        out.append(line)
        index += 1
    while out and not out[-1].strip():
        out.pop()
    if entry is not None:
        out += ["", MANAGED_MARK.format(role=role), fstab_line(*entry)]
    return "\n".join(out) + "\n"


def count_fstab_errors(output: str) -> int:
    return sum(1 for line in output.splitlines() if "[E]" in line)


# ---------------------------------------------------------------- unit systemd
def rewrite_unit(text: str, ssd_mount: str, hdd_mount: str) -> str:
    line = f"ReadWritePaths=-{ssd_mount} -{hdd_mount} -{RUN_DIR}"
    new, count = re.subn(r"(?m)^ReadWritePaths=.*$", line, text, count=1)
    if count:
        return new
    new, count = re.subn(r"(?m)^(ProtectSystem=.*)$", lambda m: f"{m.group(1)}\n{line}", text, count=1)
    if not count:
        raise HelperError("unit_unrecognized", "Unit systemd server tidak dikenali; pasang ulang dengan install_pi.sh")
    return new


# ---------------------------------------------------------------- daftar disk (lsblk)
def _as_bool(value: object) -> bool:
    return value in (True, 1, "1", "true")


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[call-overload,no-any-return]
    except (TypeError, ValueError):
        return None


def _walk(node: dict, out: list[dict]) -> None:
    out.append(node)
    for child in node.get("children") or []:
        _walk(child, out)


def parse_lsblk(
    data: dict, role_mounts: dict[str, str], managed: dict[str, dict[str, str]], allow_loop: bool = False
) -> list[dict[str, Any]]:
    """Ubah keluaran `lsblk -J` menjadi daftar partisi (atau disk tanpa partisi) beserta
    alasan bila tidak bisa dipakai."""
    disk_types = ("disk", "loop") if allow_loop else ("disk",)
    managed_uuid = {info["uuid"]: role for role, info in managed.items()}
    mount_role = {mount: role for role, mount in role_mounts.items()}
    partitions: list[dict[str, Any]] = []
    for disk in data.get("blockdevices") or []:
        name = str(disk.get("name") or "")
        if disk.get("type") not in disk_types or name.startswith(IGNORED_DISK_PREFIXES):
            continue
        nodes: list[dict] = []
        _walk(disk, nodes)
        system = any(n.get("mountpoint") in SYSTEM_MOUNTS for n in nodes)
        candidates = nodes[1:] if disk.get("children") else nodes
        for node in candidates:
            if node.get("type") not in ("part", "disk", "loop"):
                continue
            fstype = node.get("fstype") or None
            uuid = node.get("uuid") or None
            mountpoint = node.get("mountpoint") or None
            role = mount_role.get(mountpoint) if mountpoint else None
            if role is None and uuid in managed_uuid:
                role = managed_uuid[uuid]
            if system:
                reason = "system_disk"
            elif not fstype:
                reason = "no_filesystem"
            elif fstype not in ALLOWED_FS:
                reason = "unsupported_filesystem"
            elif not uuid:
                reason = "no_uuid"
            elif mountpoint and mountpoint not in mount_role:
                reason = "mounted_elsewhere"
            else:
                reason = None
            partitions.append({
                "path": node.get("path") or f"/dev/{node.get('name')}",
                "name": node.get("name"),
                "disk": name,
                "size_bytes": _as_int(node.get("size")),
                "fstype": fstype,
                "uuid": uuid,
                "label": node.get("label") or None,
                "model": (disk.get("model") or "").strip() or None,
                "transport": disk.get("tran") or None,
                "removable": _as_bool(disk.get("rm")),
                "mountpoint": mountpoint,
                "system": system,
                "usable": reason is None,
                "reason": reason,
                "reason_text": REASON_TEXT.get(reason) if reason else None,
                "role": role,
            })
    return partitions


# ---------------------------------------------------------------- akses sistem (bisa diganti di tes)
class System:
    """Semua efek samping ke sistem lewat sini, supaya logika helper bisa diuji tanpa root."""

    def run(self, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)

    def path(self, value: str) -> Path:
        return Path(value)

    def mounts(self) -> dict[str, tuple[str, str]]:
        table: dict[str, tuple[str, str]] = {}
        try:
            text = Path("/proc/self/mounts").read_text(encoding="utf-8")
        except OSError:
            return table
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                table[_unescape(parts[1])] = (parts[0], parts[2])
        return table

    def lsblk(self) -> dict:
        result = self.run(
            ["lsblk", "-J", "-b", "-o", "NAME,PATH,TYPE,SIZE,FSTYPE,UUID,LABEL,MOUNTPOINT,MODEL,TRAN,RM,PKNAME"],
            timeout=30,
        )
        if result.returncode != 0:
            raise HelperError("lsblk_failed", f"Gagal membaca daftar disk: {result.stderr.strip()}")
        data = json.loads(result.stdout or "{}")
        # lsblk membaca FSTYPE/UUID dari database udev, yang bisa belum terisi sesaat setelah disk
        # dicolok (atau tidak ada sama sekali di container). Lengkapi dengan probe langsung (blkid).
        nodes: list[dict] = []
        for disk in data.get("blockdevices") or []:
            _walk(disk, nodes)
        for node in nodes:
            if node.get("children") or (node.get("fstype") and node.get("uuid")) or not node.get("path"):
                continue
            if node.get("type") not in ("part", "disk", "loop") or not node.get("size"):
                continue
            probe = self.run(["blkid", "-p", "-o", "export", str(node["path"])], timeout=15)
            if probe.returncode != 0:
                continue
            info = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
            node["fstype"] = node.get("fstype") or info.get("TYPE")
            node["uuid"] = node.get("uuid") or info.get("UUID")
            node["label"] = node.get("label") or info.get("LABEL")
        return data

    def chown_service(self, path: Path, user: str) -> None:
        try:
            entry = pwd.getpwnam(user)
        except KeyError as exc:
            raise HelperError("service_user_missing", f"User layanan '{user}' tidak ada") from exc
        os.chown(path, entry.pw_uid, entry.pw_gid)

    def disk_usage(self, path: Path) -> tuple[int, int] | None:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return None
        return usage.total, usage.free

    def health(self, host: str, port: str) -> int | None:
        url = f"http://{host}:{port}/api/v1/system/health"
        try:
            with urllib.request.urlopen(url, timeout=3) as response:  # URL lokal tetap (127.0.0.1)
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)  # 503 = server hidup tetapi degraded: tetap dihitung merespons
        except (urllib.error.URLError, OSError):
            return None

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# ---------------------------------------------------------------- job
class JobLog:
    """Status operasi terakhir, disimpan di state dir agar bisa dibaca setelah server restart."""

    def __init__(self, state_dir: Path, op: str, role: str, detail: dict[str, Any]) -> None:
        self.path = state_dir / "job.json"
        previous = read_json(self.path) or {}
        self.data: dict[str, Any] = {
            "id": int(previous.get("id") or 0) + 1,
            "op": op,
            "role": role,
            "status": "running",
            "detail": detail,
            "steps": [],
            "message": None,
            "error_code": None,
            "started_at": utc_now(),
            "finished_at": None,
        }
        self.save()

    def step(self, text: str) -> None:
        log(text)
        self.data["steps"].append(text)
        self.save()

    def finish(self, ok: bool, message: str, code: str | None = None) -> None:
        self.data.update(
            status="succeeded" if ok else "failed", message=message, error_code=code, finished_at=utc_now()
        )
        self.save()

    def save(self) -> None:
        write_text_atomic(self.path, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_text_atomic(path: Path, text: str) -> None:
    """Tulis atomik; pemilik & mode file lama dipertahankan."""
    previous = path.stat() if path.exists() else None
    tmp = path.with_name(f".{path.name}.sp-tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if previous is not None:
        os.chmod(tmp, previous.st_mode & 0o7777)
        os.chown(tmp, previous.st_uid, previous.st_gid)
    os.replace(tmp, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------- helper
Continuation = Callable[[], None]


class StorageHelper:
    def __init__(self, system: System, env: dict[str, str]) -> None:
        self.sys = system
        self.config_file = Path(env.get("SP_CONFIG_FILE", "/etc/server-penyimpanan/server.env"))
        self.unit_file = Path(env.get("SP_UNIT_FILE", "/etc/systemd/system/server-penyimpanan.service"))
        self.service = env.get("SP_SERVICE_NAME", "server-penyimpanan")
        self.service_user = env.get("SP_SERVICE_USER", "penyimpanan")
        self.fstab = Path(env.get("SP_FSTAB", "/etc/fstab"))
        self.state_dir = Path(env.get("SP_STATE_DIR", "/var/lib/server-penyimpanan-storage"))
        self.allow_loop = env.get("SP_ALLOW_LOOP") == "1"
        self.health_wait = int(env.get("SP_HEALTH_WAIT", str(HEALTH_WAIT_SECONDS)))
        self._lock_fd: int | None = None

    # ---------- baca keadaan ----------
    def read_config(self) -> dict[str, str]:
        try:
            values = parse_env(self.config_file.read_text(encoding="utf-8"))
        except OSError as exc:
            raise HelperError("config_unreadable", f"Konfigurasi {self.config_file} tidak bisa dibaca") from exc
        return {key: values.get(key) or default for key, default in DEFAULTS.items()}

    def role_mounts(self, config: dict[str, str]) -> dict[str, str]:
        return {role: config[spec.mount_key] for role, spec in ROLES.items()}

    def read_fstab(self) -> str:
        try:
            return self.fstab.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def partitions(self, config: dict[str, str], fstab_text: str) -> list[dict[str, Any]]:
        return parse_lsblk(self.sys.lsblk(), self.role_mounts(config), parse_managed(fstab_text), self.allow_loop)

    def read_job(self) -> dict | None:
        return read_json(self.state_dir / "job.json")

    def status(self) -> dict[str, Any]:
        config = self.read_config()
        fstab_text = self.read_fstab()
        managed = parse_managed(fstab_text)
        partitions = self.partitions(config, fstab_text)
        by_path = {p["path"]: p for p in partitions}
        mounts = self.sys.mounts()
        roles = []
        for role, spec in ROLES.items():
            mount_point = config[spec.mount_key]
            source, fstype = mounts.get(mount_point, (None, None))
            part = by_path.get(source) if source else None
            usage = self.sys.disk_usage(self.sys.path(mount_point)) if source else None
            roles.append({
                "role": role,
                "mount_point": mount_point,
                "paths": {key: config[key] for key, _ in spec.path_keys},
                "mounted": source is not None,
                "device": source,
                "uuid": part["uuid"] if part else None,
                "fstype": fstype,
                "label": part["label"] if part else None,
                "model": part["model"] if part else None,
                "managed": role in managed,
                "fstab_uuid": managed.get(role, {}).get("uuid"),
                "total_bytes": usage[0] if usage else None,
                "free_bytes": usage[1] if usage else None,
            })
        return {"version": PROTOCOL_VERSION, "roles": roles, "partitions": partitions, "job": self.read_job()}

    # ---------- utilitas operasi ----------
    def lock(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.state_dir / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise HelperError("busy", "Operasi disk lain sedang berjalan, coba lagi sebentar") from exc
        self._lock_fd = fd  # dilepas otomatis saat proses selesai

    def release(self) -> None:
        """Lepas kunci lebih awal (proses produksi cukup selesai; dipakai di tes)."""
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def backup_files(self, job_id: int) -> Path:
        root = self.state_dir / "backups"
        target = root / f"{job_id:06d}"
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        for source in (self.fstab, self.config_file, self.unit_file):
            if source.exists():
                shutil.copy2(source, target / source.name)
        for old in sorted(p for p in root.iterdir() if p.is_dir())[:-BACKUPS_KEEP]:
            shutil.rmtree(old, ignore_errors=True)
        return target

    def restore_file(self, backup_dir: Path, original: Path) -> None:
        saved = backup_dir / original.name
        if saved.exists():
            write_text_atomic(original, saved.read_text(encoding="utf-8"))

    def write_fstab(self, text: str) -> None:
        """Tulis fstab baru setelah diverifikasi: tidak boleh menambah error dibanding sebelumnya."""
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        candidate = self.state_dir / "fstab.candidate"
        candidate.write_text(text, encoding="utf-8")
        before: subprocess.CompletedProcess[str] | None = None
        after: subprocess.CompletedProcess[str] | None = None
        try:
            before = self.sys.run(["findmnt", "--verify", "--tab-file", str(self.fstab)], timeout=30)
            after = self.sys.run(["findmnt", "--verify", "--tab-file", str(candidate)], timeout=30)
        except FileNotFoundError:
            pass  # findmnt tidak ada: lewati verifikasi
        finally:
            candidate.unlink(missing_ok=True)
        if (
            before is not None and after is not None
            and count_fstab_errors(after.stdout + after.stderr) > count_fstab_errors(before.stdout + before.stderr)
        ):
            raise HelperError("fstab_invalid", f"fstab baru tidak valid: {(after.stdout + after.stderr).strip()}")
        write_text_atomic(self.fstab, text)

    def mount(self, uuid: str, fstype: str, mount_point: str) -> None:
        target = self.sys.path(mount_point)
        target.mkdir(parents=True, exist_ok=True, mode=0o755)
        if mount_point not in self.sys.mounts() and any(target.iterdir()):
            raise HelperError(
                "mount_point_not_empty",
                f"Folder {mount_point} berisi file (mungkin di kartu SD); kosongkan/pindahkan dulu lewat SSH",
            )
        result = self.sys.run(["mount", "-t", fstype, "-o", MOUNT_OPTIONS, f"UUID={uuid}", mount_point])
        if result.returncode != 0:
            raise HelperError("mount_failed", f"Gagal memasang disk: {result.stderr.strip() or result.stdout.strip()}")

    def umount(self, mount_point: str) -> None:
        result = self.sys.run(["umount", mount_point])
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            if "busy" in message.lower():
                raise HelperError("device_busy", f"Disk di {mount_point} sedang dipakai, coba lagi nanti")
            raise HelperError("umount_failed", f"Gagal melepas disk: {message}")

    def prepare_dirs(self, role: str, config: dict[str, str]) -> None:
        for key, is_file in ROLES[role].path_keys:
            directory = self.sys.path(str(PurePosixPath(config[key]).parent) if is_file else config[key])
            directory.mkdir(parents=True, exist_ok=True, mode=0o750)
            self.sys.chown_service(directory, self.service_user)
            os.chmod(directory, 0o750)

    def systemctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.sys.run(["systemctl", *args], timeout=180)

    def wait_healthy(self, config: dict[str, str]) -> bool:
        host = config["HOST"]
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        deadline = self.health_wait
        waited = 0.0
        while waited <= deadline:
            if self.sys.health(host, config["PORT"]) is not None:
                return True
            self.sys.sleep(2)
            waited += 2
        return False

    def find_partition(self, config: dict[str, str], fstab_text: str, uuid: str) -> dict[str, Any]:
        for part in self.partitions(config, fstab_text):
            if part["uuid"] == uuid:
                return part
        raise HelperError("partition_not_found", "Partisi tidak ditemukan (disk dicabut?)")

    # ---------- operasi ----------
    def op_mount(self, role: str, uuid: str) -> tuple[dict, Continuation | None]:
        role = validate_role(role)
        uuid = validate_uuid(uuid)
        if role != "hdd":
            raise HelperError(
                "ssd_via_ssh",
                "SSD memuat database server, jadi hanya bisa dipasang/dilepas lewat SSH. "
                "Dari aplikasi Anda bisa memindah lokasi mount-nya.",
            )
        self.lock()
        config = self.read_config()
        fstab_text = self.read_fstab()
        mounts = self.role_mounts(config)
        mount_point = mounts[role]
        other = next(r for r in ROLES if r != role)
        part = self.find_partition(config, fstab_text, uuid)
        if part["mountpoint"] == mounts[other] or parse_managed(fstab_text).get(other, {}).get("uuid") == uuid:
            raise HelperError("used_by_other_role", f"Disk ini sedang dipakai sebagai {ROLES[other].label}")
        if not part["usable"]:
            raise HelperError(part["reason"], part["reason_text"] or "Partisi tidak bisa dipakai")
        current = self.sys.mounts().get(mount_point)
        already = current is not None and current[0] == part["path"]
        if current is not None and not already:
            raise HelperError("mount_point_busy", f"Sudah ada disk lain di {mount_point}; lepas dulu")

        job = JobLog(self.state_dir, "mount", role, {"uuid": uuid, "device": part["path"], "mount_point": mount_point})
        backup = self.backup_files(job.data["id"])
        mounted_now = False
        try:
            if not already:
                job.step(f"Memasang {part['path']} ({part['fstype']}) di {mount_point}")
                self.mount(uuid, part["fstype"], mount_point)
                mounted_now = True
            job.step("Menyimpan ke /etc/fstab (otomatis terpasang saat boot)")
            self.write_fstab(rewrite_fstab(
                fstab_text, role, (uuid, mount_point, part["fstype"]),
                free_mount_points={mount_point}, free_specs={f"UUID={uuid}", part["path"]},
            ))
            job.step("Menyiapkan folder data")
            self.prepare_dirs(role, config)
        except Exception as exc:
            self.restore_file(backup, self.fstab)
            if mounted_now:
                self.sys.run(["umount", mount_point])
            code = exc.code if isinstance(exc, HelperError) else "internal_error"
            job.finish(False, str(exc), code)
            raise
        job.finish(True, f"{ROLES[role].label} terpasang di {mount_point}")
        return {"job": job.data}, None

    def op_unmount(self, role: str) -> tuple[dict, Continuation | None]:
        role = validate_role(role)
        if role != "hdd":
            raise HelperError("ssd_via_ssh", "SSD memuat database server, jadi hanya bisa dilepas lewat SSH")
        self.lock()
        config = self.read_config()
        fstab_text = self.read_fstab()
        mount_point = self.role_mounts(config)[role]
        current = self.sys.mounts().get(mount_point)
        job = JobLog(self.state_dir, "unmount", role, {"mount_point": mount_point, "device": current and current[0]})
        backup = self.backup_files(job.data["id"])
        try:
            if current is not None:
                job.step(f"Melepas {current[0]} dari {mount_point}")
                self.umount(mount_point)
            job.step("Menghapus entri dari /etc/fstab")
            self.write_fstab(rewrite_fstab(fstab_text, role, None, free_mount_points={mount_point}, free_specs=set()))
        except Exception as exc:
            self.restore_file(backup, self.fstab)
            code = exc.code if isinstance(exc, HelperError) else "internal_error"
            job.finish(False, str(exc), code)
            raise
        job.finish(True, f"{ROLES[role].label} dilepas; aman untuk dicabut")
        return {"job": job.data}, None

    def op_set_mount_point(self, role: str, mount_point: str) -> tuple[dict, Continuation | None]:
        role = validate_role(role)
        new_mp = validate_mount_point(mount_point)
        self.lock()
        config = self.read_config()
        fstab_text = self.read_fstab()
        spec = ROLES[role]
        old_mp = config[spec.mount_key]
        other = next(r for r in ROLES if r != role)
        if new_mp == old_mp:
            raise HelperError("no_change", "Lokasi mount sama dengan yang sekarang")
        if new_mp == self.role_mounts(config)[other]:
            raise HelperError("mount_point_conflict", f"Lokasi itu dipakai {ROLES[other].label}")
        mounts = self.sys.mounts()
        if new_mp in mounts:
            raise HelperError("mount_point_busy", f"Sudah ada filesystem lain terpasang di {new_mp}")
        target = self.sys.path(new_mp)
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise HelperError("mount_point_not_empty", f"Folder {new_mp} sudah ada dan tidak kosong")

        current = mounts.get(old_mp)
        device: dict[str, Any] | None = None
        if current is not None:
            by_path = {p["path"]: p for p in self.partitions(config, fstab_text)}
            device = by_path.get(current[0])
            if device is None or not device["uuid"]:
                raise HelperError("no_uuid", f"Perangkat {current[0]} tidak punya UUID; pindahkan lewat SSH")
        elif role == "ssd":
            raise HelperError("not_mounted", "SSD tidak terpasang; periksa lewat SSH")

        new_config = dict(config)
        new_config[spec.mount_key] = new_mp
        for key, _ in spec.path_keys:
            new_config[key] = reroot(config[key], old_mp, new_mp)
        managed = parse_managed(fstab_text).get(role)

        job = JobLog(self.state_dir, "set_mount_point", role, {"from": old_mp, "to": new_mp})

        def run() -> None:
            self._move_mount_point(job, role, config, new_config, fstab_text, device, managed)

        return {"job": job.data}, run

    def _move_mount_point(
        self,
        job: JobLog,
        role: str,
        config: dict[str, str],
        new_config: dict[str, str],
        fstab_text: str,
        device: dict[str, Any] | None,
        managed: dict[str, str] | None,
    ) -> None:
        spec = ROLES[role]
        old_mp, new_mp = config[spec.mount_key], new_config[spec.mount_key]
        backup = self.backup_files(job.data["id"])
        done: list[str] = []
        try:
            job.step("Menghentikan server sementara")
            self.systemctl("stop", self.service)
            done.append("stopped")
            if device is not None:
                job.step(f"Melepas {device['path']} dari {old_mp}")
                self.umount(old_mp)
                done.append("unmounted")
                job.step(f"Memasang {device['path']} di {new_mp}")
                self.mount(device["uuid"], device["fstype"], new_mp)
                done.append("mounted")
            if device is not None:
                entry: tuple[str, str, str] | None = (device["uuid"], new_mp, device["fstype"])
                specs = {f"UUID={device['uuid']}", device["path"]}
            elif managed is not None:
                entry, specs = (managed["uuid"], new_mp, managed["fstype"]), {f"UUID={managed['uuid']}"}
            else:
                entry, specs = None, set()
            job.step("Memperbarui /etc/fstab")
            self.write_fstab(rewrite_fstab(fstab_text, role, entry, {old_mp, new_mp}, specs))
            job.step("Memperbarui konfigurasi server")
            updates = {spec.mount_key: new_config[spec.mount_key]}
            updates.update({key: new_config[key] for key, _ in spec.path_keys})
            write_text_atomic(self.config_file, set_env_keys(self.config_file.read_text(encoding="utf-8"), updates))
            if self.unit_file.exists():
                job.step("Memperbarui izin tulis layanan (systemd)")
                write_text_atomic(self.unit_file, rewrite_unit(
                    self.unit_file.read_text(encoding="utf-8"),
                    new_config["PRIMARY_MOUNT_POINT"], new_config["BACKUP_MOUNT_POINT"],
                ))
            self.systemctl("daemon-reload")
            if device is not None:
                job.step("Menyiapkan folder data")
                self.prepare_dirs(role, new_config)
            job.step("Menjalankan server kembali")
            started = self.systemctl("start", self.service)
            if started.returncode != 0:
                raise HelperError("service_start_failed", f"Server gagal dijalankan: {started.stderr.strip()}")
            if not self.wait_healthy(new_config):
                raise HelperError("service_unhealthy", "Server tidak merespons setelah dijalankan ulang")
        except Exception as exc:  # noqa: BLE001 - apa pun kesalahannya, kembalikan seperti semula
            code = exc.code if isinstance(exc, HelperError) else "internal_error"
            log(f"gagal ({code}): {exc}; memulihkan")
            job.step(f"Gagal: {exc}. Memulihkan pengaturan lama")
            self._rollback(job, backup, done, old_mp, new_mp, device, config)
            job.finish(False, f"{exc} (pengaturan lama dipulihkan)", code)
            return
        self._remove_empty_dir(old_mp)
        job.finish(True, f"{spec.label} sekarang di {new_mp}")

    def _rollback(
        self,
        job: JobLog,
        backup: Path,
        done: list[str],
        old_mp: str,
        new_mp: str,
        device: dict[str, Any] | None,
        config: dict[str, str],
    ) -> None:
        steps: list[tuple[str, Callable[[], object]]] = [
            ("fstab", lambda: self.restore_file(backup, self.fstab)),
            ("konfigurasi", lambda: self.restore_file(backup, self.config_file)),
            ("unit", lambda: self.restore_file(backup, self.unit_file)),
            ("daemon-reload", lambda: self.systemctl("daemon-reload")),
        ]
        if "mounted" in done:
            steps.append(("lepas lokasi baru", lambda: self.sys.run(["umount", new_mp])))
        if "unmounted" in done and device is not None:
            steps.append(("pasang lokasi lama", lambda: self.mount(device["uuid"], device["fstype"], old_mp)))
        if "stopped" in done:
            steps.append(("jalankan server", lambda: self.systemctl("start", self.service)))
        for name, action in steps:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - lanjutkan pemulihan langkah lain
                job.step(f"Pemulihan '{name}' gagal: {exc}")
        self._remove_empty_dir(new_mp)
        if "stopped" in done:
            self.wait_healthy(config)

    def _remove_empty_dir(self, mount_point: str) -> None:
        if not MOUNT_POINT_RE.fullmatch(mount_point) or mount_point in self.sys.mounts():
            return
        try:
            self.sys.path(mount_point).rmdir()  # hanya bila kosong
        except OSError:
            pass

    # ---------- dispatch ----------
    def handle(self, request: dict) -> tuple[dict, Continuation | None]:
        op = request.get("op")
        if op == "ping":
            return {"version": PROTOCOL_VERSION}, None
        if op == "status":
            return self.status(), None
        if op == "job":
            return {"job": self.read_job()}, None
        if op == "mount":
            return self.op_mount(request.get("role"), request.get("uuid"))  # type: ignore[arg-type]
        if op == "unmount":
            return self.op_unmount(request.get("role"))  # type: ignore[arg-type]
        if op == "set_mount_point":
            return self.op_set_mount_point(request.get("role"), request.get("mount_point"))  # type: ignore[arg-type]
        raise HelperError("unknown_op", f"Operasi tidak dikenal: {op!r}")


def respond(helper: StorageHelper, raw: bytes) -> tuple[dict, Continuation | None]:
    if len(raw) > MAX_REQUEST_BYTES:
        return {"ok": False, "code": "request_too_large", "message": "Permintaan terlalu besar"}, None
    try:
        request = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        request = None
    if not isinstance(request, dict):
        return {"ok": False, "code": "bad_request", "message": "Permintaan bukan JSON yang valid"}, None
    try:
        result, continuation = helper.handle(request)
    except HelperError as exc:
        return {"ok": False, "code": exc.code, "message": exc.message}, None
    except Exception as exc:  # noqa: BLE001 - jangan pernah biarkan server tanpa jawaban
        log(f"error tak terduga: {exc!r}")
        return {"ok": False, "code": "internal_error", "message": f"Kesalahan helper: {exc}"}, None
    return {"ok": True, "result": result}, continuation


def close_connection() -> None:
    """Akhiri koneksi ke server SEKARANG, sebelum pekerjaan lanjutan (yang menghentikan server).

    systemd (Accept=yes) memberikan koneksi yang sama sebagai stdin, stdout, DAN fd 3
    (LISTEN_FDS). Selama salah satunya masih terbuka, server tidak menerima EOF dan terus
    menunggu jawaban, padahal helper akan menghentikan server itu: keduanya saling tunggu
    sampai systemd membunuh server (TimeoutStopSec). shutdown() mengirim EOF ke server walau
    masih ada salinan fd lain; setelah itu semua salinan ditutup.
    """
    try:
        conn = socket.socket(fileno=os.dup(1))
    except OSError:
        conn = None  # stdout bukan socket (mis. dijalankan manual)
    if conn is not None:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            conn.close()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.close(devnull)
    if os.environ.get("LISTEN_PID") == str(os.getpid()):
        for fd in range(3, 3 + int(os.environ.get("LISTEN_FDS") or 0)):
            try:
                os.close(fd)
            except OSError:
                pass


def main(argv: list[str]) -> int:
    helper = StorageHelper(System(), dict(os.environ))
    if "--status" in argv:
        print(json.dumps(helper.status(), indent=2, ensure_ascii=False))
        return 0
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    response, continuation = respond(helper, raw)
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    if continuation is not None:
        close_connection()  # server langsung mendapat jawabannya
        continuation()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
